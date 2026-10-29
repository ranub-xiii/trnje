# Environment assessment and implementation decision

## Status

**Status: BLOCKED for environment selection; implementation has not started.**

This document defines the evidence needed to select Trnje's first execution environment. Prior conversation records are leads, not a substitute for a fresh, reproducible inventory from the current Termux session. Do not describe any option as benchmarked until the procedure below has actually run and its output has been saved.

## Known baseline from prior diagnostics

The following details were reported in earlier sessions and must be revalidated before they are treated as current:

- Android API 34, aarch64.
- Python 3.13.13; Node.js 24.17 and npm 11.17.
- Debian 13 under proot; tmux available.
- Ruff 0.15.19 and coverage 7.16.2.
- Reported RAM about 5.67 GB; storage previously reported around 35–39 GB free.
- Prior model/provider experience included Codex CLI usage-limit problems, initial Gemini CLI authentication problems, and local-model configuration/input-limit failures in another project. These are not proof of current Trnje model availability or of current account quota state.
- A past Termux MCP diagnostic could launch `/system/bin/true`, while launching its Python interpreter raised `PermissionError`. Runtime identity, mount flags, and effective access were not established then. This is a specific diagnostic lead, not proof that all Termux tools or execution routes are blocked.

## Candidate environments

### A. Termux-first

Use the existing Android/Termux environment as the main executor. Inventory installed tools before installing anything. Verify Git, Python, shell, test runners, compiler/build tools, storage/workspace access, background execution, local model runtimes, and whether commands can run from the actual agent process—not just from an interactive terminal.

**Advantages to measure:** reuse of installed tools, low additional setup, local control, offline deterministic operations, no mandatory cloud-model spend.

**Risks to measure:** Android process suspension, screen-off survival, memory pressure, model compatibility and latency, filesystem boundaries, battery/thermal behavior, and MCP subprocess restrictions.

### B. Hybrid: Termux plus GitHub Actions

Keep task orchestration and workspace changes in Termux; delegate only reproducible CI jobs or explicitly remote-required work to GitHub Actions. Use a minimal workflow with pinned runtime versions and explicit permissions. Treat network-dependent services as optional; never make Actions the only copy of task state or local work.

**Advantages to measure:** remote CI isolation and repeatability, access to runner-specific tools, possible continuation of checks while the phone is idle.

**Risks to measure:** network dependence, queue/startup latency, workflow minutes/permissions, secrets exposure, artifact retention, synchronization conflicts, and failure handling when GitHub is unavailable.

### C. Separate host or runner

Test only if a real machine or runner is available and authorized. Do not create or assume a paid server. Record resource limits, ownership, persistence, access controls, costs, and operational burden before use.

**Advantages to measure:** reliable long-running processes and additional CPU/RAM where the host actually provides them.

**Risks to measure:** provisioning and maintenance, cost, credential management, network dependency, and extra attack surface.

## Inventory before benchmark

Capture a sanitized, read-only report from the actual Termux session. Include:

- Android/Termux version, architecture, kernel/runtime identity where accessible, memory and free storage.
- Installed command paths and versions for Git, Python, pip, Node/npm, shell, tmux, proot, compilers, test/lint/format tools, GitHub CLI, container tools if present, and process/background utilities.
- Installed model runtimes and available local model files, sizes, quantization/context configuration, and a short actual inference smoke test if safe. Do not infer model capability from installed package names alone.
- GitHub access scope and Actions availability from existing authorized connections; do not print tokens or credential values.
- Workspace write/read access, subprocess launch behavior, process persistence after screen-off, and ability to recover after a controlled interruption.
- Current project changes before and after each test. Never run destructive cleanup, reset, or broad installation as part of inventory.

Record unavailable checks as `UNKNOWN` or `BLOCKED`, including the exact reason. Redact environment variables and command output that could contain tokens, account identifiers, private paths, or other secrets.

### Current inventory helper

A read-only helper is committed at [`scripts/inventory_termux.sh`](scripts/inventory_termux.sh). Run it from the Trnje repository root in the actual Termux shell with `bash scripts/inventory_termux.sh`. It creates a new, permission-restricted Markdown report in the current directory and refuses to overwrite an existing report. It does not install packages, make network calls, run model inference, or modify Git state. It reports tool availability/version output, memory/storage, model-like file counts and aggregate size (not names), existing Git change counts (not paths), and shell subprocess plus temporary write/read smoke checks. Review the generated report before sharing it; platform metadata can still be identifying. This helper does **not** test Android screen-off survival, the agent/MCP subprocess route, authenticated GitHub/Actions access, model quality, or the comparative coding benchmark.

