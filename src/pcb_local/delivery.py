# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit delivery of accepted results. Never accesses the live editor."""
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import stat
import tempfile
import uuid

from .batch import check_chain
from .jobs import write_json
from .workspace import assert_unchanged, source_files, sha256, check_output_location


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def plain_path(path):
    """Reject linked files/directories instead of exporting through aliases."""
    path = Path(path).expanduser().absolute()
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError("Linked paths are not supported for delivery: " + str(part))
    return path


def check_fill_evidence(folder, report):
    """Historical accepted flags never substitute for exact native fill proof."""
    from .validation import has_zones
    folder=Path(folder).resolve()
    if report.get('command')=='batch':
        for link in report.get('accepted_chain',[]):
            path=plain_path(link['result'])
            check_fill_evidence(path.parent,read(path))
        return
    if not (has_zones(report['source_board']) or has_zones(report['candidate'])):
        return
    verified=report.get('verification',{})
    directory=plain_path(report.get('verification_directory',folder/'verification'))
    if folder not in directory.parents:
        raise ValueError('Fill verification evidence lies outside the accepted task')
    witness=verified.get('zone_refill',{})
    if (not verified.get('accepted') or not verified.get('verification_complete')
            or verified.get('checks',{}).get('zone_refill_scope_preserved') is not True
            or witness.get('status')!='verified' or witness.get('checks',{}).get('zone_refill_scope_preserved') is not True
            or verified.get('case',{}).get('zone_refill') != report.get('task',{}).get('zone_refill')
            or not (directory/'verification.json').is_file()
            or read(directory/'verification.json')!=verified):
        raise ValueError('Exact native fill protection evidence is missing or changed; historical accepted results require re-verification')
    for path,expected in report['candidate_hashes'].items():
        if Path(path).parent==Path(report['candidate']).parent and Path(path).name.startswith('board.'):
            if verified.get('source_hashes_after',{}).get(path)!=expected:
                raise ValueError('Fill protection verifier did not bind this candidate context')


