#!/usr/bin/env bash
# Installs the standalone TRNJE worker and its two stdlib-only MCP modules.
# The worker is replaced last; a running worker is never restarted here.
set -euo pipefail
umask 077

fail() {
  printf 'BLOCKED: %s\n' "$1" >&2
  exit 2
}

mode="${1:-install}"
[ "$#" -le 1 ] || fail "Use either no argument or --update."
case "$mode" in
  install|--update) ;;
  *) fail "Unknown mode. Use --update to replace an existing worker safely." ;;
esac

command -v python >/dev/null 2>&1 || fail "python is required."
command -v gh >/dev/null 2>&1 || fail "GitHub CLI gh is required."
command -v install >/dev/null 2>&1 || fail "install utility is required."

repo_root="$HOME/trnje-e2e-aRhnEF/repo"
[ -d "$repo_root/.git" ] || fail "Configured Trnje checkout is missing."
publisher="$HOME/bin/trnje-publish"
[ -x "$publisher" ] || fail '$HOME/bin/trnje-publish is missing or not executable.'

bin_dir="$HOME/bin"
[ ! -L "$bin_dir" ] || fail "Refusing to use a symlink as $bin_dir."
mkdir -p "$bin_dir"

worker_destination="$bin_dir/trnje-worker"
queue_destination="$bin_dir/trnje_api_queue.py"
mcp_destination="$bin_dir/trnje_mcp_http.py"
destinations=("$worker_destination" "$queue_destination" "$mcp_destination")

for destination in "${destinations[@]}"; do
  [ ! -L "$destination" ] || fail "Refusing to replace symlink: $destination"
  if [ -e "$destination" ] && [ ! -f "$destination" ]; then
    fail "Install target is not a regular file: $destination"
  fi
done

if [ "$mode" = "install" ]; then
  for destination in "${destinations[@]}"; do
    [ ! -e "$destination" ] || fail "Refusing to overwrite existing $destination; use --update."
  done
else
  [ -f "$worker_destination" ] || fail "--update requires an existing regular worker file."
fi

login="$(gh api user --jq .login 2>/dev/null || true)"
case "$login" in
  xiiitore|ranub-xiii) ;;
  *) fail "GitHub authentication is missing or uses a non-allowlisted account." ;;
esac

issue_number="$(gh api repos/ranub-xiii/trnje/issues/3 --jq .number 2>/dev/null || true)"
[ "$issue_number" = "3" ] || fail "Dedicated Trnje mailbox Issue #3 is not accessible."

worker_ref="${TRNJE_WORKER_REF:-main}"
case "$worker_ref" in
  ""|*[!A-Za-z0-9._/-]*|*..*) fail "TRNJE_WORKER_REF contains unsupported characters." ;;
esac

tmpdir="$(printenv TMPDIR 2>/dev/null || true)"
[ -n "$tmpdir" ] || tmpdir="/tmp"
worker_tmp="$(mktemp "$tmpdir/trnje-worker.XXXXXX")"
queue_tmp="$(mktemp "$tmpdir/trnje-queue.XXXXXX")"
mcp_tmp="$(mktemp "$tmpdir/trnje-mcp.XXXXXX")"
worker_staged=""
queue_staged=""
mcp_staged=""
cleanup() {
  rm -f "$worker_tmp" "$queue_tmp" "$mcp_tmp"
  [ -z "$worker_staged" ] || rm -f "$worker_staged"
  [ -z "$queue_staged" ] || rm -f "$queue_staged"
  [ -z "$mcp_staged" ] || rm -f "$mcp_staged"
}
trap cleanup EXIT HUP INT TERM

fetch_python_file() {
  local path="$1"
  local destination="$2"
  gh api "repos/ranub-xiii/trnje/contents/${path}?ref=${worker_ref}" --jq .content |
    python -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' > "$destination"
}

# Fetch all three files from exactly the same immutable branch ref.
fetch_python_file "scripts/trnje_termux_worker.py" "$worker_tmp"
fetch_python_file "scripts/trnje_api_queue.py" "$queue_tmp"
fetch_python_file "scripts/trnje_mcp_http.py" "$mcp_tmp"

expected_worker_sha256="fb1f9a49d7a78a3101eeeceb5ef53ec706e8ddce12aa5fe07b2e61f925491045"
expected_queue_sha256="c3ff841837686b61b5638ae118c9d5cab815a9b542c437a474ee8da97b6582de"
expected_mcp_sha256="7e963c99d4110e3bc308a1d5535e176627343900e41f67d07c4d314b1742cbb2"
actual_worker_sha256="$(python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$worker_tmp")"
actual_queue_sha256="$(python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$queue_tmp")"
actual_mcp_sha256="$(python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$mcp_tmp")"
[ "$actual_worker_sha256" = "$expected_worker_sha256" ] || fail "Worker SHA-256 does not match the installer pin."
[ "$actual_queue_sha256" = "$expected_queue_sha256" ] || fail "Queue module SHA-256 does not match the installer pin."
[ "$actual_mcp_sha256" = "$expected_mcp_sha256" ] || fail "MCP module SHA-256 does not match the installer pin."

python - "$worker_tmp" "$queue_tmp" "$mcp_tmp" <<'PY'
import pathlib
import sys
for name in sys.argv[1:]:
    source = pathlib.Path(name).read_text(encoding="utf-8")
    compile(source, name, "exec")
PY

if [ "$mode" = "--update" ]; then
  backup_dir="$HOME/.local/state/trnje-worker/backups"
  mkdir -p "$backup_dir"
  chmod 700 "$backup_dir"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  for destination in "${destinations[@]}"; do
    if [ -f "$destination" ]; then
      backup="$backup_dir/$(basename "$destination").$stamp.bak"
      [ ! -e "$backup" ] || fail "Backup already exists; refusing to overwrite it: $backup"
      cp -p "$destination" "$backup"
      printf 'BACKUP: %s\n' "$backup"
    fi
  done
fi

queue_staged="$bin_dir/.trnje_api_queue.new.$$"
mcp_staged="$bin_dir/.trnje_mcp_http.new.$$"
worker_staged="$bin_dir/.trnje-worker.new.$$"
for staged in "$queue_staged" "$mcp_staged" "$worker_staged"; do
  [ ! -e "$staged" ] && [ ! -L "$staged" ] || fail "Temporary install path already exists: $staged"
done

install -m 600 "$queue_tmp" "$queue_staged"
install -m 600 "$mcp_tmp" "$mcp_staged"
install -m 700 "$worker_tmp" "$worker_staged"

# Install dependencies first and the importing worker last.
mv "$queue_staged" "$queue_destination"
queue_staged=""
mv "$mcp_staged" "$mcp_destination"
mcp_staged=""
mv "$worker_staged" "$worker_destination"
worker_staged=""

printf 'PASS: installed TRNJE worker and MCP modules from %s\n' "$worker_ref"
printf 'WORKER_SHA256: %s\n' "$actual_worker_sha256"
printf 'QUEUE_SHA256: %s\n' "$actual_queue_sha256"
printf 'MCP_SHA256: %s\n' "$actual_mcp_sha256"
printf 'No repository, task state, worker lock, or running process was changed.\n'
printf 'The existing worker is not restarted automatically.\n'
