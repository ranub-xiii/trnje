# trnje design

## Mission and boundaries

Build a local-first coding agent that executes bounded software tasks, adapts effort to task risk, preserves work through interruptions, and reports what actually happened. The primary agent loop does the work; additional agents or services are optional and must justify their complexity.

The agent may read, edit, test, repair, branch, and commit within its configured project workspace. This authority does not extend automatically to unrelated user files, accounts, credentials, external services, or irreversible actions. Implementation must follow the approved project scope and acceptance gates.

## Minimal runtime

The initial runtime needs only these capabilities, not necessarily separate modules:

1. Task input, initially a local CLI or task file; GitHub Issues can be added as an adapter.
2. One adaptive agent loop: inspect, plan proportionally, act, observe, test, repair if useful, verify, report.
3. Bounded file, search, Git, shell, and test operations.
4. A model adapter. Prefer a configured local model when it is capable; remote providers are optional.
5. One authoritative durable task-state source sufficient for task identity, workspace/branch, checkpoint, attempts, changed-file summary, check results, and evidence locations. SQLite is a candidate, not a requirement.
6. A privacy/output gate before external transmission or publication.
7. A concise result with status and links/paths to execution evidence.

Do not introduce a distributed task bus, mandatory multi-agent chain, separate service for every responsibility, elaborate dashboard, or duplicate state unless evidence demonstrates a concrete need.

## State, recovery, and change preservation

- Keep one authoritative source for task status. Other files, databases, comments, or external interfaces may mirror it only where required; they must not independently claim conflicting outcomes.
- Persist only what is needed to resume safely and identify the last meaningful operation and evidence.
- On restart, inspect actual filesystem and Git/worktree state; never infer success from an intended or interrupted operation.
- Preserve pre-existing and unrelated changes. Do not reset, clean, overwrite, stage, commit, or merge unrelated work silently.
- Capture command, working directory, exit code, bounded stdout/stderr, timeout, and relevant environment metadata. Redact secrets before persistence.
- Bound time, tokens, and attempts. Do not repeat an unchanged failure indefinitely; retry only with a reason or changed hypothesis/input/action.

## Model routing and cost

Local execution and deterministic tools should remain useful when a model, network, GitHub, or other optional service is unavailable. The system must report a specific blocker when it cannot continue safely.

Free-only mode is a hard policy: paid APIs are disabled by default and must not be invoked after local failure unless the user explicitly changes the policy. Provider selection must never silently incur charges. No particular model or cloud service is a hard dependency.

## Confidentiality and publication

Treat handling requirements as substantive, not as labels:

- **Project data:** content intended for the project repository.
- **Confidential data:** private user data, private logs, or content not intended for public release.
- **Secrets:** tokens, passwords, cookies, credentials, and other authentication material.

Before any external transmission or publication, inspect the exact outgoing payload, including the diff, logs, artifacts, PR body, issue/comment text, and other message content. Scan for secrets and private data; redact or exclude them. A `CONFIDENTIAL` label is metadata, not a security control. Record credential use only as non-secret metadata such as service, purpose, outcome, and time. Minimize content sent to external models. If publication safety is uncertain, block publication while preserving safe local work and diagnostics.

## Verification and automatic merge

Verification must match task risk. Run relevant targeted checks, broaden them when the change warrants it, inspect the final diff and changed-file scope, and capture evidence for every required command.

Automatic merge is permitted only if all of the following are true:

1. Every applicable mandatory check has a verified successful result.
2. No required result is failed, missing, cancelled, stale, or unverifiable.
3. The target branch, diff, and changed files comply with task and repository policy.
4. The privacy/output gate passes for all content being transmitted or published.
5. The target branch's configured protection and merge policy explicitly permit the merge.
6. No unresolved integrity or workspace-preservation concern remains.

If any gate is not satisfied, block the merge and report the exact missing or failed condition. “Tests probably passed,” a written criterion, or a model's assertion is not evidence. Never describe an unexecuted test as passed.

## Environment selection before implementation

Compare, using the same representative task and documented procedure:

- **Android/Termux local-first**
- **Hybrid:** Termux for primary work plus selective GitHub Actions
- **Separate host/runner:** only if one is actually available to test

Measure and record, where applicable: setup requirements; task success and check results; startup and end-to-end time; peak memory if measurable; model compatibility and latency; battery/thermal impact; storage; screen-off/background reliability; network/service failure behavior; interruption recovery; privacy implications; ongoing cost; and operational complexity. Mark unavailable measurements explicitly rather than inventing estimates. Define the minimum acceptable task-performance threshold before comparing results. Choose only from observed results; if no option meets the threshold, document the blocker and revise the scope rather than claiming feasibility.

## Result statuses and evidence

Use `PASS`, `FAIL`, `BLOCKED`, and `UNKNOWN` consistently. `PASS` requires observed execution evidence matching the criterion. `FAIL` means observed behavior violated it. `BLOCKED` means execution could not proceed due to a specific prerequisite or policy restriction. `UNKNOWN` means evidence is absent or insufficient. Keep conclusions distinct from observations and assumptions.