def accepted_result(task_dir):
    folder = Path(task_dir).resolve()
    report = read(folder / "result.json")
    state = read(folder / "status.json")
    if (report.get("status") != "accepted" or report.get("accepted") is not True
            or report.get("exit_code") != 0 or state.get("status") != "accepted"
            or Path(report["task_dir"]).resolve() != folder):
        raise ValueError("Only a completed accepted result may be saved or adopted")
    source = plain_path(report["source_board"])
    candidate = plain_path(report["candidate"])
    repair_outputs = ("scoping/candidate/board.kicad_pcb", "geometry-search/candidate/board.kicad_pcb")
    repair_outputs += tuple('pns-candidates/%d/board.kicad_pcb'%i for i in range(8))
    repair_outputs += tuple('strategies/'+strategy+'/'+relative
        for strategy in ('add-only','push','pns-fixed','push-local-refill','pns-fixed-local-refill','ripup','multilayer') for relative in repair_outputs)
    repair_outputs += ('strategies/geometry-multilayer-local-refill/geometry-search/candidate/board.kicad_pcb',)
    repair_outputs += ("strategies/geometry/geometry-search/candidate/board.kicad_pcb", "strategies/geometry-multilayer/geometry-search/candidate/board.kicad_pcb", "trace-quality/proposal/board.kicad_pcb")
    allowed = {folder / relative for relative in (*repair_outputs, "candidate/board.kicad_pcb")}
    if report.get("command") == "batch":
        check_chain(report)
        child = Path(report["accepted_chain"][-1]["result"]).resolve().parent
        if child.parent != folder or not child.name.endswith("-repair"):
            raise ValueError("Invalid batch candidate task location")
        allowed = {child / relative for relative in repair_outputs}
    owner_folder = child if report.get('command') == 'batch' else folder
    owner = read(owner_folder/'result.json') if report.get('command') == 'batch' else report
    focused = None
    try:
        rel=candidate.relative_to(owner_folder).as_posix()
    except ValueError:rel=''
    matched=re.fullmatch(r'strategies/((?:push|push-local-refill|ripup|multilayer)-focused-[0-9a-f]{12})/(scoping/candidate|pns-candidates/[0-7])/board\.kicad_pcb',rel)
    if matched:
        focused=matched.group(1)
        if owner.get('automatic_strategy',{}).get('selected')==focused:
            allowed.add(candidate)
    if candidate not in allowed:
        raise ValueError("Candidate is not a validated task output")
    if 'pns-candidates' in candidate.relative_to(folder).parts:
        owner = read(child/'result.json') if report.get('command') == 'batch' else report
        owner_folder = child if report.get('command') == 'batch' else folder
        pool = owner.get('candidate_pool',{})
        selected = [row for row in pool.get('candidates',[]) if row.get('fingerprint') == pool.get('selected_fingerprint')]
        if (pool.get('status') != 'accepted' or len(selected) != 1 or selected[0].get('status') != 'accepted'
                or selected[0].get('board') != str(candidate) or selected[0].get('hashes') != owner['candidate_hashes']):
            raise ValueError('PNS candidate does not match its accepted pool entry')
        directory = plain_path(owner.get('verification_directory',''))
        prefixes = [owner_folder] + [owner_folder/'strategies'/s for s in ('push','pns-fixed','push-local-refill','pns-fixed-local-refill')]
        if focused:prefixes.append(owner_folder/'strategies'/focused)
        verification_paths = {p/'verification' for p in prefixes}
        verification_paths.update(p/'candidate-validations'/str(i)/'verification' for p in prefixes for i in range(1,8))
        if directory not in verification_paths or selected[0].get('verification_directory') != str(directory):
            raise ValueError('PNS pool verifier is not an exact managed task location')
        verified = read(directory/'verification.json')
        if (verified != owner.get('verification') or not verified.get('verification_complete')
                or not verified.get('accepted') or not verified.get('checks') or not all(verified['checks'].values())):
            raise ValueError('PNS pool independent acceptance evidence changed or is incomplete')
        for path, expected in owner['candidate_hashes'].items():
            if Path(path).parent == candidate.parent and Path(path).name.startswith('board.'):
                if verified.get('source_hashes_after',{}).get(path) != expected:
                    raise ValueError('PNS pool verifier did not bind the candidate context')
    if rel=='trace-quality/proposal/board.kicad_pcb':
        quality=owner.get('trace_quality',{})
        directory=owner_folder/'trace-quality/verification'
        verified=read(directory/'verification.json')
        if (quality.get('status')!='accepted' or quality.get('candidate_board')!=str(candidate)
                or quality.get('candidate_hashes')!=owner.get('candidate_hashes')
                or quality.get('verification_directory')!=str(directory)
                or owner.get('verification')!=verified or not verified.get('verification_complete')
                or not verified.get('accepted') or not verified.get('checks') or not all(verified['checks'].values())):
            raise ValueError('Quality candidate lost its complete independent acceptance evidence')
        for path,expected in owner['candidate_hashes'].items():
            if Path(path).parent==candidate.parent and Path(path).name.startswith('board.') and verified.get('source_hashes_after',{}).get(path)!=expected:
                raise ValueError('Quality verifier did not bind final candidate context')
    assert_unchanged(source, report["source_hashes"])
    assert_unchanged(candidate, report["candidate_hashes"])
    check_fill_evidence(folder,report)
    return report


def files_relative(board, hashes):
    base = board.parent
    result = {}
    for name, digest in hashes.items():
        path = plain_path(name)
        relative = path.relative_to(base)
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Nonregular project file: " + str(path))
        result[relative.as_posix()] = digest
    return result


def copy_exact(board, hashes, directory):
    relatives = files_relative(board, hashes)
    for relative, expected in relatives.items():
        source = board.parent / relative
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            while block := incoming.read(1024 * 1024):
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if sha256(target) != expected:
            raise ValueError("Copied file does not match acceptance: " + relative)
    return relatives


def receipt_base(folder, report, action):
    return {"schema_version": 1, "action": action, "created_at": datetime.now(timezone.utc).isoformat(),
            "task_dir": str(folder), "task_result_sha256": sha256(folder / "result.json"),
            "source_board": report["source_board"], "candidate": report["candidate"],
            "batch_outcome": report.get("batch_outcome"), "counts": report.get("counts"),
            "note": "PCB/rules/library context only, not a schematic export. Full acceptance evidence remains in the task directory."}


