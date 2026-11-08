# trnje

`trnje` is a local-first autonomous coding agent intended to complete bounded software tasks with minimal user intervention while protecting user data and reporting only evidence-backed results.

## Current status

**Prototype implementation has started on `feat/local-model-adapter`.** The branch adds a bounded `llama-cli` adapter, a smoke-test command, and unit tests for success and failure handling. This is not yet a complete coding agent, and no acceptance test AT-01 through AT-14 is claimed as passed.

## Local model adapter

The adapter uses only a local `llama-cli` process. It does not call a remote provider or silently fall back to a paid service. Configure the model path explicitly:

```bash
export TRNJE_MODEL_PATH="$HOME/models/qwen2.5-coder-0.5b-instruct-q4_0.gguf"
python scripts/model_smoke.py
```

The command uses a bounded timeout and token count, emits a JSON result, and returns a nonzero exit code unless the expected marker appears in model output. To run unit tests, use `python -m pytest -q`.

## Minimal task loop (experimental)

The first task loop is intentionally narrow: it inspects one explicitly selected file, asks the local model for one exact text replacement, applies it only if the old text matches exactly once, and runs the fixed repository checks. It does not retry, run model-supplied shell commands, commit, push, or merge. If verification fails after an edit, the changed file is left in place and the JSON report says so; inspect the diff before continuing.

```bash
export TRNJE_MODEL_PATH="$HOME/models/qwen2.5-coder-0.5b-instruct-q4_0.gguf"
python scripts/run_task.py \
  --workspace "$HOME/trnje" \
  --file "path/inside/project.py" \
  --task "Describe one small, testable correction"
```

Use a real relative path and a narrow task. This is an experimental one-file loop, not proof of full autonomous coding capability. Its output is a JSON report; exit code 0 means the fixed checks passed, not that the task was independently judged correct.

## Prototype goal

For one bounded coding task, the first prototype should inspect the project, make a scoped fix, run relevant checks, attempt a limited repair when justified, preserve progress across interruption, and report the observed result with evidence.

## Operating principles

- Local-first and free-only by default; paid providers must never be activated silently.
- One primary agent loop; extra agents, providers, state stores, or services require demonstrated value.
- Preserve existing user changes; do not silently discard or include unrelated work.
- Protect secrets and private data before any external transmission or publication.
- A success claim requires execution evidence. Missing or unverifiable evidence is not a pass.
- Automatic merge is allowed only after all applicable mandatory checks and policy gates pass.

The canonical design and authority boundaries are in [DESIGN.md](DESIGN.md). The 16 acceptance tests and release gate are in [ACCEPTANCE_TESTS.md](ACCEPTANCE_TESTS.md).

## Initial phases

1. **Compare environments:** measure Android/Termux, a hybrid Termux plus selective GitHub Actions setup, and a separate host/runner only if one is actually available. Use the same representative task and report evidence, limitations, cost, and reliability. Do not choose an environment based on assumptions.
2. **Establish the smallest working loop:** task input, bounded tools, model adapter, relevant checks, evidence-backed result, and bounded retries.
3. **Add recovery and privacy controls:** one authoritative task-state source, safe restart behavior, and checks of exact outgoing content.
4. **Enable Git automation conditionally:** branch/commit/PR workflow first; automatic merge only after every applicable required check and policy gate passes.
5. **Enable optional services only when justified:** no mandatory cloud model, paid provider, or always-on session.

Each phase must deliver a working capability. Do not add scaffolding or administration without a concrete acceptance test or demonstrated need. Phase exits and test status must be supported by actual evidence; planned tests are not passed tests.
