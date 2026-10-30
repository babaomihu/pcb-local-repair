"""Copy a saved project inside a bounded, cancellable process group."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_local.workspace import copy_project

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("board", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    board, context = copy_project(args.board, args.destination)
    print(json.dumps({"board": str(board), "context": context}))
