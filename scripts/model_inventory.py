#!/usr/bin/env python3
"""Read-only, bounded inventory of local GGUF models for Trnje on Termux."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

HOME = Path.home()
STATE = HOME / ".local/state/trnje-worker"
LOGS = STATE / "logs"
ROOTS = (
    HOME / "models",
    HOME / ".cache/llama.cpp",
    HOME / ".local/share/llama.cpp/models",
    HOME / "llama.cpp/models",
    HOME / "storage/shared/Download",
    HOME / "storage/shared/Models",
)
MAX_DEPTH = 4
MAX_DIRS = 500
MAX_ENTRIES = 5000
MAX_CANDIDATES = 20
MAX_FILE_BYTES = 6 * 1024**3
MAX_TOTAL_HASH_BYTES = 6 * 1024**3
SKIP_DIRS = {".git", ".ssh", ".config", "node_modules", "__pycache__", "Android", "data"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def inventory() -> dict:
    started = time.monotonic()
    candidates: list[Path] = []
    errors: list[str] = []
    seen_dirs = 0
    seen_entries = 0

    for root in ROOTS:
        if not root.exists() or not root.is_dir():
            continue
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            errors.append(f"root_unavailable:{root}")
            continue
        stack = [(resolved_root, 0)]
        while stack and seen_dirs < MAX_DIRS and seen_entries < MAX_ENTRIES:
            current, depth = stack.pop()
            seen_dirs += 1
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        seen_entries += 1
                        if seen_entries > MAX_ENTRIES:
                            break
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_file(follow_symlinks=False):
                                if entry.name.lower().endswith(".gguf"):
                                    candidates.append(Path(entry.path))
                                    if len(candidates) >= MAX_CANDIDATES:
                                        break
                            elif (entry.is_dir(follow_symlinks=False) and depth < MAX_DEPTH
                                  and entry.name not in SKIP_DIRS):
                                stack.append((Path(entry.path), depth + 1))
                        except OSError:
                            continue
                if len(candidates) >= MAX_CANDIDATES:
                    break
            except OSError:
                errors.append(f"directory_unreadable:{current}")
        if len(candidates) >= MAX_CANDIDATES or seen_dirs >= MAX_DIRS or seen_entries >= MAX_ENTRIES:
            break

    models = []
    total_hashed = 0
    for path in candidates:
        try:
            size = path.stat().st_size
            item = {"path": str(path), "size_bytes": size, "sha256": None, "hash_status": "SKIPPED"}
            if size > MAX_FILE_BYTES:
                item["hash_status"] = "SKIPPED_FILE_TOO_LARGE"
            elif total_hashed + size > MAX_TOTAL_HASH_BYTES:
                item["hash_status"] = "SKIPPED_TOTAL_HASH_LIMIT"
            else:
                item["sha256"] = sha256_file(path)
                item["hash_status"] = "PASS"
                total_hashed += size
            models.append(item)
        except OSError as exc:
            models.append({"path": str(path), "hash_status": "FAIL", "error": type(exc).__name__})

    llama = shutil.which(os.environ.get("TRNJE_LLAMA_CLI", "llama-cli"))
    version = None
    if llama:
        try:
            proc = subprocess.run([llama, "--version"], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=10,
                                  check=False, shell=False)
            version = {"status": "PASS" if proc.returncode == 0 else "FAIL",
                       "returncode": proc.returncode,
                       "output": ((proc.stdout or "") + (proc.stderr or "")).strip()[:500]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            version = {"status": "BLOCKED", "error": type(exc).__name__}

    usage = shutil.disk_usage(HOME)
    meminfo = Path("/proc/meminfo")
    memory = {}
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(("MemTotal:", "MemAvailable:")):
                key, value = line.split(":", 1)
                memory[key] = value.strip()

    return {
        "status": "PASS" if not errors else "PASS_WITH_WARNINGS",
        "scope": [str(p) for p in ROOTS],
        "limits": {"max_depth": MAX_DEPTH, "max_directories": MAX_DIRS,
                   "max_entries": MAX_ENTRIES, "max_candidates": MAX_CANDIDATES,
                   "max_file_hash_bytes": MAX_FILE_BYTES,
                   "max_total_hash_bytes": MAX_TOTAL_HASH_BYTES},
        "directories_scanned": seen_dirs,
        "entries_scanned": seen_entries,
        "models": models,
        "model_count": len(models),
        "llama_cli": {"available": bool(llama), "path": llama, "version": version},
        "model_path_configured": bool(os.environ.get("TRNJE_MODEL_PATH")),
        "storage": {"total_bytes": usage.total, "free_bytes": usage.free},
        "memory": memory,
        "warnings": errors,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "read_only": True,
    }


def main() -> int:
    result = inventory()
    LOGS.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = LOGS / f"model-inventory-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{time.time_ns()}.md"
    with report.open("x", encoding="utf-8") as stream:
        stream.write("# Trnje local model inventory\n\n~~~json\n"
                     + json.dumps(result, ensure_ascii=False, indent=2)
                     + "\n~~~\n")
    try:
        report.chmod(0o600)
    except OSError:
        pass
    print(json.dumps({"status": result["status"], "report": str(report),
                      "model_count": result["model_count"]}, ensure_ascii=False))
    gh = shutil.which("gh")
    if not gh:
        print("BLOCKED: GitHub CLI gh is unavailable; local report was preserved.")
        return 2
    try:
        login = subprocess.run([gh, "api", "user", "--jq", ".login"],
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=20, check=False, shell=False)
        if login.returncode != 0 or login.stdout.strip() not in {"ranub-xiii", "xiiitore"}:
            print("BLOCKED: GitHub authentication is missing or not allowlisted; local report was preserved.")
            return 2
        issue = subprocess.run([gh, "api", "repos/ranub-xiii/trnje/issues/3", "--jq", ".number"],
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=20, check=False, shell=False)
        if issue.returncode != 0 or issue.stdout.strip() != "3":
            print("BLOCKED: dedicated Trnje Issue #3 is not accessible; local report was preserved.")
            return 2
        body = "TRNJE_MODEL_INVENTORY_JSON\n" + json.dumps(result, ensure_ascii=False, sort_keys=True)
        if len(body.encode("utf-8")) > 16_000:
            print("BLOCKED: inventory report exceeds the safe GitHub comment limit; local report was preserved.")
            return 2
        posted = subprocess.run(
            [gh, "api", "repos/ranub-xiii/trnje/issues/3/comments", "--method", "POST", "-f", f"body={body}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30, check=False, shell=False)
        if posted.returncode != 0:
            print("BLOCKED: GitHub result publication failed; local report was preserved.")
            return 2
        confirmation = json.loads(posted.stdout)
        if not isinstance(confirmation.get("id"), int):
            print("BLOCKED: GitHub did not confirm the result comment; local report was preserved.")
            return 2
        print(json.dumps({"status": "PASS", "issue_comment_id": confirmation["id"],
                          "issue_url": confirmation.get("html_url"), "local_report": str(report)},
                         ensure_ascii=False))
        return 0
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"BLOCKED: publication failed ({type(exc).__name__}); local report was preserved.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
