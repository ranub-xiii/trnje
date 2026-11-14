#!/usr/bin/env python3
"""Standalone Trnje-only GitHub Issues worker. No Emberlane imports or shared state."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from trnje_api_queue import TaskQueue
from trnje_mcp_http import create_server as create_mcp_server

REPO, ISSUE = "ranub-xiii/trnje", 3
ACTORS = {"xiiitore", "ranub-xiii"}
MARK, RESULT = "TRNJE_TASK_JSON", "TRNJE_RESULT_JSON"
HOME = Path.home()
ROOT = HOME / "trnje-e2e-aRhnEF" / "repo"
STATE = HOME / ".local/state/trnje-worker"
LOGS = STATE / "logs"
STATE_FILE, LOCK_FILE = STATE / "state.json", STATE / "worker.lock"
PUBLISHER = HOME / "bin/trnje-publish"
MAX_TEXT, MAX_COMMENT = 24_000, 16_384
MAX_READ_BYTES = 12_000
DROPBOX_API = "https://api.dropboxapi.com/2"
DROPBOX_CONTENT = "https://content.dropboxapi.com/2"
DROPBOX_TOKEN_URL = "https://api.dropbox.com/oauth2/token"
DROPBOX_CONFIG_DEFAULT = HOME / ".config/emberlane/dropbox.env"
DROPBOX_SOURCE_RE = re.compile(r"\n\n<!-- TRNJE_DROPBOX_SOURCE:([0-9a-f]{64}) -->\s*\Z")
DROPBOX_MAX_PAGES_PER_POLL = 5
_DROPBOX_ACCESS_TOKEN = ""
_DROPBOX_ACCESS_TOKEN_EXPIRES = 0.0

REDACT = [
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bsl\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\b(authorization|access[_ -]?token|refresh[_ -]?token|password|client[_ -]?secret|api[_ -]?key|secret[_ -]?key)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
DENIED = {".git", ".ssh", ".config", ".env", "credentials", "credential", "secrets", "secret", "token", "tokens"}


class Blocked(Exception):
    pass


def clean(s: str) -> str:
    for pattern in REDACT:
        s = pattern.sub("[REDACTED]", s)
    return s[:MAX_TEXT]


def env() -> dict[str, str]:
    e = os.environ.copy()
    e.update(GIT_PAGER="cat", PAGER="cat", TERM="dumb", GIT_TERMINAL_PROMPT="0",
             CI="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
             PYTHONPYCACHEPREFIX=str(STATE / "pycache"), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return e


def call(argv: list[str], cwd: Path | None = None, timeout: int = 60) -> dict:
    try:
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout, check=False,
                           shell=False, env=env())
    except subprocess.TimeoutExpired:
        return {"status": "BLOCKED", "exit": None, "output": "", "stdout": "", "stderr": "",
                "detail": f"timeout after {timeout}s"}
    except OSError as exc:
        return {"status": "BLOCKED", "exit": None, "output": "", "stdout": "", "stderr": "",
                "detail": type(exc).__name__}
    stdout, stderr = clean(p.stdout or ""), clean(p.stderr or "")
    out = clean(stdout + stderr)
    return {"status": "PASS" if p.returncode == 0 else "FAIL",
            "exit": p.returncode, "output": out, "stdout": stdout, "stderr": stderr,
            "detail": "" if p.returncode == 0 else f"exit {p.returncode}"}


def root() -> Path:
    if not ROOT.is_dir() or not (ROOT / ".git").exists():
        raise Blocked("Configured Trnje checkout is missing.")
    r = ROOT.resolve(strict=True)
    top = call(["git", "rev-parse", "--show-toplevel"], r, 15)
    if top["status"] != "PASS" or Path(top["output"].strip()).resolve() != r:
        raise Blocked("Git root does not match the configured Trnje workspace.")
    return r


def dirs() -> None:
    for p in (STATE, LOGS):
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            p.chmod(0o700)
        except OSError:
            pass


def load() -> dict:
    if not STATE_FILE.exists():
        return {"version": 1, "last_seen": 0, "tasks": {}}
    try:
        v = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise Blocked(f"State unreadable ({type(exc).__name__}); refusing reset.")
    if (not isinstance(v, dict) or v.get("version") != 1 or not isinstance(v.get("tasks"), dict)
            or not isinstance(v.get("last_seen", 0), int)
            or any(not isinstance(record, dict) for record in v["tasks"].values())):
        raise Blocked("State schema invalid; refusing reset.")
    return v


def save(s: dict) -> None:
    dirs()
    tmp = STATE / f".state-{os.getpid()}-{time.time_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    finally:
        tmp.unlink(missing_ok=True)


GH_RETRY_DELAYS = (2, 5)
GH_TRANSIENT_ERROR = re.compile(
    r"(?i)(TLS handshake timeout|i/o timeout|connection reset by peer|"
    r"connection timed out|temporary failure in name resolution|"
    r"unexpected EOF|unexpected end of file|HTTP 5[0-9]{2})"
)


def gh(args: list[str], timeout: int = 30) -> str:
    """Call GitHub CLI, retrying only bounded, recognizable transient failures."""
    command = ["gh", "api", *args]
    for attempt in range(len(GH_RETRY_DELAYS) + 1):
        try:
            p = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=timeout, check=False, shell=False, env=env())
        except subprocess.TimeoutExpired as exc:
            if attempt < len(GH_RETRY_DELAYS):
                time.sleep(GH_RETRY_DELAYS[attempt])
                continue
            raise Blocked(
                f"GitHub API request failed after {attempt + 1} attempts "
                f"(TimeoutExpired; retry limit reached)."
            ) from exc
        except OSError as exc:
            # Missing executables and local configuration errors are not network retries.
            raise Blocked(f"GitHub API request failed ({type(exc).__name__}).") from exc

        if p.returncode == 0:
            return p.stdout

        error = clean(p.stderr or p.stdout)
        transient = bool(GH_TRANSIENT_ERROR.search(error))
        if transient and attempt < len(GH_RETRY_DELAYS):
            time.sleep(GH_RETRY_DELAYS[attempt])
            continue
        reason = "retry limit reached" if transient else "non-retryable error"
        raise Blocked(
            f"GitHub API request failed after {attempt + 1} attempt(s) "
            f"({reason}): {error[:400]}"
        )
    raise Blocked("GitHub API request failed: retry loop exhausted.")
def verify_gh() -> str:
    if not shutil.which("gh"):
        raise Blocked("GitHub CLI gh is missing.")
    login = gh(["user", "--jq", ".login"], 20).strip()
    if login not in ACTORS:
        raise Blocked("Authenticated GitHub account is not allowlisted.")
    if gh([f"repos/{REPO}/issues/{ISSUE}", "--jq", ".number"], 20).strip() != str(ISSUE):
        raise Blocked("Dedicated Trnje Issue #3 is not accessible.")
    return login


def comments() -> list[dict]:
    endpoint = f"repos/{REPO}/issues/{ISSUE}/comments?per_page=100"
    raw = gh(["--paginate", "--jq", ".[] | @json", endpoint], 45)
    if len(raw.encode()) > 2_000_000:
        raise Blocked("Mailbox response exceeds 2 MB.")
    try:
        items = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except json.JSONDecodeError:
        raise Blocked("GitHub returned malformed mailbox data.")
    return sorted((x for x in items if isinstance(x, dict) and isinstance(x.get("id"), int)),
                  key=lambda x: x["id"])


def post(body: str) -> bool:
    return call(["gh", "api", f"repos/{REPO}/issues/{ISSUE}/comments",
                 "--method", "POST", "-f", f"body={body}"], timeout=30)["status"] == "PASS"


def pairs_no_duplicates(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key")
        d[k] = v
    return d


def parse(body: str) -> tuple[str, dict]:
    if len(body.encode()) > MAX_COMMENT or not body.startswith(MARK + "\n"):
        raise Blocked("Task marker or size invalid.")
    raw = body[len(MARK) + 1:]
    source_match = DROPBOX_SOURCE_RE.search(body)
    if source_match:
        raw = body[len(MARK) + 1:source_match.start()]
    elif "TRNJE_DROPBOX_SOURCE:" in raw:
        raise Blocked("Dropbox source marker is malformed.")
    try:
        obj = json.loads(raw, object_pairs_hook=pairs_no_duplicates)
    except (ValueError, json.JSONDecodeError):
        raise Blocked("Task JSON malformed or has duplicate keys.")
    if not isinstance(obj, dict) or set(obj) != {"kind", "payload"} or not isinstance(obj["payload"], dict):
        raise Blocked("Task schema invalid.")
    kind, p = obj["kind"], obj["payload"]
    schemas = {"diagnostics": set(), "git_status": set(), "run_tests": set(),
               "read_file": {"path"}, "run_task": {"file", "task", "expect"}}
    if not isinstance(kind, str) or kind not in schemas:
        raise Blocked("Task kind or payload fields are not allowlisted.")
    if kind == "read_file":
        if "path" not in p or set(p) - {"path", "offset", "limit"}:
            raise Blocked("read_file accepts only path, offset, and limit.")
        if not isinstance(p["path"], str) or not p["path"].strip():
            raise Blocked("read_file.path must be a non-empty string.")
        offset, limit = p.get("offset", 0), p.get("limit", MAX_READ_BYTES)
        if type(offset) is not int or offset < 0:
            raise Blocked("read_file.offset must be a non-negative integer.")
        if type(limit) is not int or not 4 <= limit <= MAX_READ_BYTES:
            raise Blocked(f"read_file.limit must be between 4 and {MAX_READ_BYTES} bytes.")
    elif set(p) != schemas[kind]:
        raise Blocked("Task kind or payload fields are not allowlisted.")
    if kind == "run_task" and (not all(isinstance(p[k], str) and p[k].strip() for k in ("file", "task", "expect"))
                               or len(p["task"]) > 2000 or len(p["expect"]) > 2000):
        raise Blocked("run_task requires bounded file, task, and expect strings.")
    return kind, p


def file_in_root(r: Path, name: str, max_bytes: int | None = None) -> Path:
    p = Path(name)
    if p.is_absolute() or not name or any(x in ("", ".", "..") for x in p.parts):
        raise Blocked("Path must be a clean relative path.")
    if any(x.lower() in DENIED or x.lower().startswith(".env") or "secret" in x.lower()
           or "credential" in x.lower() for x in p.parts) or p.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
        raise Blocked("Sensitive paths are not accessible.")
    target, current = r / p, r
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise Blocked("Symlink paths are not accessible.")
    if not target.exists():
        raise Blocked("FILE_NOT_FOUND: requested file does not exist.")
    if not target.is_file():
        raise Blocked("NOT_A_FILE: requested path is not a regular file.")
    if max_bytes is not None:
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise Blocked(f"FILE_STAT_ERROR: {type(exc).__name__}.")
        if size > max_bytes:
            raise Blocked(f"FILE_TOO_LARGE: file exceeds {max_bytes} bytes.")
    return target


def read_file_chunk(r: Path, name: str, offset: int = 0, limit: int = MAX_READ_BYTES) -> dict:
    target = file_in_root(r, name)
    try:
        size = target.stat().st_size
        if offset > size:
            raise Blocked(f"OFFSET_OUT_OF_RANGE: {offset} exceeds file size {size}.")
        start = offset
        with target.open("rb") as stream:
            if start:
                stream.seek(start)
                for _ in range(3):
                    byte = stream.read(1)
                    if not byte or byte[0] & 0xC0 != 0x80:
                        break
                    start += 1
                    stream.seek(start)
            stream.seek(start)
            raw = stream.read(limit + 4)
    except Blocked:
        raise
    except OSError as exc:
        raise Blocked(f"FILE_READ_ERROR: {type(exc).__name__}.")
    chunk = raw[:limit]
    while True:
        try:
            content = chunk.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.end == len(chunk):
                chunk = chunk[:exc.start]
                continue
            raise Blocked(f"FILE_NOT_UTF8: invalid UTF-8 near byte {start + exc.start}.")
    end = start + len(chunk)
    has_more = end < size
    next_offset = end if has_more else None
    output = (f"FILE: {name}\nSIZE_BYTES: {size}\nSTART_OFFSET: {start}\nEND_OFFSET: {end}\n"
              f"NEXT_OFFSET: {next_offset if next_offset is not None else 'none'}\n"
              f"COMPLETE: {str(not has_more).lower()}\n\n{content}")
    return {"status": "PASS",
            "detail": f"Read {len(chunk)} bytes at offset {start} of {size}; "
                      + (f"continue at offset {next_offset}." if has_more else "file complete."),
            "exit": 0, "output": clean(output)}


def git_status(r: Path) -> dict:
    data = {"workspace": "$HOME/trnje-e2e-aRhnEF/repo"}
    failed = []
    for name, cmd in (("branch", ["git", "branch", "--show-current"]),
                      ("head", ["git", "rev-parse", "HEAD"]),
                      ("status", ["git", "status", "--short"])):
        x = call(cmd, r, 20)
        data[name] = {k: x[k] for k in ("status", "exit", "output")}
        if x["status"] != "PASS":
            failed.append(x)
    status = "PASS" if not failed else ("BLOCKED" if any(x["status"] == "BLOCKED" for x in failed) else "FAIL")
    return {"status": status, "detail": "Git status captured." if not failed else "Git status incomplete.",
            "exit": 0 if not failed else failed[0]["exit"], "output": json.dumps(data, indent=2)}


def execute(r: Path, kind: str, p: dict) -> dict:
    if kind == "git_status":
        return git_status(r)
    if kind == "diagnostics":
        x = git_status(r)
        data = json.loads(x["output"])
        data.update(gh_available=bool(shutil.which("gh")),
                    trnje_publish_available=PUBLISHER.is_file() and os.access(PUBLISHER, os.X_OK),
                    llama_cli_available=bool(shutil.which(os.environ.get("TRNJE_LLAMA_CLI", "llama-cli"))),
                    model_path_configured=bool(os.environ.get("TRNJE_MODEL_PATH")))
        py = call([sys.executable, "--version"], r, 20)
        data["python"] = {k: py[k] for k in ("status", "exit", "output")}
        status = x["status"] if py["status"] == "PASS" else py["status"]
        return {"status": status, "detail": "Read-only diagnostics.", "exit": 0 if status == "PASS" else None,
                "output": json.dumps(data, indent=2)}
    if kind == "read_file":
        try:
            return read_file_chunk(r, p["path"], p.get("offset", 0), p.get("limit", MAX_READ_BYTES))
        except (Blocked, OSError, UnicodeError) as exc:
            return {"status": "BLOCKED", "detail": str(exc), "exit": None, "output": ""}
    if kind == "run_tests":
        outputs = []
        for cmd in ([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                    [sys.executable, "-m", "compileall", "-q", "trnje", "scripts", "tests"]):
            x = call(cmd, r, 180)
            outputs.append(f"$ {' '.join(cmd)}\n{x['status']} exit={x['exit']}\n{x['output']}")
            if x["status"] != "PASS":
                return {"status": x["status"], "detail": x["detail"], "exit": x["exit"], "output": "\n\n".join(outputs)}
        return {"status": "PASS", "detail": "pytest and compileall passed.", "exit": 0, "output": "\n\n".join(outputs)}
    if kind == "run_task":
        try:
            file_in_root(r, p["file"], max_bytes=20_000)
        except Blocked as exc:
            return {"status": "BLOCKED", "detail": str(exc), "exit": None, "output": ""}
        runner = r / "scripts/run_task.py"
        if not runner.is_file():
            return {"status": "BLOCKED", "detail": "scripts/run_task.py missing.", "exit": None, "output": ""}
        cmd = [sys.executable, str(runner), "--workspace", str(r), "--file", p["file"],
               "--task", p["task"], "--expect", p["expect"], "--model-timeout", "10800", "--check-timeout", "180"]
        # Allow the model timeout plus check execution and process startup/teardown.
        x = call(cmd, r, 11100)
        # The runner's structured TaskReport is authoritative for BLOCKED/FAIL/PASS.
        # Do not collapse every nonzero CLI exit into FAIL.
        if x["exit"] is not None:
            try:
                report = json.loads(x.get("stdout", ""))
                report_status = report.get("status") if isinstance(report, dict) else None
                if report_status not in {"PASS", "FAIL", "BLOCKED", "UNKNOWN"}:
                    raise ValueError("unrecognized TaskReport status")
                expected_exit = 0 if report_status == "PASS" else 1
                if x["exit"] != expected_exit:
                    raise ValueError("TaskReport status and CLI exit code disagree")
                x["status"] = report_status
                x["detail"] = clean(str(report.get("detail", "")))[:400]
                x["task_report"] = report
            except (ValueError, TypeError):
                x["status"] = "UNKNOWN"
                x["detail"] = "Runner exited but its structured TaskReport could not be verified."
        x.pop("stdout", None)
        x.pop("stderr", None)
        after = call(["git", "status", "--short"], r, 20)
        diff = call(["git", "diff", "--stat"], r, 20)
        x["output"] = (x["output"] + "\n\nPost-task worktree:\n"
                       + f"git status: {after['status']} exit={after['exit']}\n{after['output']}\n"
                       + f"git diff --stat: {diff['status']} exit={diff['exit']}\n{diff['output']}")[:MAX_TEXT]
        if (after["status"] != "PASS" or diff["status"] != "PASS") and x["status"] == "PASS":
            x["status"], x["detail"] = "UNKNOWN", "Task command passed but post-task inspection was incomplete."
        return x
    return {"status": "BLOCKED", "detail": "Unsupported task.", "exit": None, "output": ""}


def result_body(cid: int, kind: str, result: dict, publication: dict) -> str:
    up = publication.get("upload", {})
    obj = {"source_comment_id": cid, "kind": kind, "status": result["status"],
           "detail": clean(str(result.get("detail", "")))[:400], "exit_code": result.get("exit"),
           "log_path": up.get("path"), "sha256": up.get("sha256"),
           "readback_verified": up.get("readback_verified"),
           "publication_status": publication.get("status", "FAIL")}
    return RESULT + "\n" + json.dumps(obj, sort_keys=True)


def publish(cid: int, actor: str, kind: str, result: dict) -> dict:
    if not PUBLISHER.is_file() or not os.access(PUBLISHER, os.X_OK):
        return {"status": "FAIL"}
    LOGS.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = LOGS / f"comment-{cid}.md"
    body = (f"# Trnje worker result\n\n- Source comment: {cid}\n- Actor: {actor}\n"
            f"- Kind: {kind}\n- Status: **{result['status']}**\n- Exit: {result.get('exit')}\n\n"
            f"## Detail\n\n{clean(str(result.get('detail', '')))}\n\n## Output\n\n~~~text\n"
            f"{clean(str(result.get('output', '')))}\n~~~\n")
    try:
        with p.open("x", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        x = call([str(PUBLISHER), str(p)], root(), 90)
        if x["status"] != "PASS":
            return {"status": "FAIL"}
        v = json.loads(x["output"])
        return {"status": "PASS", "upload": v.get("upload", {})} if v.get("status") == "PASS" else {"status": "FAIL"}
    except Exception:
        return {"status": "FAIL"}


def deliver(s: dict, items: list[dict]) -> None:
    sent = set()
    for c in items:
        b = c.get("body") or ""
        if b.startswith(RESULT + "\n"):
            try:
                n = json.loads(b.split("\n", 1)[1]).get("source_comment_id")
                if isinstance(n, int):
                    sent.add(n)
            except (ValueError, IndexError):
                pass
    for key, rec in s["tasks"].items():
        if rec.get("status") != "RESULT_PENDING":
            continue
        cid = int(key)
        if cid in sent:
            rec["status"] = rec.pop("task_status", "UNKNOWN")
            rec.pop("body", None)
        elif isinstance(rec.get("body"), str):
            if post(rec["body"]):
                rec["status"] = rec.pop("task_status", "UNKNOWN")
                rec.pop("body", None)
        save(s)


def process(c: dict, s: dict, r: Path) -> None:
    cid, actor, body = c["id"], (c.get("user") or {}).get("login", ""), c.get("body") or ""
    if not body.startswith(MARK):
        return
    key = str(cid)
    if key in s["tasks"]:
        return
    if actor not in ACTORS:
        s["tasks"][key] = {"status": "REJECTED", "reason": "unauthorized_actor"}
        save(s)
        return
    s["tasks"][key] = {"status": "RUNNING", "kind": "unparsed"}
    save(s)
    try:
        kind, payload = parse(body)
        s["tasks"][key]["kind"] = kind
        save(s)
        result = execute(r, kind, payload)
    except Blocked as exc:
        kind = s["tasks"][key].get("kind", "invalid")
        result = {"status": "BLOCKED", "detail": str(exc), "exit": None, "output": ""}
    except Exception as exc:
        kind = s["tasks"][key].get("kind", "invalid")
        result = {"status": "FAIL", "detail": f"Worker operation failed ({type(exc).__name__}).",
                  "exit": None, "output": ""}
    try:
        publication = publish(cid, actor, kind, result)
    except Exception:
        publication = {"status": "FAIL"}
    s["tasks"][key] = {"status": "RESULT_PENDING", "task_status": result["status"],
                       "kind": kind, "body": result_body(cid, kind, result, publication)}
    save(s)




def dropbox_config() -> dict[str, str]:
    """Read the existing Dropbox app credentials without importing Emberlane code."""
    values = dict(os.environ)
    config_path = Path(
        values.get("TRNJE_DROPBOX_CONFIG")
        or values.get("EMBERLANE_DROPBOX_CONFIG")
        or str(DROPBOX_CONFIG_DEFAULT)
    ).expanduser()
    if config_path.exists():
        try:
            if config_path.is_symlink():
                raise Blocked("Dropbox config must not be a symlink.")
            if os.name == "posix" and config_path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise Blocked("Dropbox config permissions are too broad.")
            for line in config_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip("\"' ")
                if (key.startswith("DROPBOX_") or key == "TRNJE_DROPBOX_INBOX") and key not in values:
                    values[key] = value
        except Blocked:
            raise
        except OSError as exc:
            raise Blocked(f"Cannot read Dropbox config ({type(exc).__name__}).") from None

    required = ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise Blocked("BLOCKED_DROPBOX_AUTH: missing " + ", ".join(missing))

    inbox = values.get("TRNJE_DROPBOX_INBOX", "/trnje-inbox").strip()
    inbox_path = Path(inbox)
    if (not inbox_path.is_absolute() or inbox_path == Path("/")
            or "\\" in inbox or any(part in {".", ".."} for part in inbox_path.parts)):
        raise Blocked("TRNJE_DROPBOX_INBOX must be a non-root absolute app-folder path.")
    return {
        "app_key": values["DROPBOX_APP_KEY"],
        "app_secret": values["DROPBOX_APP_SECRET"],
        "refresh_token": values["DROPBOX_REFRESH_TOKEN"],
        "inbox": inbox,
    }


def dropbox_request(
    url: str,
    *,
    headers: dict[str, str],
    data: bytes = b"",
    timeout: float = 20.0,
    max_response_bytes: int = 1_000_000,
) -> tuple[bytes, dict[str, str]]:
    """Bounded Dropbox HTTP request; never expose credentials or response bodies."""
    if type(max_response_bytes) is not int or max_response_bytes < 0:
        raise ValueError("max_response_bytes must be a non-negative integer")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_response_bytes + 1)
            if len(body) > max_response_bytes:
                raise Blocked("Dropbox response exceeded the configured size limit.")
            return body, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raise Blocked(f"Dropbox HTTP {exc.code}.") from None
    except http.client.HTTPException as exc:
        raise Blocked(f"Dropbox response error ({type(exc).__name__}).") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Blocked(f"Dropbox network error ({type(exc).__name__}).") from None


def dropbox_access_token(cfg: dict[str, str]) -> str:
    global _DROPBOX_ACCESS_TOKEN, _DROPBOX_ACCESS_TOKEN_EXPIRES
    if _DROPBOX_ACCESS_TOKEN and time.time() < _DROPBOX_ACCESS_TOKEN_EXPIRES - 60:
        return _DROPBOX_ACCESS_TOKEN
    basic = (cfg["app_key"] + ":" + cfg["app_secret"]).encode("utf-8")
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
    }).encode("utf-8")
    data, _ = dropbox_request(
        DROPBOX_TOKEN_URL,
        headers={
            "Authorization": "Basic " + base64.b64encode(basic).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=form,
        timeout=20,
        max_response_bytes=65_536,
    )
    try:
        result = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise Blocked("Dropbox OAuth response was invalid.") from None
    token = result.get("access_token") if isinstance(result, dict) else None
    expires = result.get("expires_in") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token:
        raise Blocked("Dropbox OAuth response did not contain an access token.")
    _DROPBOX_ACCESS_TOKEN = token
    _DROPBOX_ACCESS_TOKEN_EXPIRES = (
        time.time() + expires if type(expires) is int and expires > 0 else time.time()
    )
    return token


def dropbox_list_page(token: str, inbox: str, cursor: str | None = None) -> dict:
    if cursor:
        endpoint = DROPBOX_API + "/files/list_folder/continue"
        payload = {"cursor": cursor}
    else:
        endpoint = DROPBOX_API + "/files/list_folder"
        payload = {
            "path": inbox,
            "recursive": False,
            "include_deleted": False,
            "limit": 100,
        }
    data, _ = dropbox_request(
        endpoint,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        timeout=30,
        max_response_bytes=1_000_000,
    )
    try:
        result = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise Blocked("Dropbox returned malformed folder data.") from None
    if (not isinstance(result, dict) or not isinstance(result.get("entries"), list)
            or not isinstance(result.get("cursor"), str) or not result["cursor"]
            or not isinstance(result.get("has_more"), bool)):
        raise Blocked("Dropbox folder response schema was invalid.")
    return result


def dropbox_download(token: str, entry: dict) -> bytes:
    file_id, revision, expected_size = entry.get("id"), entry.get("rev"), entry.get("size")
    if (not isinstance(file_id, str) or not file_id.startswith("id:")
            or not isinstance(revision, str) or not revision
            or type(expected_size) is not int or not 1 <= expected_size <= MAX_COMMENT):
        raise Blocked("Dropbox inbox file metadata was invalid or exceeded the size limit.")
    data, headers = dropbox_request(
        DROPBOX_CONTENT + "/files/download",
        headers={
            "Authorization": "Bearer " + token,
            "Dropbox-API-Arg": json.dumps({"path": file_id}, separators=(",", ":")),
        },
        timeout=30,
        max_response_bytes=MAX_COMMENT,
    )
    metadata_header = next(
        (value for key, value in headers.items() if key.lower() == "dropbox-api-result"),
        None,
    )
    if not metadata_header:
        raise Blocked("Dropbox download omitted its file metadata.")
    try:
        metadata = json.loads(metadata_header)
    except (ValueError, TypeError):
        raise Blocked("Dropbox download metadata was malformed.") from None
    if (not isinstance(metadata, dict) or metadata.get("id") != file_id
            or metadata.get("rev") != revision or metadata.get("size") != expected_size
            or len(data) != expected_size):
        raise Blocked("Dropbox inbox file changed during download; retry on the next poll.")
    return data


def dropbox_source_marker(file_id: str, revision: str) -> str:
    digest = hashlib.sha256((file_id + "\0" + revision).encode("utf-8")).hexdigest()
    return f"<!-- TRNJE_DROPBOX_SOURCE:{digest} -->"


def dropbox_ingest(state: dict, items: list[dict]) -> int:
    """Forward validated Dropbox task files into the existing authenticated GitHub mailbox."""
    if os.environ.get("TRNJE_DROPBOX_ENABLED") != "1":
        return 0

    cfg = dropbox_config()
    token = dropbox_access_token(cfg)
    known = set()
    for comment in items:
        body = comment.get("body") or ""
        known.update(re.findall(r"<!-- TRNJE_DROPBOX_SOURCE:([0-9a-f]{64}) -->", body))

    cursor = state.get("dropbox_cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 16_384):
        raise Blocked("Stored Dropbox cursor is invalid.")

    forwarded = 0
    for page_number in range(DROPBOX_MAX_PAGES_PER_POLL):
        try:
            page = dropbox_list_page(token, cfg["inbox"], cursor)
        except Blocked as exc:
            if cursor and "Dropbox HTTP 409" in str(exc):
                state.pop("dropbox_cursor", None)
                save(state)
                cursor = None
                page = dropbox_list_page(token, cfg["inbox"], None)
            else:
                raise

        for entry in page["entries"]:
            if not isinstance(entry, dict) or entry.get(".tag") != "file":
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.lower().endswith(".json"):
                continue

            file_id, revision = entry.get("id"), entry.get("rev")
            if not isinstance(file_id, str) or not isinstance(revision, str):
                raise Blocked("Dropbox inbox file omitted its stable ID or revision.")
            marker = dropbox_source_marker(file_id, revision)
            digest = marker.removeprefix("<!-- TRNJE_DROPBOX_SOURCE:").removesuffix(" -->")
            if digest in known:
                continue

            raw = dropbox_download(token, entry)
            try:
                body = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise Blocked("Dropbox task file must be UTF-8.") from None
            if "TRNJE_DROPBOX_SOURCE:" in body:
                raise Blocked("Dropbox task file must not contain forwarding metadata.")
            parse(body)
            forwarded_body = body.rstrip("\r\n") + "\n\n" + marker
            if len(forwarded_body.encode("utf-8")) > MAX_COMMENT:
                raise Blocked("Dropbox task plus forwarding metadata exceeds the comment size limit.")
            if not post(forwarded_body):
                raise Blocked("Dropbox task forwarding to GitHub Issue #3 failed.")
            known.add(digest)
            forwarded += 1

        next_cursor = page["cursor"]
        state["dropbox_cursor"] = next_cursor
        save(state)
        if not page["has_more"]:
            break
        cursor = next_cursor

    return forwarded


def recover(s: dict) -> None:
    changed = False
    for key, rec in s["tasks"].items():
        if rec.get("status") == "RUNNING":
            kind = rec.get("kind", "unknown")
            res = {"status": "UNKNOWN", "detail": "Worker restarted during task; not retried. Inspect worktree before a new task.",
                   "exit": None}
            rec.update(status="RESULT_PENDING", task_status="UNKNOWN",
                       body=result_body(int(key), kind, res, {"status": "UNKNOWN"}))
            changed = True
    if changed:
        save(s)


def once(r: Path, s: dict) -> int:
    items = comments()
    deliver(s, items)
    forwarded = 0
    if os.environ.get("TRNJE_DROPBOX_ENABLED") == "1":
        try:
            forwarded = dropbox_ingest(s, items)
            s["last_dropbox_poll_at"] = int(time.time())
            s["last_dropbox_poll_status"] = "PASS"
            s.pop("last_dropbox_poll_error", None)
        except Blocked as exc:
            # Dropbox is an optional input channel; its outage must not block GitHub Issue #3.
            s["last_dropbox_poll_at"] = int(time.time())
            s["last_dropbox_poll_status"] = "BLOCKED"
            s["last_dropbox_poll_error"] = clean(str(exc))[:300]
        save(s)
    n = 0
    for c in items:
        if c["id"] <= int(s.get("last_seen", 0)):
            continue
        process(c, s, r)
        deliver(s, items)
        s["last_seen"] = c["id"]
        save(s)
        n += 1
    s["last_poll_at"] = int(time.time())
    s["last_poll_status"] = "PASS"
    s.pop("last_poll_error", None)
    save(s)
    return n + forwarded




def validate_mcp_task(kind: str, payload: dict) -> tuple[str, dict]:
    """Apply exactly the same allowlist/schema checks to MCP and mailbox tasks."""
    if not isinstance(kind, str) or not isinstance(payload, dict):
        raise ValueError("Task kind and payload must be valid.")
    try:
        body = MARK + "\n" + json.dumps(
            {"kind": kind, "payload": payload}, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        )
        return parse(body)
    except (Blocked, TypeError, ValueError):
        raise ValueError("Task refused by the existing TRNJE allowlist/schema.") from None

def write_mcp_evidence(task: dict, result: dict) -> tuple[str, str]:
    """Persist a redacted, fsynced result record and return its path and digest."""
    dirs()
    LOGS.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        LOGS.chmod(0o700)
    except OSError:
        pass
    record = {
        "task_id": task["task_id"],
        "kind": task["kind"],
        "status": result.get("status"),
        "detail": clean(str(result.get("detail", "")))[:400],
        "exit_code": result.get("exit"),
        "output": clean(str(result.get("output", ""))),
        "task_report": result.get("task_report"),
    }
    body = "# TRNJE direct MCP task\n\n~~~json\n"
    body += json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2)
    body += "\n~~~\n"
    path = LOGS / ("mcp-" + task["task_id"] + ".md")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return str(path), digest


def process_mcp_queue(queue: TaskQueue, workspace: Path) -> bool:
    """Execute at most one queued API task on the existing worker thread."""
    task = queue.claim_next()
    if task is None:
        return False
    try:
        result = execute(workspace, task["kind"], task["payload"])
    except Blocked as exc:
        result = {"status": "BLOCKED", "detail": clean(str(exc)), "exit": None, "output": ""}
    except Exception as exc:
        result = {
            "status": "FAIL",
            "detail": "Worker task failed (" + type(exc).__name__ + ").",
            "exit": None,
            "output": "",
        }

    if result.get("status") not in {"PASS", "FAIL", "BLOCKED", "UNKNOWN"}:
        result = {
            "status": "UNKNOWN",
            "detail": "Executor returned an unrecognized status; no success is claimed.",
            "exit": result.get("exit"),
            "output": clean(str(result.get("output", ""))),
        }
    try:
        evidence_path, evidence_sha256 = write_mcp_evidence(task, result)
    except Exception as exc:
        result = {
            "status": "UNKNOWN",
            "detail": "Task execution finished, but durable evidence could not be written ("
                      + type(exc).__name__ + ").",
            "exit": result.get("exit"),
            "output": clean(str(result.get("output", ""))),
        }
        evidence_path, evidence_sha256 = "", ""
    try:
        queue.complete(
            task["task_id"], result,
            evidence_path=evidence_path,
            evidence_sha256=evidence_sha256,
        )
        print(
            "MCP task " + task["task_id"] + ": " + result["status"],
            flush=True,
        )
    except Exception as exc:
        # The task remains RUNNING. Startup recovery will mark it UNKNOWN, never replay it.
        print(
            "BLOCKED: MCP result persistence failed (" + type(exc).__name__ + ").",
            file=sys.stderr, flush=True,
        )
    return True

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll-seconds", type=int, default=15)
    a = ap.parse_args()
    if not 5 <= a.poll_seconds <= 120:
        ap.error("--poll-seconds must be between 5 and 120")
    dirs()
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("BLOCKED: another Trnje worker holds the lock.", file=sys.stderr)
        os.close(fd)
        return 2

    mcp_server = None
    mcp_thread = None
    try:
        r = root()
        s = load()
        recover(s)
        wake = threading.Event()
        mcp_queue = None

        if not a.once and os.environ.get("TRNJE_MCP_ENABLED") == "1":
            try:
                mcp_queue = TaskQueue(STATE / "mcp-tasks.sqlite3")
                recovered = mcp_queue.recover_interrupted()
                mcp_server = create_mcp_server(
                    mcp_queue, validate_mcp_task, wake.set
                )
                mcp_thread = threading.Thread(
                    target=mcp_server.serve_forever,
                    name="trnje-mcp-http",
                    daemon=True,
                )
                mcp_thread.start()
                print(
                    "TRNJE MCP gateway listening on 127.0.0.1:"
                    + str(mcp_server.server_address[1])
                    + "; recovered_as_unknown=" + str(recovered),
                    flush=True,
                )
            except Exception as exc:
                # MCP is optional: a bad OAuth/port config must not stop the existing mailbox worker.
                if mcp_server is not None:
                    mcp_server.server_close()
                mcp_queue = None
                mcp_server = None
                mcp_thread = None
                print(
                    "BLOCKED: MCP gateway not started (" + type(exc).__name__
                    + "); GitHub/Dropbox worker remains available.",
                    file=sys.stderr, flush=True,
                )

        try:
            login = verify_gh()
            github_ready = True
        except Blocked as exc:
            if a.once:
                raise
            login = "unavailable"
            github_ready = False
            print(
                "BLOCKED: GitHub mailbox is unavailable at startup ("
                + clean(str(exc))[:200] + "); direct MCP tasks may still run.",
                file=sys.stderr, flush=True,
            )

        if a.once:
            n = once(r, s)
            if n:
                print(f"Scanned {n} new mailbox items (GitHub and Dropbox forwards).", flush=True)
            return 0

        print(
            "Trnje worker ready: " + login + "; Issue #" + str(ISSUE)
            + "; workspace=$HOME/trnje-e2e-aRhnEF/repo",
            flush=True,
        )
        next_poll = time.monotonic()
        while True:
            if mcp_queue is not None:
                # Clear before claiming: an enqueue during the claim or execution
                # leaves the event set and causes the next pass to run immediately.
                wake.clear()
                if process_mcp_queue(mcp_queue, r):
                    continue

            now = time.monotonic()
            if now >= next_poll:
                try:
                    if not github_ready:
                        login = verify_gh()
                        github_ready = True
                        print("PASS: GitHub mailbox connection restored.", flush=True)
                    n = once(r, s)
                    if n:
                        print(
                            f"Scanned {n} new mailbox items (GitHub and Dropbox forwards).",
                            flush=True,
                        )
                except Blocked as exc:
                    github_ready = False
                    s["last_poll_at"] = int(time.time())
                    s["last_poll_status"] = "BLOCKED"
                    s["last_poll_error"] = clean(str(exc))[:300]
                    save(s)
                    print(f"BLOCKED: GitHub/Dropbox poll: {clean(str(exc))[:200]}", file=sys.stderr, flush=True)
                next_poll = time.monotonic() + a.poll_seconds
                continue

            wait_seconds = max(0.0, next_poll - time.monotonic())
            if mcp_queue is None:
                time.sleep(wait_seconds)
            else:
                wake.wait(wait_seconds)
    except KeyboardInterrupt:
        return 130
    except Blocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    finally:
        if mcp_server is not None:
            if mcp_thread is not None and mcp_thread.is_alive():
                try:
                    mcp_server.shutdown()
                except Exception:
                    pass
            mcp_server.server_close()
        if mcp_thread is not None:
            mcp_thread.join(timeout=2)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

if __name__ == "__main__":
    raise SystemExit(main())