def export_result(task_dir, destination):
    folder = Path(task_dir).resolve()
    report = accepted_result(folder)
    source, candidate = Path(report["source_board"]), Path(report["candidate"])
    destination = plain_path(destination)
    if destination.exists():
        raise ValueError("Export destination must be a new directory")
    if folder == destination or folder in destination.parents or candidate.parent in destination.parents:
        raise ValueError("Export must be outside the validation task")
    check_output_location(source, destination)
    receipt = receipt_base(folder, report, "export-result")
    destination.mkdir(exist_ok=False)
    # Failed exports deliberately remain incomplete and never overwrite old data.
    marker = destination / ".pcb-local-incomplete"
    marker.write_text("Export is not complete. Do not treat this folder as accepted.\n")
    hashes = copy_exact(candidate, report["candidate_hashes"], destination)
    if accepted_result(folder) != report:
        raise ValueError("Accepted task changed during export")
    copied = destination / candidate.name
    if files_relative(copied, source_files(copied)) != hashes:
        raise ValueError("Exported project context is not identical to the accepted candidate")
    receipt.update(status="exported", board=str(copied), files=hashes)
    metadata = destination / "pcb-local-export.json"
    with metadata.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    marker.unlink()
    return receipt


def ensure_closed(board):
    # Reject stale locks too. We never delete KiCad locks or guess unsaved state.
    for ext in (".kicad_pcb", ".kicad_pro"):
        file = board.with_suffix(ext)
        for lock in (file.parent / ("~" + file.name + ".lck"), file.parent / (file.name + ".lck")):
            if lock.exists() or lock.is_symlink():
                raise ValueError("原工程仍有 KiCad 锁文件，请先保存/处理未保存修改并关闭原工程，再重试：" + str(lock))


def context_identity(board, hashes):
    files = files_relative(board, hashes)
    normalized = {}
    for name, digest in files.items():
        if name == board.name:
            continue
        if name.startswith(board.stem + ".") and "/" not in name:
            name = "<board>" + name[len(board.stem):]
        normalized[name] = digest
    return normalized


def adopt_result(task_dir, *, original_closed=False):
    if not original_closed:
        raise ValueError("Confirm that the original editor is closed before adopting")
    folder = Path(task_dir).resolve()
    report = accepted_result(folder)
    source, candidate = Path(report["source_board"]), Path(report["candidate"])
    ensure_closed(source)
    if context_identity(source, report["source_hashes"]) != context_identity(candidate, report["candidate_hashes"]):
        raise ValueError("Candidate project context differs; export instead of replacing original rules/libraries")
    guard = source.parent / ("." + source.name + ".pcb-local-adopt.lock")
    backup_root = plain_path(source.parent / ".pcb-local-backups")
    check_output_location(source, backup_root)
    backup = backup_root / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:10])
    staged = None
    receipt = receipt_base(folder, report, "adopt-result")
    # Exclusive guard serializes our own adopters. KiCad locks are rechecked
    # immediately before commit; original editor must stay closed throughout.
    with guard.open("x", encoding="utf-8") as lock:
        lock.write(str(os.getpid()))
    try:
        ensure_closed(source)
        backup.mkdir(parents=True, exist_ok=False)
        originals = copy_exact(source, report["source_hashes"], backup)
        receipt.update(status="prepared", backup_dir=str(backup), backup_board=str(backup / source.name),
                       original_files=originals, replacement_sha256=sha256(candidate))
        if (backup / "pcb-local-adoption.json").exists() or (backup / "pcb-local-adoption.json.tmp").exists():
            raise ValueError("Backup receipt name conflicts with project content")
        write_json(backup / "pcb-local-adoption.json", receipt)
        fd, name = tempfile.mkstemp(prefix="." + source.name + ".pcb-local-", dir=source.parent)
        staged = Path(name)
        with os.fdopen(fd, "wb") as stream, candidate.open("rb") as incoming:
            while block := incoming.read(1024 * 1024):
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(stat.S_IMODE(source.stat().st_mode))
        if sha256(staged) != report["candidate_hashes"][str(candidate)]:
            raise ValueError("Staged PCB differs from accepted candidate")
        if accepted_result(folder) != report:
            raise ValueError("Accepted task changed before adoption")
        ensure_closed(source)
        os.replace(staged, source)
        staged = None
        # From this point replacement is committed. Always return the backup
        # location even if updating the receipt fails; never blindly roll back
        # over a concurrent editor's later changes.
        receipt.update(status="adopted", board=str(source))
        try:
            expected = dict(report["source_hashes"])
            expected[str(source)] = report["candidate_hashes"][str(candidate)]
            if source_files(source) != expected:
                receipt.update(status="adopted_needs_review", warning="Files changed immediately after replacement; inspect original and backup")
            write_json(backup / "pcb-local-adoption.json", receipt)
        except Exception as exc:
            receipt.update(status="adopted_needs_review", warning="Replacement committed; receipt/final check failed: " + str(exc))
        return receipt
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        guard.unlink()
