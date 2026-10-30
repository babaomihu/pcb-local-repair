"""Project a protected engine candidate onto its declared additive ROI.

This is candidate construction, never electrical or DRC acceptance. Whole
new target-net segments may be discarded; existing objects may never change.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_local.validation import (Quoted, SIDECARS, canonical, copy_library_context,
                                  field, name, parse_sexpr, protection, read_board)
from pcb_local.workspace import check_output_location, source_files


ROI_ERROR = "copper envelope outside allowed region"


class ScopeRejected(ValueError):
    """Raw output violates a rule that projection is forbidden to hide."""


def _serialize(node, depth=0):
    """Serialize parsed KiCad tokens, retaining quoted escapes verbatim.

    parse_sexpr's Quoted already stores the escape sequences from the file.
    JSON quoting or escaping a second time would alter the represented text.
    Implicit bare teardrop children become ordinary parenthesized children.
    """
    if isinstance(node, Quoted):
        return '"' + str(node) + '"'
    if isinstance(node, str):
        return node
    if not isinstance(node, list):
        raise TypeError("Unsupported S-expression token: " + type(node).__name__)
    if not any(isinstance(child, list) for child in node):
        return "(" + " ".join(_serialize(child, depth + 1) for child in node) + ")"
    prefix, index = [], 0
    while index < len(node) and not isinstance(node[index], list):
        prefix.append(_serialize(node[index], depth + 1))
        index += 1
    children = ["  " * (depth + 1) + _serialize(child, depth + 1) for child in node[index:]]
    return "(" + " ".join(prefix) + "\n" + "\n".join(children) + "\n" + "  " * depth + ")"


def _copy_raw_files(board, folder):
    """Preserve engine-authored bytes; do not invent missing rule files."""
    folder.mkdir()
    copied = folder / "board.kicad_pcb"
    shutil.copy2(board, copied)
    for suffix in SIDECARS:
        source = board.with_suffix(suffix)
        if source.is_file():
            shutil.copy2(source, copied.with_suffix(suffix))
    for table in ("fp-lib-table", "sym-lib-table"):
        if (board.parent / table).is_file():
            shutil.copy2(board.parent / table, folder / table)
    return copied


def _routing_uuid(node):
    value = field(node, "uuid") or field(node, "tstamp")
    return str(value[1]) if value and len(value) >= 2 else None


def build_scoped_candidate(before, raw_after, task, out_dir):
    """Return a persisted projection report and, when ready, a new project.

    ``task`` uses validation's target_net/allowed_layer/region_mm contract.
    A fresh out_dir contains raw-engine/ evidence, candidate/ (only when raw
    protection passed), and scope-report.json. status=ready means *only* that
    candidate construction passed; full native verification is still required.
    """
    started = time.monotonic()
    before, raw_after, out_dir = (Path(value).resolve() for value in (before, raw_after, out_dir))
    # Refuse before making output when it would recursively enter a source lib.
    check_output_location(before, out_dir)
    check_output_location(raw_after, out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "status": "error", "candidate_board": None,
              "before": str(before), "raw_after": str(raw_after),
              "task": json.loads(json.dumps(task)), "requires_native_verification": True,
              "discarded_segments": [], "retained_added_segments": [], "errors": [],
              "source_hashes_before": {}, "source_hashes_after": {},
              "policy": "Only discard whole newly added authorized-net/layer straight segments outside ROI; never trim, move, resize, or repair protected changes."}
    try:
        for board in (before, raw_after):
            report["source_hashes_before"].update(source_files(board))
        evidence = _copy_raw_files(raw_after, out_dir / "raw-engine")
        report["raw_engine_board"] = str(evidence)
        old, raw = read_board(before), read_board(raw_after)
        raw_check = protection(old, raw, before, raw_after, task)
        report["raw_protection"] = raw_check
        blocking = [key for key, okay in raw_check["checks"].items()
                    if key != "additions_within_permission" and not okay]
        invalid = [row for row in raw_check["scope_errors"]
                   if any(error != ROI_ERROR for error in row["errors"])]
        if blocking or invalid:
            report["blocking_checks"] = blocking
            report["invalid_additions"] = invalid
            raise ScopeRejected("Raw engine output has protected changes or forbidden additions; no projection is permitted")

        # Every scope error now has exactly the sole permitted cause. In
        # particular, an out-of-ROI via or wrong-net segment is never erased.
        discarded = {row["uuid"] for row in raw_check["scope_errors"]}
        added = set(raw_check["added_routing"])
        report["discarded_segments"] = [{"uuid": uid, "reason": ROI_ERROR} for uid in sorted(discarded)]
        report["retained_added_segments"] = sorted(added - discarded)
        report["raw_added_count"] = len(added)
        report["discarded_count"] = len(discarded)
        report["retained_added_count"] = len(added - discarded)

        candidate = _copy_raw_files(raw_after, out_dir / "candidate")
        report["library_context"] = copy_library_context(raw_after, candidate.parent, before)
        if discarded:
            roots = parse_sexpr(raw_after.read_text(encoding="utf-8"))
            if len(roots) != 1 or name(roots[0]) != "kicad_pcb":
                raise ScopeRejected("Raw output is not one KiCad board")
            root = roots[0]
            removed = [node for node in root[1:] if name(node) == "segment" and _routing_uuid(node) in discarded]
            if len(removed) != len(discarded):
                raise ScopeRejected("Discard UUIDs do not identify exactly one added segment each")
            root[:] = [root[0]] + [node for node in root[1:]
                                  if not (name(node) == "segment" and _routing_uuid(node) in discarded)]
            serialized = _serialize(root) + "\n"
            reparsed = parse_sexpr(serialized)
            if canonical(reparsed) != canonical([root]):
                raise ScopeRejected("AST serialization changed retained semantics")
            candidate.write_text(serialized, encoding="utf-8")
            report["serialization"] = "AST removal and standard parenthesized serialization; quoted escapes preserved"
        else:
            report["serialization"] = "byte-identical raw board copy"

        scoped = read_board(candidate)
        expected_ids = set(raw["routing"]) - discarded
        if set(scoped["routing"]) != expected_ids or any(
                canonical(scoped["routing"][uid]) != canonical(raw["routing"][uid]) for uid in expected_ids):
            raise ScopeRejected("Retained routing does not exactly match the raw candidate")
        if scoped["static"] != raw["static"]:
            raise ScopeRejected("Serialization changed nonrouting definitions")
        expected_identity = {uid: value for uid, value in raw["identities"].items() if uid not in discarded}
        if scoped["identities"] != expected_identity:
            raise ScopeRejected("Serialization changed retained item identity/net")
        check = protection(old, scoped, before, candidate, task)
        report["candidate_protection"] = check
        if not all(check["checks"].values()):
            raise ScopeRejected("Scoped candidate failed static protection checks")
        report["candidate_board"] = str(candidate)
        report["candidate_hashes"] = source_files(candidate)
        report["status"] = "ready"
    except (ScopeRejected, ValueError, IndexError, TypeError) as exc:
        report["status"] = "rejected"
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
    except Exception as exc:
        report["status"] = "error"
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        try:
            for board in (before, raw_after):
                report["source_hashes_after"].update(source_files(board))
            intact = bool(report["source_hashes_before"]) and report["source_hashes_after"] == report["source_hashes_before"]
        except Exception as exc:
            intact = False
            report["errors"].append("Source integrity check failed: " + str(exc))
        report["source_files_untouched"] = intact
        if not intact:
            report["status"] = "rejected"
            report["errors"].append("Source board/project/context changed during candidate construction")
        if report["status"] != "ready":
            report["candidate_board"] = None
        report["seconds"] = time.monotonic() - started
        report["report_path"] = str(out_dir / "scope-report.json")
        (out_dir / "scope-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("before", "raw-after", "task", "out-dir"):
        parser.add_argument("--" + argument, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        task = json.loads(args.task.read_text(encoding="utf-8"))
        result = build_scoped_candidate(args.before, args.raw_after, task, args.out_dir)
    except Exception as exc:
        # An existing/unsafe out-dir is never overwritten to log an error.
        print(json.dumps({"status": "error", "candidate_board": None,
            "requires_native_verification": True, "errors": [type(exc).__name__ + ": " + str(exc)]}), file=sys.stderr)
        return 2
    print(json.dumps({key: result.get(key) for key in
        ("status", "candidate_board", "report_path", "discarded_count", "retained_added_count", "errors")}, ensure_ascii=False))
    return {"ready": 0, "rejected": 1, "error": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
