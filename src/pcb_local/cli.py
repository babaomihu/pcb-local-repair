"""Command-line interface for isolated, independently validated local repair."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time

from . import __version__
from .task_policy import permissions
from .discovery import discover, discover_automatic, select_task, validate_endpoints, region
from .jobs import Job, Halt, write_json
from .workspace import (load_config, runtime_fingerprint, copy_project, source_files,
                        assert_unchanged, validate_config, check_output_location)

PACKAGE = Path(__file__).resolve().parent
EXIT = {"accepted": 0, "complete": 0, "no_progress": 10, "unsupported": 20,
        "rejected": 30, "protection_violation": 31, "verification_incomplete": 40, "error": 50,
        "timed_out": 124, "cancelled": 130}


def report_text(report):
    lines = ["# PCB local repair result", "", "Status: **" + report["status"] + "**", "",
             report.get("reason", ""), "", "Task directory: " + report["task_dir"], "",
             ("Source file integrity: " + str(report.get("source_files_untouched", "not established")) +
              ". Only `accepted` is a successful repair."), ""]
    if "task" in report:
        lines += ["Target: `" + report["task"]["target_net"] + "`", "",
                  "ROI (mm): " + repr(report["task"]["region_mm"]), ""]
    if report.get("candidate"):
        lines += ["Candidate: " + report["candidate"], ""]
    if report.get("candidate_generator"):
        lines += ["Candidate generator: " + report["candidate_generator"], "",
                  "Geometry supplement: " + str(report.get("geometry_fallback", {}).get("status", "disabled")), ""]
    if report.get('automatic_strategy'):
        auto=report['automatic_strategy']
        lines += ['', 'Automatic strategy: '+str(auto.get('selected',auto.get('status'))),
                  'Generation used: %.3f / 60 s; validation used: %.3f / 120 s' % (auto['generation_seconds'],auto['verification_seconds']), '']
        lines += ['- '+a['name']+': '+a['status']+' — '+a.get('reason','') for a in auto['attempts']]
        lines += ['', 'Exact attempts, permissions and evidence: strategy-report.json', '']
    if report.get('zone_refill_choices'):
        lines += ['', '可显式选择的板级铺铜（非自动授权；目标同网、无局部填铜或越界变化仍会拒绝）：', '']
        lines += ['- `{}`：{}；{}；范围 {}'.format(z['uuid'],z['net'],','.join(z['layers']),z['bounds_mm']) for z in report['zone_refill_choices']]
    if report.get('permission_reduction'):
        reduction=report['permission_reduction']
        lines += ['', '## 原走线保持固定', '',
            '按网选出的 %s 段超过原 %s 段修改上限；自动模式已缩减为零原线修改权限，继续尝试补线。没有扩大授权或任意选择部分线段。' %
            (reduction['eligible_track_count'],reduction['requested_max_modified_tracks']), '']
    if report.get('route_quality'):
        from .review_summary import quality_text
        lines += ['', '## 候选走线指标（仅供审查）', '', quality_text(report['route_quality']), report['route_quality']['limitation'], '']
    if report.get('capability_summary'):
        lines += ['', '## 本任务策略范围（不代表已经可布通）', '']
        for row in report['capability_summary']['strategies']:
            lines.append('- {}：{}；层 {}；最多新增孔 {}；原线操作 {}。{}'.format(
                row['name'], row['state'], ','.join(row['route_layers']), row['max_new_vias'], row['original_track_mode'], row['reason']))
        lines += ['', *report['capability_summary']['limitations'], '']
    verification = report.get("verification", {})
    if verification:
        lines += ["## Acceptance checks", ""]
        lines += ["- " + key + ": " + str(value) for key, value in verification.get("checks", {}).items()]
        lines += ["", "## Limitations", ""]
        lines += ["- " + item for item in verification.get("limitations", [])]
        delta = verification.get("drc", {})
        lines += ["", "## Full-board DRC comparison", ""]
        for section, counts in delta.get("sections", {}).items():
            lines.append("- {}: {} → {}; new/changed {}; resolved {}".format(
                section, counts["before_count"], counts["after_count"],
                len(counts["new_or_changed"]), len(counts["resolved"])))
        ignored = [row.get("key", repr(row)) if isinstance(row, dict) else str(row)
                   for row in delta.get("ignored_checks", [])]
        lines += ["", "Project-ignored checks: " + ", ".join(ignored),
                  "", "Individual findings and exact native evidence: " + report.get("verification_directory", str(Path(report["task_dir"]) / "verification")) + "/verification.json and {before,after}/.",
                  "", "## Copper changes", ""]
        supplement = verification.get("supplemental_drc", {})
        if supplement:
            lines += ["Additional strict-copy DRC: " + str(verification.get("checks", {}).get("supplemental_drc_no_new_or_changed")),
                      "Original project rules are unchanged; separately copied projects enable ignored critical checks.", ""]
        for item in report.get("changes", {}).get("added_details", []):
            lines.append("- `{uuid}`: {net}, {layer}, {start_mm} → {end_mm}, width {width_mm} mm".format(**item))
    if report.get("visualization",{}).get("path"):
        lines += ["", "[查看本地修复对比](comparison.html)（只读概览，不能代替原生验收）", ""]
    if "discovery" in report:
        lines += ["", "## Discovered local targets", ""]
        for target in report["discovery"]["targets"]:
            lines.append("- `{}` `{}`: {} ({})".format(target["id"], target["net"],
                         "supported" if target["supported"] else "unsupported",
                         ", ".join(p["reference"] + ":" + p["number"] for p in target["pads"])))
    if report.get('modification_recommendations'):
        lines += ['', '## 修改建议（尚未授权）', '仅显式采纳的建议才授予所列对象权限；完整坐标见 recommendations.json。']
        for proposal in report['modification_recommendations']:
            lines.append('- `{}` 目标 `{}`：{}；原线段 {}；ROI {}'.format(proposal['id'],proposal['target_id'],', '.join(proposal['modification']['nets']),', '.join(proposal['modification']['routing_uuids']),proposal['region_mm']))
    if report.get('diagnosis'):
        from .diagnostics import markdown
        lines.append(markdown(report['diagnosis']))
    return "\n".join(lines) + "\n"


def describe_changes(candidate, protection, before=None):
    """Attach reviewable geometry to the verifier's unchanged acceptance result."""
    from .validation import read_board, field
    changes = dict(protection)
    parsed = read_board(candidate)
    details = []
    for uid in protection.get("added_routing", []):
        node = parsed["routing"][uid]
        detail = {"uuid": uid, "kind": str(node[0]), "net": parsed["identities"][uid]["net"],
                  "start_mm": None, "end_mm": None, "width_mm": None, "layer": None}
        for key, target in (("start", "start_mm"), ("mid", "mid_mm"), ("end", "end_mm"), ("at", "position_mm")):
            value = field(node, key)
            if value:
                detail[target] = [float(v) for v in value[1:3]]
        for key, target in (("width", "width_mm"), ("layer", "layer")):
            value = field(node, key)
            if value:
                detail[target] = float(value[1]) if key == "width" else str(value[1])
        if str(node[0]) == "via":
            detail.update(diameter_mm=float(field(node, "size")[1]), drill_mm=float(field(node, "drill")[1]),
                          layers=list(map(str, field(node, "layers")[1:])))
        details.append(detail)
    changes["added_details"] = details
    if before:
        old = read_board(before)
        changes["removed_details"] = [{"uuid": uid, "net": old["identities"][uid]["net"], "original_geometry": old["routing"][uid]}
            for uid in protection.get("removed_routing", [])]
        changes["changed_details"] = [{"uuid": uid, "net": old["identities"][uid]["net"],
            "original_geometry": old["routing"][uid], "candidate_geometry": parsed["routing"][uid]}
            for uid in protection.get("changed_routing", [])]
    return changes


