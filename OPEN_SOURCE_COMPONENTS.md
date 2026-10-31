# Free and open-source components for Trnje

This inventory distinguishes source availability from a genuinely free, usable runtime. Trnje remains local-first; no paid API, hosted model, subscription, or metered provider may be activated.

## Imported source (reference only)

- **mini-SWE-agent** — https://github.com/SWE-agent/mini-swe-agent
  - License: MIT (`LICENSE.md`, upstream commit `04d809ceab9df28f9adaed044884180159172930`).
  - Imported as a pinned Git submodule at `vendor/mini-swe-agent`.
  - Purpose: inspect its compact agent loop, step limits, trajectory persistence, and model/environment separation.
  - **Not enabled as Trnje's runtime.** Its current `pyproject.toml` declares a substantial dependency set (including LiteLLM, Pydantic, Textual, datasets, and OpenAI client packages). Its local environment executes shell commands with `shell=True`; it must not be run against a real workspace until a separate sandbox and explicit command policy are proven.
  - No model, API key, cloud service, or dependency installation is included by this source import.

## Already available in the Termux environment

- **llama.cpp** — https://github.com/ggml-org/llama.cpp
  - License: MIT.
  - Trnje's Termux environment already has `llama-cli` and a configured local model path according to the existing project handoff.
  - Do not add a second source checkout or download another model until a measured need is established.
  - Upstream documents a Termux build path: https://github.com/ggml-org/llama.cpp/blob/master/docs/android.md

## Evaluated but not installed

- **OpenHands / Agent Canvas** — https://github.com/OpenHands/OpenHands
  - License: MIT.
  - Current project is a self-hosted developer control center with multiple backends. Its operational footprint is not justified for the current Android/Termux target.
- **OpenCode** — https://github.com/anomalyco/opencode
  - License: MIT.
  - Terminal coding agent. Android/Termux runtime compatibility has not been demonstrated; do not install its generic Linux binaries or run its install script on Android without a device-specific test.
- **SWE-agent (original)** — https://github.com/SWE-agent/SWE-agent
  - License: MIT.
  - Upstream README says mini-SWE-agent supersedes it; therefore it is not separately imported.
- **Aider** — https://github.com/Aider-AI/aider
  - Candidate only. Do not install until its exact current license, Python dependency footprint, and local-model behavior are checked against the target Termux environment.

## Mandatory free-only policy

1. Agent source license and model-weight license are separate. Verify both before use.
2. A local model is not automatically free for every use; inspect its model card and license.
3. Do not configure hosted inference, paid APIs, subscriptions, or usage-metered services.
4. Do not execute imported agent code in the Trnje workspace until command isolation, workspace boundaries, timeout handling, and output redaction are independently tested.
5. A Git submodule is source acquisition, not runtime installation. This import does not claim that mini-SWE-agent runs on Android or passes Trnje's acceptance tests.

## Retrieval

For an existing clone of Trnje, retrieve the pinned source with:

```sh
git submodule update --init --depth 1 vendor/mini-swe-agent
```

For a fresh clone, use:

```sh
git clone --recurse-submodules <trnje-repository-url>
```

Do not use `--remote`: the submodule is intentionally pinned to the reviewed upstream commit.
