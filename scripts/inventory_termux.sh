#!/usr/bin/env bash
# Read-only environment inventory for Trnje. Creates one sanitized Markdown report.
set -u
umask 077

stamp="$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || printf 'unknown-time')"
out="${TRNJE_INVENTORY_OUT:-./trnje-inventory-${stamp}.md}"
if [ -e "$out" ]; then
  printf 'BLOCKED: report path already exists; refusing to overwrite: %s\n' "$out" >&2
  exit 2
fi
if ! ( : > "$out" ) 2>/dev/null; then
  printf 'BLOCKED: cannot create report at requested output path.\n' >&2
  exit 2
fi
chmod 600 "$out" 2>/dev/null || true
exec 3>"$out"

redact() {
  sed -e "s|$HOME|\$HOME|g" -e "s|$PREFIX|\$PREFIX|g" 2>/dev/null
}
section() { printf '\n## %s\n' "$1" >&3; }
kv() { printf -- '- **%s:** %s\n' "$1" "$2" >&3; }
has() { command -v "$1" >/dev/null 2>&1; }
version() {
  label="$1"; shift
  if has "$1"; then
    value="$("$@" 2>&1 | head -n 2 | redact | tr '\n' ' ' | cut -c1-220)"
    [ -n "$value" ] || value="installed; version output unavailable"
    kv "$label" "$value"
  else
    kv "$label" "NOT FOUND"
  fi
}

printf '# Trnje environment inventory\n' >&3
kv "Captured at (UTC)" "$stamp"
kv "Report scope" "Read-only checks; no installs, network calls, Git writes, or model inference"
kv "Working directory label" "$(basename "$PWD" 2>/dev/null || printf unknown)"
kv "Shell" "${BASH_VERSION:-unknown}"

section "Platform and resources"
version "uname" uname -a
if has getprop; then
  kv "Android API level" "$(getprop ro.build.version.sdk 2>/dev/null | head -n1)"
  kv "Android release" "$(getprop ro.build.version.release 2>/dev/null | head -n1)"
else
  kv "Android properties" "getprop NOT FOUND"
fi
if [ -r /proc/meminfo ]; then
  awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {printf "- **%s:** %s %s\n", $1, $2, $3}' /proc/meminfo >&3
else
  kv "Memory" "UNKNOWN: /proc/meminfo unavailable"
fi
if has df; then
  df -Pk . 2>/dev/null | awk 'NR==2 {printf "- **Workspace filesystem (KiB):** total=%s used=%s available=%s\n", $2,$3,$4}' >&3
else
  kv "Disk usage" "df NOT FOUND"
fi

section "Tool availability and versions"
version "Git" git --version
version "Python" python --version
version "Python 3" python3 --version
version "pip" pip --version
version "Node.js" node --version
version "npm" npm --version
version "Bash" bash --version
version "tmux" tmux -V
version "proot" proot --version
version "Clang" clang --version
version "GCC" gcc --version
version "Make" make --version
version "CMake" cmake --version
version "GitHub CLI" gh --version
version "pytest" pytest --version
version "Ruff" ruff --version
version "coverage" coverage --version
version "Ollama" ollama --version
version "llama.cpp CLI" llama-cli --version
version "llama.cpp server" llama-server --version
version "llamafile" llamafile --version
version "MLC" mlc --version
version "Docker" docker --version
version "Podman" podman --version
version "curl" curl --version
version "jq" jq --version

section "Model-file presence (names and paths intentionally omitted)"
model_dirs=0
model_files=0
model_bytes=0
for d in "$HOME/models" "$HOME/llama.cpp/models" "$HOME/.ollama/models" "$PREFIX/share"; do
  if [ -d "$d" ]; then
    model_dirs=$((model_dirs + 1))
    while IFS= read -r -d '' f; do
      case "$f" in
        *.gguf|*.ggml|*.safetensors|*.bin|*.onnx|*.pt|*.pth|*.model)
          model_files=$((model_files + 1))
          size="$(wc -c < "$f" 2>/dev/null || printf 0)"
          case "$size" in ''|*[!0-9]*) size=0 ;; esac
          model_bytes=$((model_bytes + size))
          ;;
      esac
    done < <(find "$d" -maxdepth 3 -type f \( -name '*.gguf' -o -name '*.ggml' -o -name '*.safetensors' -o -name '*.bin' -o -name '*.onnx' -o -name '*.pt' -o -name '*.pth' -o -name '*.model' \) -print0 2>/dev/null)
  fi
done
kv "Inspected conventional model directories found" "$model_dirs"
kv "Model-like files found (count only)" "$model_files"
kv "Model-like file size total (bytes)" "$model_bytes"
kv "Inference smoke test" "NOT RUN; inventory does not load or execute model files"

section "Workspace and subprocess smoke checks"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  kv "Git worktree detected" "YES"
  status="$(git status --porcelain=v1 2>/dev/null || true)"
  modified="$(printf '%s\n' "$status" | awk 'NF && substr($0,1,2)!="??" {n++} END{print n+0}')"
  untracked="$(printf '%s\n' "$status" | awk 'substr($0,1,2)=="??" {n++} END{print n+0}')"
  kv "Existing tracked changes (count only)" "$modified"
  kv "Existing untracked paths (count only)" "$untracked"
else
  kv "Git worktree detected" "NO"
fi
if ( sh -c 'exit 0' ) >/dev/null 2>&1; then
  kv "Subprocess launch smoke test" "PASS (sh -c exit 0)"
else
  kv "Subprocess launch smoke test" "FAIL (sh -c exit 0)"
fi
tmpdir="$(mktemp -d 2>/dev/null || true)"
if [ -n "$tmpdir" ] && [ -d "$tmpdir" ]; then
  if printf 'trnje-inventory-check\n' > "$tmpdir/check" &&
     [ "$(cat "$tmpdir/check" 2>/dev/null)" = "trnje-inventory-check" ]; then
    kv "Temporary write/read smoke test" "PASS"
  else
    kv "Temporary write/read smoke test" "FAIL"
  fi
  rm -f "$tmpdir/check" 2>/dev/null || true
  rmdir "$tmpdir" 2>/dev/null || true
else
  kv "Temporary write/read smoke test" "BLOCKED: mktemp unavailable"
fi

section "Explicitly not tested by this inventory"
printf '%s\n' \
  '- GitHub authentication scopes, repository write permission, Actions availability, or provider quota (no authenticated requests made).' \
  '- Actual model inference, model quality, context limit, latency, or memory peak.' \
  '- Android screen-off survival, process suspension, battery/thermal behavior, and interruption recovery.' \
  '- Agent/MCP-specific subprocess permissions; this script tests only the shell route that launched it.' \
  '- Any remote runner or hybrid benchmark.' >&3

section "Interpretation"
printf '%s\n' \
  '- This report is an inventory, not a benchmark or a pass for Trnje acceptance tests.' \
  '- Review the report before sharing it; although values and file names are omitted, platform/tool metadata may still be identifying.' \
  '- No existing project files were edited by this script. It creates only this report and a temporary smoke-test file that it removes.' >&3

exec 3>&-
printf 'Inventory report created: %s\n' "$out" >&2
printf 'Report permissions: '
ls -l "$out" 2>/dev/null | awk '{print $1}'
