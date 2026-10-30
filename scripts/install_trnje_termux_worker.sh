#!/usr/bin/env bash
# Installs only the standalone worker; does not modify either repository.
set -eu
umask 077

fail() {
  printf 'BLOCKED: %s\n' "$1" >&2
  exit 2
}

command -v curl >/dev/null 2>&1 || fail "curl is required."
command -v python >/dev/null 2>&1 || fail "python is required."
command -v gh >/dev/null 2>&1 || fail "GitHub CLI gh is required."
command -v install >/dev/null 2>&1 || fail "install utility is required."

repo_root="$HOME/trnje-e2e-aRhnEF/repo"
[ -d "$repo_root/.git" ] || fail "Configured Trnje checkout is missing."
publisher="$HOME/bin/trnje-publish"
[ -x "$publisher" ] || fail '$HOME/bin/trnje-publish is missing or not executable.'

destination="$HOME/bin/trnje-worker"
[ ! -e "$destination" ] || fail "Refusing to overwrite existing $destination."

login="$(gh api user --jq .login 2>/dev/null || true)"
case "$login" in
  xiiitore|ranub-xiii) ;;
  *) fail "GitHub authentication is missing or uses a non-allowlisted account." ;;
esac

issue_number="$(gh api repos/ranub-xiii/trnje/issues/3 --jq .number 2>/dev/null || true)"
[ "$issue_number" = "3" ] || fail "Dedicated Trnje mailbox Issue #3 is not accessible."

url="https://raw.githubusercontent.com/ranub-xiii/trnje/feat/trnje-termux-worker/scripts/trnje_termux_worker.py"
expected_sha256="bec960c1f8e3bf2ff2dee61eab198a75b94d4da0c37a4c0d47e0cb3ba5a55984"
tmpdir="$(printenv TMPDIR 2>/dev/null || true)"
[ -n "$tmpdir" ] || tmpdir="/tmp"
tmp="$(mktemp "$tmpdir/trnje-worker.XXXXXX")"
trap 'rm -f "$tmp"' EXIT HUP INT TERM

curl -fsSL --connect-timeout 10 --max-time 60 "$url" -o "$tmp" || fail "Could not fetch the pinned Trnje worker."
actual_sha256="$(python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$tmp")"
[ "$actual_sha256" = "$expected_sha256" ] || fail "Worker SHA-256 does not match the installer pin."

python - "$tmp" <<'PY'
import pathlib
import sys
source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
compile(source, sys.argv[1], "exec")
PY

mkdir -p "$HOME/bin"
install -m 700 "$tmp" "$destination"
printf 'PASS: installed independent Trnje worker at %s\n' "$destination"
printf 'No Trnje or Emberlane repository files were changed.\n'
printf 'Start: %s --poll-seconds 15\n' "$destination"
