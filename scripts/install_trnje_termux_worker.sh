#!/usr/bin/env bash
# Installs only the standalone worker; does not modify either repository.
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

destination="$HOME/bin/trnje-worker"
[ ! -L "$destination" ] || fail "Refusing to replace a symlink at $destination."
if [ "$mode" = "install" ]; then
  [ ! -e "$destination" ] || fail "Refusing to overwrite existing $destination; use --update."
else
  [ -f "$destination" ] || fail "--update requires an existing regular worker file."
fi

login="$(gh api user --jq .login 2>/dev/null || true)"
case "$login" in
  xiiitore|ranub-xiii) ;;
  *) fail "GitHub authentication is missing or uses a non-allowlisted account." ;;
esac

issue_number="$(gh api repos/ranub-xiii/trnje/issues/3 --jq .number 2>/dev/null || true)"
[ "$issue_number" = "3" ] || fail "Dedicated Trnje mailbox Issue #3 is not accessible."

tmpdir="$(printenv TMPDIR 2>/dev/null || true)"
[ -n "$tmpdir" ] || tmpdir="/tmp"
tmp="$(mktemp "$tmpdir/trnje-worker.XXXXXX")"
staged=""
cleanup() {
  rm -f "$tmp"
  [ -z "$staged" ] || rm -f "$staged"
}
trap cleanup EXIT HUP INT TERM

# The repository is private: fetch through the authenticated GitHub CLI API.
gh api "repos/ranub-xiii/trnje/contents/scripts/trnje_termux_worker.py?ref=feat/trnje-termux-worker" --jq .content |
  python -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' > "$tmp"

expected_sha256="538c7a5781de4541ed448a95f04e1e0eb66bc32feeca08939046ef97fb9baf0c"
actual_sha256="$(python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$tmp")"
[ "$actual_sha256" = "$expected_sha256" ] || fail "Worker SHA-256 does not match the installer pin."

python - "$tmp" <<'PY'
import pathlib
import sys
source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
compile(source, sys.argv[1], "exec")
PY

mkdir -p "$HOME/bin"
if [ "$mode" = "--update" ]; then
  backup_dir="$HOME/.local/state/trnje-worker/backups"
  mkdir -p "$backup_dir"
  chmod 700 "$backup_dir"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup="$backup_dir/trnje-worker.$stamp.bak"
  [ ! -e "$backup" ] || fail "Backup already exists; refusing to overwrite it."
  cp -p "$destination" "$backup"
  staged="$HOME/bin/.trnje-worker.new.$$"
  [ ! -e "$staged" ] || fail "Temporary install path already exists."
  install -m 700 "$tmp" "$staged"
  mv "$staged" "$destination"
  staged=""
  printf 'PASS: updated independent Trnje worker at %s\n' "$destination"
  printf 'BACKUP: %s\n' "$backup"
else
  install -m 700 "$tmp" "$destination"
  printf 'PASS: installed independent Trnje worker at %s\n' "$destination"
fi
printf 'No Trnje or Emberlane repository files were changed.\n'
printf 'Restart the existing tmux pane after update to load the new code.\n'
