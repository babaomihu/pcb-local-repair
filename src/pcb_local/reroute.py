"""Prepare a disposable routing transaction; the original snapshot is immutable."""
from pathlib import Path
from .modification import contract
from .scope_candidate import _serialize, _routing_uuid, _copy_raw_files
from .validation import parse_sexpr, name, read_board, canonical, copy_library_context


def prepare(board, task, folder):
    board, folder = Path(board), Path(folder)
    spec = contract(task)
    if spec["mode"] != "ripup":
        raise ValueError("Reroute preparation requires an explicit ripup task")
    selected = set(spec["routing_uuids"])
    old = read_board(board)
    if not selected <= set(old["routing"]):
        raise ValueError("Authorized original track disappeared before transaction")
    candidate = _copy_raw_files(board, folder)
    copy_library_context(board, folder, board)
    roots = parse_sexpr(board.read_text())
    root = roots[0]
    root[:] = [root[0]] + [node for node in root[1:] if not (name(node) == "segment" and _routing_uuid(node) in selected)]
    candidate.write_text(_serialize(root) + "\n")
    check = read_board(candidate)
    if (set(check["routing"]) != set(old["routing"]) - selected or check["static"] != old["static"]
            or any(canonical(check["routing"][uid]) != canonical(old["routing"][uid]) for uid in check["routing"])):
        raise ValueError("Reroute transaction altered an unauthorized object")
    return candidate
