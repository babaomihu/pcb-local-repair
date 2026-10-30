"""Run native preflight in a process group owned by the task supervisor."""
import argparse
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcb_local import validation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--kicad-cli", required=True)
    parser.add_argument("--kicad-python", required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=False)
    args.before = args.board
    args.deadline = time.monotonic() + 60
    result = {"complete": False}
    try:
        result["rule_support"] = validation.rule_support(args.board)
        if not result["rule_support"]["supported"]:
            result["status"] = "unsupported"
            result["errors"] = result["rule_support"]["reasons"]
        else:
            measurement = validation.measure(args.board, "native", args, validation.read_board(args.board))
            result.update(complete=True, status="complete", measurement=measurement)
    except Exception as exc:
        result.update(status="verification_incomplete", errors=[type(exc).__name__ + ": " + str(exc)])
        if any(json.loads(p.read_text()).get("timed_out") for p in args.out_dir.rglob("*.command.json")):
            result["status"] = "timed_out"
    (args.out_dir / "inspection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "measurement"}, ensure_ascii=False))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
