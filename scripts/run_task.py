#!/usr/bin/env python3
"""Run one bounded local-model task against one explicitly selected file."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trnje.task import run_task


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Existing project workspace")
    parser.add_argument("--file", required=True, help="One relative file to inspect and edit")
    parser.add_argument("--task", required=True, help="Bounded task description")
    parser.add_argument("--model-timeout", type=int, default=120)
    parser.add_argument("--check-timeout", type=int, default=120)
    args = parser.parse_args()
    report = run_task(
        args.workspace,
        args.file,
        args.task,
        model_timeout_seconds=args.model_timeout,
        check_timeout_seconds=args.check_timeout,
    )
    print(json.dumps(report.as_dict(), ensure_ascii=False))
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
