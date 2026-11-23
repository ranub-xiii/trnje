# Trnje environment inventory
- **Captured at (UTC):** 20261029T121707Z
- **Report scope:** Read-only checks; no installs, network calls, Git writes, or model inference
- **Working directory label:** trnje
- **Shell:** 5.3.9(1)-release

## Platform and resources
- **uname:** Linux localhost 4.19.191-ge6429b82e599 #1 SMP PREEMPT Fri Aug 14 12:26:31 UTC 2026 aarch64 Android 
- **Android API level:** 34
- **Android release:** 14
- **MemTotal::** 5668324 kB
- **MemAvailable::** 1772408 kB
- **SwapTotal::** 4194300 kB
- **SwapFree::** 2141592 kB
- **Workspace filesystem (KiB):** total=111384544 used=78924204 available=32243380

## Tool availability and versions
- **Git:** git version 2.54.0 
- **Python:** Python 3.13.13 
- **Python 3:** Python 3.13.13 
- **pip:** pip 26.1.2 from $PREFIX/lib/python3.13/site-packages/pip (python 3.13) 
- **Node.js:** v24.17.0 
- **npm:** 11.17.0 
- **Bash:** GNU bash, version 5.3.9(1)-release (aarch64-unknown-linux-android) Copyright (C) 2025 Free Software Foundation, Inc. 
- **tmux:** tmux 3.6b 
- **proot:**  _____ _____              ___ |  __ \  __ \_____  _____|   |_ 
- **Clang:** clang version 21.1.8 Target: aarch64-unknown-linux-android30 
- **GCC:** clang version 21.1.8 Target: aarch64-unknown-linux-android30 
- **Make:** GNU Make 4.4.1 Built for aarch64-unknown-linux-android 
- **CMake:** cmake version 4.3.4  
- **GitHub CLI:** gh version 2.95.0 https://github.com/cli/cli/releases/tag/v2.95.0 
- **pytest:** pytest 9.1.1 
- **Ruff:** ruff 0.15.19 
- **coverage:** Coverage.py, version 7.16.2 without C extension Full documentation is at https://coverage.readthedocs.io/en/7.16.2 
- **Ollama:** NOT FOUND
- **llama.cpp CLI:** version: 0 (unknown) built with Clang 21.0.0 for Android aarch64 
- **llama.cpp server:** version: 0 (unknown) built with Clang 21.0.0 for Android aarch64 
- **llamafile:** NOT FOUND
- **MLC:** NOT FOUND
- **Docker:** NOT FOUND
- **Podman:** NOT FOUND
- **curl:** curl 8.21.0 (aarch64-unknown-linux-android) libcurl/8.21.0 OpenSSL/3.6.3 zlib/1.3.2 libssh2/1.11.1 nghttp2/1.69.0 ngtcp2/1.23.0 nghttp3/1.16.0 Release-Date: 2026-06-24 
- **jq:** jq-1.8.2 

## Model-file presence (names and paths intentionally omitted)
- **Inspected conventional model directories found:** 3
- **Model-like files found (count only):** 22
- **Model-like file size total (bytes):** 3728539960
- **Inference smoke test:** NOT RUN; inventory does not load or execute model files

## Workspace and subprocess smoke checks
- **Git worktree detected:** YES
- **Existing tracked changes (count only):** 0
- **Existing untracked paths (count only):** 1
- **Subprocess launch smoke test:** PASS (sh -c exit 0)
- **Temporary write/read smoke test:** PASS

## Explicitly not tested by this inventory
- GitHub authentication scopes, repository write permission, Actions availability, or provider quota (no authenticated requests made).
- Actual model inference, model quality, context limit, latency, or memory peak.
- Android screen-off survival, process suspension, battery/thermal behavior, and interruption recovery.
- Agent/MCP-specific subprocess permissions; this script tests only the shell route that launched it.
- Any remote runner or hybrid benchmark.

## Interpretation
- This report is an inventory, not a benchmark or a pass for Trnje acceptance tests.
- Review the report before sharing it; although values and file names are omitted, platform/tool metadata may still be identifying.
- No existing project files were edited by this script. It creates only this report and a temporary smoke-test file that it removes.