The current ChatGPT session has no verified direct command-execution path into the user's active Termux shell. Therefore committing and read-back verifying the helper is not evidence that it has run on the device. Its report must be collected from that real environment before the inventory status changes.

## Common benchmark

Run the same small, deterministic coding task in each candidate environment that is actually available. The task should require reading a small repository, changing one function, adding or adjusting a test, running targeted tests and lint, and producing a final diff plus evidence. Use the same input, test, time/attempt budget, and acceptance criteria.

Capture per run:

1. Environment and runtime versions.
2. Task completion and correctness against the test.
3. Targeted test/lint results and exit codes.
4. Setup/startup and end-to-end elapsed time.
5. Peak memory if measurable; storage use; battery/thermal behavior where measurable.
6. Model identity/configuration, latency, context limits, and any inference errors.
7. Screen-off/background survival and controlled-interruption recovery.
8. Network/provider outages and behavior when optional services are unavailable.
9. Privacy boundary: exact outbound payload, permissions, secrets scanning, and artifacts retained.
10. Recurring cost, setup effort, operational complexity, and known limitations.

Do not compare different tasks or model settings as if they were equivalent. Run multiple repetitions only where variability matters and record all attempts, not just the best run.

## Decision rule

Before running the comparison, agree on minimum thresholds for correctness, preservation of existing changes, secret safety, recoverability, and acceptable end-to-end time. Security and change-preservation requirements are hard gates, not weighted trade-offs.

Select the least complex environment that passes the hard gates and meets the performance threshold. Prefer Termux-first if it passes; choose hybrid only when measured Actions value justifies the additional integration; choose a separate host only when it is available, authorized, and its measured benefits outweigh its cost and maintenance. If no tested option passes, narrow the task or record the blocker. Do not claim feasibility from intuition.

## Initial architecture proposal after selection

Keep the first implementation minimal:

- One task loop with explicit, bounded inspect → edit → test → repair → verify → report transitions.
- One authoritative durable task-state source; avoid parallel competing status files.
- A narrow, configurable tool interface for workspace reads/writes, shell commands, Git, and tests.
- A model adapter that is optional to deterministic local operations.
- Explicit time/attempt/token budgets and stop conditions.
- A publication gate that scans the exact outgoing diff, logs, artifacts, issue/PR text, and other payloads before external transmission.
- Evidence-backed `PASS`, `FAIL`, `BLOCKED`, and `UNKNOWN` statuses.
- Git branch/commit/PR automation only after workspace preservation and privacy checks are proven. Automatic merge remains gated on every applicable verified required check and repository policy.

Do not begin the implementation until the inventory and comparison report establish the feasible execution path and document unresolved blockers.

## Provider limits and system clock

Do not change the device clock or falsify timestamps to bypass a Codex/provider usage limit or make a service misread prior usage. That is not a valid capacity strategy and can corrupt logs, token expiry, TLS checks, cache validity, scheduled jobs, and audit chronology. It also does not establish that the provider will reset or ignore its server-side quota.

Use legitimate options instead: wait for the provider's stated reset, inspect the actual quota/reset message, continue deterministic coding and tests locally while the model is unavailable, or use a separately authorized provider/model within its real limits and cost policy. Trnje's free-only mode must not silently switch to paid service. Do not store credentials or quota tokens in benchmark reports.

## Evidence status

- Inventory helper: **PASS — committed and read back from GitHub** ([script](scripts/inventory_termux.sh), commit `22725c3e2ccdd21931d2db7f81ddcf86c249dad2`). This verifies repository persistence only, not script execution.
- Current Termux inventory: **UNKNOWN — helper not run in the device shell; no direct Termux execution channel is available in this session**.
- GitHub Actions availability: **UNKNOWN — code search returned no indexed workflow results and the inventory commit has no reported status contexts; neither observation proves Actions are disabled**.
- Termux-first benchmark: **UNKNOWN — not run**.
- Hybrid benchmark: **UNKNOWN — not run**.
- Separate-host benchmark: **UNKNOWN — availability not established**.
- Environment selection: **BLOCKED pending evidence**.
- Trnje implementation: **not started**.
