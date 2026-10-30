"""Geometric proposals within one original generation budget.

Auto uses a separate frozen-input attempt. Explicit krt+geometry may supplement
an unconnected proposal. Neither mode accepts a board without full validation.
"""
import json
from pathlib import Path
import time

from .jobs import Halt
from .workspace import source_files
from .validation import connectivity_delta

PACKAGE = Path(__file__).resolve().parent


def _supplement(job, config, board, candidate, task, native, report, deadline, independent=False, multilayer=False):
    evidence = {"status": "not_attempted", "shared_search_budget_seconds": 60,
                "mode": "independent_frozen_input" if independent else "krt_supplement",
                "generation_allocation_seconds": max(0,deadline-time.monotonic())}
    report["geometry_fallback"] = evidence
    report["candidate_generator"] = "geometry-visibility" if independent else "krt"

    def finish(value):
        if independent:
            status=evidence['status']
            terminal={'unsupported':'unsupported','no_path':'no_progress','bounded_out':'no_progress',
                      'budget_exhausted':'timed_out'}
            if status in terminal:
                raise Halt(terminal[status], 'Independent geometry '+status+': '+str(evidence.get('reason','see geometry evidence')))
            if status!='found':raise Halt('error','Unexpected geometry result: '+str(status))
        return value

    def remaining():
        job.check()
        return deadline - time.monotonic()

    def run(label, argv):
        budget = remaining()
        if budget <= 1:
            evidence.update(status="budget_exhausted")
            return False
        evidence['phase']=label
        try:
            code = job.run(label, argv, timeout=budget)
        except Halt as exc:
            evidence.update(status='budget_exhausted' if exc.status=='timed_out' else exc.status,reason=str(exc))
            raise
        if code:
            evidence.update(status="error",reason=label+" failed with code "+str(code))
            raise Halt("error", label + " failed; see subprocess log")
        return True

    model_path = job.folder / "geometry-model.json"
    model_args = [config["kicad_python"], str(PACKAGE / "geometry_snapshot.py"),
            "--board", str(board), "--task", str(job.folder / "task.json"), "--output", str(model_path)]
    if multilayer:model_args.append("--multilayer-model")
    measured = report.get("preflight", {}).get("measurement", {})
    if measured.get("refilled_board") and measured.get("refilled_sha256"):
        model_args += ["--refilled-board", measured["refilled_board"],
                       "--refilled-sha256", measured["refilled_sha256"]]
    if not run("geometry-model", model_args):
        return finish(candidate)
    model = json.loads(model_path.read_text())
    if model.get("status") != "ready":
        evidence.update(status="unsupported", reason=model.get("reason"),
                        unsupported_items=model.get("unsupported_items",[]))
        raw=model.get('outline_diagnostics_raw')
        if raw:
            raw_path=job.folder/'geometry-outline-raw.json'
            raw_path.write_text(json.dumps(raw,indent=2)+'\n')
            output=job.folder/'geometry-outline-diagnosis.json'
            left=remaining()
            if left<=1:
                evidence['outline_diagnostics']={'status':'budget_exhausted','acceptance_authorized':False}
            else:
                try:
                    code=job.run('geometry-outline-diagnosis',[config['router_python'],
                        str(PACKAGE/'outline_diagnostics.py'),'--analyze',str(raw_path),'--output',str(output)],timeout=left)
                    evidence['outline_diagnostics']=(json.loads(output.read_text()) if output.exists()
                        else {'status':'unavailable','reason':'Diagnostic worker did not produce a report','acceptance_authorized':False})
                    evidence['outline_diagnostics']['worker_exit_code']=code
                except Halt as exc:
                    if exc.status in ('cancelled','protection_violation','verification_incomplete'):raise
                    evidence['outline_diagnostics']={'status':'budget_exhausted' if exc.status=='timed_out' else 'unavailable',
                        'reason':str(exc),'acceptance_authorized':False}
                except Exception as exc:
                    evidence['outline_diagnostics']={'status':'unavailable','reason':str(exc),'acceptance_authorized':False}
        return finish(candidate)

    if not independent:
        # Connectivity only chooses a proposal; it never replaces refill/DRC. A
        # cached-fill false positive merely keeps KRT for full validation. A false
        # negative may select another proposal but never bypasses acceptance.
        probe = job.folder / "krt-connectivity.json"
        frozen = source_files(candidate)
        if not run("krt-connectivity", [config["kicad_python"], str(PACKAGE / "native_snapshot.py"),
                                        str(candidate), str(probe)]):
            return finish(candidate)
        if source_files(candidate) != frozen:
            raise Halt("protection_violation", "Connectivity probe changed the KRT candidate")
        delta = connectivity_delta(native, json.loads(probe.read_text()), task)
        evidence["krt_connectivity"] = delta
        if not delta["existing_components_preserved"] or not delta["target_pad_identities_valid"]:
            raise Halt("protection_violation", "KRT candidate lost original connectivity or endpoint identity")
        if delta["target_connected"]:
            evidence.update(status="not_needed")
            return finish(candidate)
    budget = remaining()
    if budget <= 1:
        evidence.update(status="budget_exhausted")
        return finish(candidate)
    folder = job.folder / "geometry-search"
    if not run("geometry-search", [config["router_python"], str(PACKAGE / ("multilayer_geometry.py" if multilayer else "geometry_search.py")),
            "--model", str(model_path), "--board", str(board), "--out-dir", str(folder),
            "--timeout", str(max(.01, budget - .5))]):
        return finish(candidate)
    result = json.loads((folder / "search.json").read_text())
    evidence.update(result)
    if not result.get("input_unchanged"):
        raise Halt("protection_violation", "Geometric search changed its input")
    if result["status"] == "found":
        proposed = Path(result["candidate_board"])
        if source_files(proposed) != result["candidate_hashes"]:
            raise Halt("protection_violation", "Geometric candidate changed before validation")
        report["candidate_generator"] = "geometry-multilayer" if multilayer else "geometry-visibility"
        return proposed
    return finish(candidate)


def supplement(job, config, board, candidate, task, native, report, deadline, independent=False, multilayer=False):
    frozen=source_files(board)
    try:
        return _supplement(job,config,board,candidate,task,native,report,deadline,independent,multilayer)
    finally:
        if source_files(board)!=frozen:
            raise Halt('protection_violation','Geometric generation changed its frozen baseline')
