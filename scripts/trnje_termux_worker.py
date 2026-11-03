#!/usr/bin/env python3
"""Standalone Trnje-only GitHub Issues worker. No Emberlane imports or shared state."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
        return {"status": "BLOCKED", "exit": None, "output": "", "detail": f"timeout after {timeout}s"}
    except OSError as exc:
        return {"status": "BLOCKED", "exit": None, "output": "", "detail": type(exc).__name__}
    out = clean((p.stdout or "") + (p.stderr or ""))
    return {"status": "PASS" if p.returncode == 0 else "FAIL",
            "exit": p.returncode, "output": out,
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


def gh(args: list[str], timeout: int = 30) -> str:
    try:
        p = subprocess.run(["gh", "api", *args], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout, check=False, shell=False, env=env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked(f"GitHub API request failed ({type(exc).__name__}).")
    if p.returncode:
        raise Blocked("GitHub API request failed: " + clean(p.stderr or p.stdout)[:400])
    return p.stdout


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
    try:
        obj = json.loads(body[len(MARK) + 1:], object_pairs_hook=pairs_no_duplicates)
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
               "--task", p["task"], "--expect", p["expect"], "--model-timeout", "120", "--check-timeout", "180"]
        x = call(cmd, r, 600)
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
    n = 0
    for c in items:
        if c["id"] <= int(s.get("last_seen", 0)):
            continue
        process(c, s, r)
        deliver(s, items)
        s["last_seen"] = c["id"]
        save(s)
        n += 1
    return n


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
    try:
        r = root()
        login = verify_gh()
        s = load()
        recover(s)
        print(f"Trnje worker ready: {login}; Issue #{ISSUE}; workspace=$HOME/trnje-e2e-aRhnEF/repo", flush=True)
        while True:
            try:
                n = once(r, s)
                if n:
                    print(f"Scanned {n} new mailbox comments.", flush=True)
            except Blocked as exc:
                print(f"BLOCKED: {exc}", file=sys.stderr, flush=True)
            if a.once:
                return 0
            time.sleep(a.poll_seconds)
    except KeyboardInterrupt:
        return 130
    except Blocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