def preflight(job, config, board, report, preparation_cache=None):
    inspection_dir = job.folder / "inspection"
    argv = [sys.executable, str(PACKAGE / "inspect_worker.py"), "--board", str(board),
            "--out-dir", str(inspection_dir), "--kicad-cli", config["kicad_cli"],
            "--kicad-python", config["kicad_python"]]
    code = job.run("native-preflight", argv, timeout=60)
    inspection = json.loads((inspection_dir / "inspection.json").read_text()) if (inspection_dir / "inspection.json").exists() else {}
    report["preflight"] = inspection
    if code or not inspection.get("complete"):
        raise Halt(inspection.get("status", "verification_incomplete"),
                   "; ".join(inspection.get("errors", ["Native preflight did not complete"])))
    native = inspection["measurement"]["snapshot"]
    if native["version"] != config["verified_kicad_version"]:
        raise Halt("unsupported", "Native KiCad version has not been validated: " + native["version"])
    probe = job.folder / "netclasses.json"
    argv = [config["router_python"], str(PACKAGE / "netclass_probe.py"), "--board", str(board),
            "--output", str(probe), "--native-python", config["kicad_python"],
            "--krt-root", config["krt_root"]]
    producer = lambda: job.run("netclass-preflight", argv, timeout=35)
    code = preparation_cache.run(job,config,board,probe,producer) if preparation_cache else producer()
    if preparation_cache:
        report["preparation_cache"] = json.loads((job.folder/"preparation-cache.json").read_text())
    report["netclasses"] = json.loads(probe.read_text()) if probe.exists() else {}
    if code or not report["netclasses"].get("passed"):
        raise Halt("unsupported", "KRT effective netclass values do not fully match native KiCad; see netclasses.json")
    return native


