#!/usr/bin/env python3
"""One bounded local-model smoke test; requires TRNJE_MODEL_PATH."""

import argparse
import json
import sys
from pathlib import Path

# Make direct execution from any working directory resolve the repository package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trnje.local_model import run_local_model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--tokens", type=int, default=24)
    args = parser.parse_args()
    result = run_local_model(
        "Reply with exactly: TRNJE_LOCAL_MODEL_OK",
        timeout_seconds=args.timeout,
        max_tokens=args.tokens,
        context_size=1024,
        threads=4,
    )
    print(json.dumps({
        "status": result.status,
        "output": result.output,
        "error": result.error,
        "returncode": result.returncode,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
    }, ensure_ascii=False))
    return 0 if result.status == "PASS" and "TRNJE_LOCAL_MODEL_OK" in result.output else 1


if __name__ == "__main__":
    raise SystemExit(main())
