# trnje

`trnje` is a local-first autonomous coding agent intended to complete bounded software tasks with minimal user intervention while protecting user data and reporting only evidence-backed results.

## Current status

**Initial project approved; implementation not yet started.** The private repository exists. The initial design and acceptance criteria are being committed before implementation.

## Prototype goal

For one bounded coding task, the first prototype should inspect the project, make a scoped change, run relevant checks, attempt a limited repair when justified, preserve progress across interruption, and report the observed result with evidence.

## Operating principles

- Local-first and free-only by default; paid providers must never be activated silently.
- One primary agent loop; extra agents, providers, state stores, or services require demonstrated value.
- Preserve existing user changes; do not silently discard or include unrelated work.
- Protect secrets and private data before any external transmission or publication.
- A success claim requires execution evidence. Missing or unverifiable evidence is not a pass.
- Automatic merge is allowed only after all applicable mandatory checks and policy gates pass.

The canonical design and authority boundaries are in [`DESIGN.md`](DESIGN.md). The 16 acceptance tests and release gate are in [`ACCEPTANCE_TESTS.md`](ACCEPTANCE_TESTS.md).

## Initial phases

1. **Compare environments:** measure Android/Termux, a hybrid Termux plus selective GitHub Actions setup, and a separate host/runner only if one is actually available. Use the same representative task and report evidence, limitations, cost, and reliability. Do not choose an environment based on assumptions.
2. **Establish the smallest working loop:** task input, bounded tools, model adapter, relevant checks, evidence-backed result, and bounded retries.
3. **Add recovery and privacy controls:** one authoritative task-state source, safe restart behavior, and checks of exact outgoing content.
4. **Enable Git automation conditionally:** branch/commit/PR workflow first; automatic merge only after every applicable required check and policy gate passes.
5. **Enable optional services only when justified:** no mandatory cloud model, paid provider, or always-on session.

Each phase must deliver a working capability. Do not add scaffolding or administration without a concrete acceptance test or demonstrated need. Phase exits and test status must be supported by actual evidence; planned tests are not passed tests.