def snapshot_project(job, source, label):
    folder = job.folder / label
    code = job.run("snapshot-" + label,
        [sys.executable, str(PACKAGE / "context_worker.py"), str(source), str(folder)], timeout=30)
    if code:
        raise Halt("unsupported", "Could not copy the saved project context; see snapshot log")
    return folder / "board.kicad_pcb", json.loads((folder / "library-context.json").read_text())


def workflow(args):
    shared_budget = getattr(args, '_shared_budget', None)
    job = Job(args.out_dir, timeout=min(240, shared_budget.remaining('total')) if shared_budget else 240,
              parent_check=getattr(args, "_parent_check", None))
    report = {"schema_version": 1, "version": __version__, "status": "error",
              "task_dir": str(job.folder), "command": args.command,
              "candidate": None, "accepted": False}
    report['candidate_exclusions'] = sorted(getattr(args, '_candidate_exclusions', set()))
    source, hashes, config, fingerprint, candidate_frozen = None, None, None, None, None
    board, input_frozen = None, None
    automatic = getattr(args, "auto_region", False)
    previous = None
    generation_used = verification_used = 0.
    try:
        if automatic and args.command != "inspect" and (
                not getattr(args, "target", None) or not getattr(args, "inspection", None)):
            raise Halt("unsupported", "Automatic region repair requires --target and --inspection from inspect --auto-region")
        config = load_config(args.config)
        fingerprint = runtime_fingerprint(config)
        if config.get("source_fingerprint") != fingerprint:
            raise Halt("unsupported", "Router source/binary differs from configured fingerprint; review and configure a new runtime file")
        report["runtime"] = config
        source = args.board.expanduser().resolve()
        report["source_board"] = str(source)
        if source.suffix != ".kicad_pcb" or not source.is_file():
            raise Halt("unsupported", "Input must be an existing .kicad_pcb")
        if not source.with_suffix(".kicad_pro").is_file():
            raise Halt("unsupported", "A matching sibling .kicad_pro is required; no default rules are invented")
        check_output_location(source, job.folder)
        hashes = source_files(source, check=job.check)
        report["source_hashes"] = hashes
        if getattr(args, "inspection", None):
            previous = json.loads(args.inspection.read_text(encoding="utf-8"))
            prior_scope = previous.get("discovery", {})
            if (previous.get("status") != "complete" or previous.get("source_board") != str(source)
                    or previous.get("source_hashes") != hashes
                    or (automatic and prior_scope.get("mode") != "automatic-regions")
                    or (not automatic and (prior_scope.get("mode") == "automatic-regions"
                                            or prior_scope.get("region_mm") != args.roi))
                    or prior_scope.get("allowed_layer") != args.layer):
                raise Halt("unsupported", "Inspection is stale or belongs to another input/ROI/layer; inspect again")
            report["inspection_binding"] = str(args.inspection.resolve())
        board, context = snapshot_project(job, source, "input")
        input_frozen = source_files(board)
        report["input_hashes"] = input_frozen
        report["library_context"] = context
        assert_unchanged(source, hashes)
        native = (preflight(job, config, board, report, args._preparation_cache)
                  if getattr(args,"_preparation_cache",None) else preflight(job, config, board, report))
        report['zone_refill_choices']=native.get('zone_refill_choices',[])
        listing = (discover_automatic(native, args.layer, check=job.check) if automatic
                   else discover(native, args.roi, args.layer, check=job.check))
        if automatic and previous is not None:
            old_targets = [t for t in previous["discovery"].get("targets", []) if t.get("id") == args.target]
            new_targets = [t for t in listing["targets"] if t["id"] == args.target]
            if (len(old_targets) != 1 or old_targets != new_targets
                    or previous["discovery"].get("automatic_region_policy") != listing["automatic_region_policy"]):
                raise Halt("unsupported", "Selected automatic region/target changed since inspection; inspect again")
        report["discovery"] = listing
        write_json(job.folder / "targets.json", listing)
        from .recommendations import recommend
        report['modification_recommendations']=recommend(native,listing,check=job.check)
        write_json(job.folder/'recommendations.json',report['modification_recommendations'])
        coverage = report["preflight"]["measurement"]["coverage"]
        if not coverage["complete"]:
            raise Halt("verification_incomplete", "Native DRC coverage is incomplete: " + "; ".join(coverage["reasons"]))
        if args.command == "inspect":
            report.update(status="complete", reason="Native inspection complete; see targets.json and report.md")
        else:
            task = select_task(listing, net=args.net, target=args.target, pads=args.pads)
            if task is None or not validate_endpoints(task, native):
                raise Halt("no_progress", "Requested local pads are already connected, or fewer than two eligible components exist")
            if getattr(args, "route_layers", None) is not None or getattr(args, "max_new_vias", 0):
                task["permissions"] = {"schema_version": 1,
                    "route_layers": args.route_layers or [args.layer],
                    "max_new_vias": args.max_new_vias, "via_type": "through"}
            try:
                permissions(task, native["copper_layers"])
            except ValueError as exc:
                raise Halt("unsupported", str(exc))
            modification_nets = getattr(args, "modify_nets", None) or getattr(args, "reroute_nets", None) or getattr(args, "shove_nets", None)
            local_tracks=getattr(args,'local_track',None)
            if local_tracks and (modification_nets or getattr(args,'accept_recommendation',None) or getattr(args,'max_modified_tracks',0)
                    or (args.command!='verify' and getattr(args,'candidate_strategy',None) not in ('pns','auto'))):
                raise Halt('unsupported','Explicit local-track UUIDs require PNS/auto and cannot mix other modification selections')
            if sum(bool(getattr(args,key,None)) for key in ('modify_nets','reroute_nets','shove_nets')) > 1:
                raise Halt("unsupported", "Choose one explicit modification permission: modify, reroute or shove")
            if getattr(args,'modify_nets',None) and args.command!='verify' and getattr(args,'candidate_strategy',None)!='auto':
                raise Halt('unsupported','Combined modification permission requires --candidate-strategy auto')
            if args.command != "verify" and getattr(args, "shove_nets", None) and getattr(args,"candidate_strategy",None) not in ("pns", "auto"):
                raise Halt("unsupported", "Shove permissions require --candidate-strategy pns")
            if getattr(args, "reroute_nets", None) and getattr(args,"candidate_strategy",None) == "pns":
                raise Halt("unsupported", "PNS uses shove permissions; KRT uses reroute permissions")
            accepted_recommendations=getattr(args,'accept_recommendation',None)
            if accepted_recommendations:
                if modification_nets or getattr(args,'max_modified_tracks',0) or previous is None or not args.target:
                    raise Halt('unsupported','Recommendation acceptance requires its inspection/target and cannot mix net-wide permissions')
                mode=getattr(args,'recommendation_mode','shove')
                strategy=getattr(args,'candidate_strategy','krt+geometry')
                if (mode=='shove' and strategy not in ('auto','pns')) or (mode=='ripup' and strategy=='pns') or (mode=='shove+ripup' and strategy!='auto'):
                    raise Halt('unsupported','Recommendation operation is incompatible with the chosen strategy')
                from .recommendations import apply
                try:
                    report['modification_witness']=apply(native,task,report['modification_recommendations'],previous.get('modification_recommendations',[]),accepted_recommendations,mode)
                except ValueError as exc:raise Halt('unsupported',str(exc))
                report['accepted_recommendation']=accepted_recommendations[0]
            elif modification_nets:
                from .modification import select
                try:
                    if any(net.startswith("-") for net in modification_nets):
                        raise ValueError("Modification net names beginning with '-' are unsupported by the multi-net selector")
                    report["modification_witness"] = select(native, task, modification_nets, args.max_modified_tracks,
                        "shove+ripup" if getattr(args,"modify_nets",None) else "shove" if getattr(args,"shove_nets",None) else "ripup",
                        fixed_fallback=args.command=='repair' and getattr(args,'candidate_strategy',None)=='auto')
                    if task.get('permission_reduction'):
                        report['permission_reduction']=task['permission_reduction']
                except ValueError as exc:
                    raise Halt("unsupported", str(exc))
            elif getattr(args, "max_modified_tracks", 0):
                raise Halt("unsupported", "Track modification budget requires explicit reroute nets")
            if getattr(args, 'refill_zone', None):
                if args.command != 'verify' and args.candidate_strategy not in ('pns', 'auto', 'geometry-multilayer'):
                    raise Halt('unsupported', 'Explicit zone refill search requires pns, auto or geometry-multilayer strategy')
                from .zone_refill import bind_selection
                layered=((args.command=='verify' or getattr(args,'candidate_strategy',None) in ('auto','geometry-multilayer'))
                         and (permissions(task)['route_layers'] != [task['allowed_layer']] or permissions(task)['max_new_vias']))
                try:
                    task['zone_refill']=bind_selection(task,args.refill_zone,native.get('zone_refill_choices',[]),layered)
                    if layered and task.get('modification'):
                        raise ValueError('Layered refill currently keeps all original tracks/vias fixed; remove track-modification selections')
                except ValueError as exc:raise Halt('unsupported',str(exc))
                from .zone_refill import contract as refill_contract
                try:refill_contract(task)
                except ValueError as exc:raise Halt('unsupported', str(exc))
            if local_tracks:
                from .local_segments import select as select_local_segments
                try:report['modification_witness']=select_local_segments(native,task,local_tracks)
                except (ValueError,KeyError) as exc:raise Halt('unsupported',str(exc))
            elif task.get('partial_segments') and all(p.get('schema_version')==2 for p in task['partial_segments']):
                from .local_segments import restrict_for_refill
                try:
                    report['local_middle_selection']=restrict_for_refill(native,task)
                    report['modification_witness']=report['local_middle_selection']['terminal_witness']
                except (ValueError,KeyError) as exc:raise Halt('unsupported',str(exc))
            report["task"] = task
            if automatic:
                task["automatic_region_policy"] = listing["automatic_region_policy"]
            if (args.command == 'repair' and task.get('zone_refill') and task.get('modification') and not local_tracks
                    and not report.get('local_middle_selection')):
                from .refill_strategy import select as select_refill_operations
                write_json(job.folder / 'requested-task.json',task)
                task,selection=select_refill_operations(native,task,partial=True)
                report['task']=task
                report['refill_operation_selection']=selection
                report['modification_witness']=selection['terminal_witness']
                write_json(job.folder / 'refill-operation-selection.json',selection)
            write_json(job.folder / "task.json", task)
            write_json(job.folder / "task-manifest.json", {"cases": [task]})
            if args.command == "repair":
                from .candidate_pipeline import generate
                if getattr(args,'candidate_strategy','krt+geometry') == 'auto':
                    from .auto_strategy import repair
                    try:
                        candidate, auto_code, auto_verification = repair(job,config,board,task,native,report,args.profile,
                            generation_budget=shared_budget.remaining('generation') if shared_budget else 60,
                            verification_budget=shared_budget.remaining('verification') if shared_budget else 120)
                    finally:
                        if shared_budget:
                            ledger=report.get('automatic_strategy',{})
                            shared_budget.charge('generation',ledger.get('generation_seconds',0))
                            shared_budget.charge('verification',ledger.get('verification_seconds',0))
                else:
                    started=time.monotonic()
                    try:
                        candidate = generate(job,config,board,task,native,report,getattr(args,'candidate_strategy','krt+geometry'),args.profile,
                            budget=shared_budget.remaining('generation') if shared_budget else 60)
                    finally:
                        generation_used=time.monotonic()-started
                        if shared_budget:shared_budget.charge('generation',generation_used)
            else:
                candidate, _ = snapshot_project(job, args.candidate.expanduser().resolve(), "candidate")
            report["candidate"] = str(candidate)
            if source_files(board) != input_frozen:
                raise Halt("protection_violation", "Frozen input snapshot changed before validation")
            candidate_frozen = source_files(candidate)
            from .candidate_pipeline import validate
            if args.command == 'repair' and getattr(args,'candidate_strategy','krt+geometry') == 'auto':
                code, verification = auto_code, auto_verification
            else:
                from .candidate_pool import validate_candidates
                if report.get('candidate_pool'):
                    # The pool binds each file before/after verification. A
                    # hard-stop report must refer to the candidate that failed,
                    # not retain the first proposal's hash under a new path.
                    candidate_frozen = None
                started=time.monotonic()
                try:
                    candidate, code, verification = validate_candidates(job,config,board,candidate,task,report,
                        budget=shared_budget.remaining('verification') if shared_budget else 120)
                finally:
                    verification_used=time.monotonic()-started
                    if shared_budget:shared_budget.charge('verification',verification_used)
                report['candidate'] = str(candidate)
                candidate_frozen = source_files(candidate)
            if args.command == 'repair' and config.get('trace_quality', True):
                from .trace_quality import improve
                ledger=report.get('automatic_strategy',{})
                generation_left=(shared_budget.remaining('generation') if shared_budget else
                                 max(0.,60.-ledger.get('generation_seconds',generation_used)))
                verification_left=(shared_budget.remaining('verification') if shared_budget else
                                   max(0.,120.-ledger.get('verification_seconds',verification_used)))
                try:
                    candidate,code,verification=improve(job,config,board,candidate,task,report,code,verification,
                        generation_budget=generation_left,verification_budget=verification_left)
                finally:
                    quality=report.get('trace_quality',{})
                    if shared_budget:
                        shared_budget.charge('generation',quality.get('generation_seconds',0.))
                        shared_budget.charge('verification',quality.get('verification_seconds',0.))
                    report['stage_totals_seconds']=dict(
                        generation=ledger.get('generation_seconds',generation_used)+quality.get('generation_seconds',0.),
                        verification=ledger.get('verification_seconds',verification_used)+quality.get('verification_seconds',0.))
                if report['stage_totals_seconds']['generation']>60. or report['stage_totals_seconds']['verification']>120.:
                    raise Halt('timed_out','Original cumulative stage budget exhausted after quality optimization')
                report['candidate']=str(candidate);candidate_frozen=source_files(candidate)
                if report.get('trace_quality',{}).get('status')=='accepted':
                    report['verification_directory']=report['trace_quality']['verification_directory']
                    from .candidate_pool import fingerprint as proposal_fingerprint
                    report['trace_quality']['final_fingerprint']=proposal_fingerprint(board,candidate,task)
            report["verification"] = verification
            verified_hashes = verification.get("source_hashes_after", {})
            for path, expected in candidate_frozen.items():
                # Rule files/PCB must appear in the verifier's own hash record;
                # library contents are additionally bound by this task ledger
                # and the verifier's independent library-context comparison.
                if Path(path).parent == candidate.parent and Path(path).name.startswith("board."):
                    if verified_hashes.get(path) != expected:
                        raise Halt("protection_violation", "Verification did not bind the expected candidate file: " + path)
            report["changes"] = describe_changes(candidate, verification.get("protection", {}), board)
            write_json(job.folder / "changes.json", report["changes"])
            if not verification.get("verification_complete") or code == 2:
                raise Halt("verification_incomplete", "; ".join(verification.get("errors", [])) or "Independent verification incomplete")
            if not verification.get("accepted") or code != 0:
                checks = verification.get("checks", {})
                failed = [key for key, value in checks.items() if not value]
                protection_checks = set(verification.get("protection", {}).get("checks", {}))
                protection_checks |= {"existing_components_preserved", "source_files_untouched", "zone_refill_scope_preserved"}
                status = ("protection_violation" if protection_checks.intersection(failed) else
                          "no_progress" if failed == ["target_connected"] else "rejected")
                raise Halt(status, "Candidate failed acceptance: " + ", ".join(failed))
            report.update(status="accepted", reason="Target connected; independent native and protection checks all passed")
        job.check()
    except Halt as exc:
        report.update(status=exc.status, reason=str(exc))
    except ValueError as exc:
        report.update(status="unsupported", reason=str(exc))
    except Exception as exc:
        report.update(status="error", reason=type(exc).__name__ + ": " + str(exc))
    finally:
        integrity_errors = []
        for key, path, expected in (("source_files_untouched", source, hashes),
                                    ("input_snapshot_untouched", board, input_frozen)):
            if expected is None:
                continue
            try:
                if source_files(path) != expected:
                    raise ValueError(key + ": file contents or membership changed")
                report[key] = True
            except Exception as exc:
                report[key] = False
                integrity_errors.append(str(exc))
        if config is not None and fingerprint is not None:
            try:
                report["runtime_untouched"] = runtime_fingerprint(config) == fingerprint
                if not report["runtime_untouched"]:
                    raise ValueError("Router source changed while the task was running")
            except Exception as exc:
                report["runtime_untouched"] = False
                integrity_errors.append(str(exc))
        candidate_path = Path(report["candidate"]) if report["candidate"] else job.folder / "engine-output/board.kicad_pcb"
        if candidate_frozen is not None or candidate_path.exists():
            try:
                report["candidate"] = str(candidate_path)
                current = source_files(candidate_path)
                if candidate_frozen is not None and current != candidate_frozen:
                    raise ValueError("Candidate/context changed during or after validation")
                report["candidate_hashes"] = candidate_frozen if candidate_frozen is not None else current
            except Exception as exc:
                report["candidate_hashes"] = candidate_frozen
                integrity_errors.append(str(exc))
        if integrity_errors:
            report.update(status="protection_violation", reason="; ".join(integrity_errors),
                          integrity_errors=integrity_errors)
        if report["status"] in ("accepted", "complete"):
            try:
                job.check()
            except Halt as exc:
                report.update(status=exc.status, reason=str(exc))
        report["accepted"] = report["status"] == "accepted"
        if "routing_transaction" in report:
            report["routing_transaction"]["state"] = "validated_candidate" if report["accepted"] else "not_accepted_original_preserved"
        report["exit_code"] = EXIT[report["status"]]
        try:
            from .review_summary import capabilities, quality
            if report.get('task') and config:
                try: report['capability_summary'] = capabilities(report['task'], config)
                except (KeyError, ValueError) as exc:
                    report['capability_summary'] = dict(display_only=True, strategies=[], limitations=['策略范围摘要不可用：'+str(exc)])
            if report.get('changes'):
                try: report['route_quality'] = quality(report['changes'])
                except (KeyError, ValueError, TypeError) as exc:
                    report['route_quality_unavailable'] = str(exc)
            from .diagnostics import explain
            report['diagnosis']=explain(report)
            write_json(job.folder/'diagnosis.json',report['diagnosis'])
            if board is not None and board.is_file():
                from .visual_report import write_visual_report
                try:report['visualization']=write_visual_report(report,job.folder/'comparison.html',before=board)
                except Exception as exc:report['visualization']={'status':'unavailable','reason':str(exc),'display_only':True}

            (job.folder / "report.md").write_text(report_text(report), encoding="utf-8")
        except Exception as exc:
            report.update(status="error", accepted=False, exit_code=EXIT["error"],
                          reason="Result report could not be delivered: " + repr(exc))
        write_json(job.folder / "result.json", report)
        job.finish(report["status"])
    print(json.dumps({"status": report["status"], "reason": report.get("reason"),
                      "result": str(job.folder / "result.json"), "candidate": report["candidate"]}, ensure_ascii=False))
    return report["exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("configure", help="Record explicit local tool paths and pin router source hashes")
    for name in ("krt-root", "router-python", "kicad-cli", "kicad-python"):
        config.add_argument("--" + name, required=True, type=Path)
    config.add_argument("--pns-python", type=Path, help="Optional independent PNS worker interpreter")
    config.add_argument("--pns-module-dir", type=Path, help="Optional compiled PCBWorld-Engine module directory")
    config.add_argument("--output", required=True, type=Path)
    for name in ("inspect", "repair", "verify", "batch"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, default=Path(".pcb-local.json"))
        sub.add_argument("--board", type=Path, required=True)
        scope = sub.add_mutually_exclusive_group(required=True)
        scope.add_argument("--roi", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
        scope.add_argument("--auto-region", action="store_true",
                           help="Discover targets without coordinates; repair uses the selected target's fixed local box")
        sub.add_argument("--layer", required=True)
        sub.add_argument("--out-dir", type=Path, required=True, help="New directory; never overwritten")
        if name == "batch":
            sub.add_argument("--order", choices=("selected","constrained-first","coordinated","joint"), default="selected", help="Ordering; joint coordinates conflicts and alternative strategies for 2–4 targets within shared per-target budgets")
            sub.add_argument("--targets", nargs="+", required=True, help="Selected inspect target IDs, processed in this order")
            sub.add_argument("--inspection", type=Path, required=True)
            sub.add_argument("--signal-kind", choices=("ordinary-single-ended",), required=True)
        elif name != "inspect":
            group = sub.add_mutually_exclusive_group(required=True)
            group.add_argument("--net", help="Exact net name; all eligible local pad components must connect")
            group.add_argument("--target", help="Connection ID from inspect with the same board/ROI/layer")
            sub.add_argument("--pads", nargs=2, help="Two unique pad UUIDs; use with --net")
            sub.add_argument("--inspection", type=Path,
                             help="Bind this request to a previous inspect result.json and its exact source/ROI/layer")
            sub.add_argument("--signal-kind", choices=("ordinary-single-ended",), required=True,
                             help="Declare design intent; differential/power/length-controlled signals are unsupported")
        if name != "inspect":
            sub.add_argument("--accept-recommendation", nargs="+", help="Explicitly authorize exact track sets from the bound inspection")
            sub.add_argument("--recommendation-mode", choices=("shove","ripup","shove+ripup"), default="shove")
            sub.add_argument("--route-layers", nargs="+", help="Explicit allowed copper layers; through vias require every board copper layer")
            sub.add_argument("--modify-nets", nargs="+", help="Explicit nets eligible for both bounded shove and ripup in auto mode")
            sub.add_argument("--shove-nets", nargs="+", help="Explicit ordinary nets whose eligible tracks PNS may push; requires --candidate-strategy pns")
            sub.add_argument("--reroute-nets", nargs="+", help="Explicit ordinary signal nets whose eligible local straight tracks may be replaced")
            sub.add_argument("--max-modified-tracks", type=int, default=0, help="Maximum original straight tracks authorized for replacement (1-32); default 0")
            sub.add_argument("--max-new-vias", type=int, default=0, help="Maximum new ordinary through vias per connection (0-32); default 0")
        if name in ('repair', 'verify', 'batch'):
            sub.add_argument('--refill-zone', action='append', help='Explicit unlocked foreign-net board zone UUID permitted to refill only inside the ROI; repeat for multiple zones (PNS/auto)')
        if name in ('repair','verify'):
            sub.add_argument('--local-track',action='append',help='Explicit original straight-track UUID: permit only its ROI-internal middle to move, retain exact fixed tails (PNS/auto); repeat up to 32')
        if name in ("repair", "batch"):
            sub.add_argument("--profile", choices=("baseline", "external-pour-check"), default="external-pour-check")
            sub.add_argument("--candidate-strategy", choices=("krt", "krt+geometry", "geometry", "geometry-multilayer", "pns", "auto"), default="krt+geometry",
                             help="Supplement unconnected KRT proposals within the same 60s search budget")
        if name == "verify":
            sub.add_argument("--candidate", required=True, type=Path)
    for name in ("status", "cancel"):
        sub = commands.add_parser(name)
        sub.add_argument("task_dir", type=Path)
    legacy = commands.add_parser("import-legacy", help="Audit and import a supported legacy board into an independent project")
    legacy.add_argument("--board", required=True, type=Path)
    legacy.add_argument("--config", required=True, type=Path)
    legacy.add_argument("--out-dir", required=True, type=Path)
    export = commands.add_parser("export-result", help="Save accepted PCB and project context into a new directory")
    export.add_argument("task_dir", type=Path)
    export.add_argument("--destination", type=Path, required=True)
    adopt = commands.add_parser("adopt-result", help="Back up and replace the unchanged, closed original PCB only")
    adopt.add_argument("task_dir", type=Path)
    adopt.add_argument("--original-closed", action="store_true", required=True,
                       help="Confirm original editor is closed; existing KiCad locks still cause refusal")
    args = parser.parse_args(argv)
    try:
        if args.command == "import-legacy":
            from .legacy_import import workflow as import_workflow
            result = import_workflow(args)
            print(json.dumps(result, ensure_ascii=False))
            return result['exit_code']
        if args.command in ("export-result", "adopt-result"):
            from .delivery import export_result, adopt_result
            value = (export_result(args.task_dir, args.destination) if args.command == "export-result"
                     else adopt_result(args.task_dir, original_closed=args.original_closed))
            print(json.dumps(value, ensure_ascii=False))
            return 0
        if args.command == "configure":
            if args.output.exists():
                raise ValueError("Configuration already exists; choose a new output file")
            data = {key: str(getattr(args, key).expanduser().absolute())
                    for key in ("krt_root", "router_python", "kicad_cli", "kicad_python")}
            if bool(args.pns_python) != bool(args.pns_module_dir):
                raise ValueError("Provide both --pns-python and --pns-module-dir")
            if args.pns_python:
                from .workspace import sha256
                module = args.pns_module_dir.expanduser().absolute()
                data["pns"] = {"python": str(args.pns_python.expanduser().absolute()),
                    "module_dir": str(module), "module_sha256": sha256(module/"kicad_rl_router.so")}
            data.update(schema_version=1, verified_kicad_version="10.0.4")
            from .drc_complete import load_runtime
            drc_runtime=load_runtime(data['kicad_cli'])
            if drc_runtime:data['drc_runtime_manifest_sha256']=drc_runtime['manifest_sha256']
            data = validate_config(data)
            data["source_fingerprint"] = runtime_fingerprint(data)
            if not data["source_fingerprint"]:
                raise ValueError("No KRT sources found")
            write_json(args.output, data)
            print(json.dumps({"config": str(args.output.resolve())}))
            return 0
        if args.command in ("status", "cancel"):
            folder = args.task_dir.resolve()
            state = json.loads((folder / "status.json").read_text())
            if args.command == "cancel" and state["status"] == "running":
                (folder / "cancel.request").touch(exist_ok=True)
                state["cancellation_requested"] = True
            if state["status"] == "running":
                try:
                    os.kill(state["pid"], 0)
                except ProcessLookupError:
                    state.update(status="interrupted", reason="Supervisor no longer exists; never accept residual output")
            elif (folder / "result.json").exists():
                result = json.loads((folder / "result.json").read_text())
                state["candidate"] = result.get("candidate")
                state["accepted"] = result.get("accepted", False)
                if result.get('command') == 'import-legacy' and state.get('status') == 'complete':
                    try:
                        from .legacy_import import check_imported
                        check_imported(result)
                        state.update(imported_board=result['imported_board'], import_verified=True, accepted=False)
                    except (ValueError, OSError, KeyError) as exc:
                        state.update(status='stale_result', import_verified=False, accepted=False, reason=str(exc))
                if state["accepted"]:
                    try:
                        if result.get("command") == "batch":
                            from .batch import check_chain
                            check_chain(result)
                        assert_unchanged(Path(result["source_board"]), result["source_hashes"])
                        if source_files(Path(result["candidate"])) != result["candidate_hashes"]:
                            raise ValueError("Candidate/context changed since acceptance")
                        from .delivery import check_fill_evidence
                        check_fill_evidence(folder,result)
                    except (ValueError, OSError) as exc:
                        state.update(status="stale_result", accepted=False, reason=str(exc))
            print(json.dumps(state, ensure_ascii=False))
            return 0
        if not args.auto_region:
            region(args.roi)
        if getattr(args, "pads", None) and getattr(args, "target", None):
            parser.error("--pads must be used with --net, never with --target")
        if args.command == "batch":
            from .batch import batch_workflow
            return batch_workflow(args)
        return workflow(args)
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 50


if __name__ == "__main__":
    raise SystemExit(main())
