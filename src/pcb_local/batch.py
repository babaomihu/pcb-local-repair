"""Sequential, explicitly selected repairs; only accepted children advance input."""
from argparse import Namespace
import copy
import hashlib
import json
import math
from pathlib import Path

from . import __version__
from .jobs import Job, Halt, write_json
from .workspace import source_files, assert_unchanged, check_output_location


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _refill_definitions(board, selected):
    """Bind authored top-level zones; regenerated fill caches are not definitions."""
    from .validation import parse_sexpr, name, field, canonical, semantic_string, Quoted
    roots = parse_sexpr(Path(board).read_text())
    if len(roots) != 1 or name(roots[0]) != 'kicad_pcb':
        raise ValueError('Invalid batch source board')
    root = roots[0]
    net_names = {str(n[1]): semantic_string(n[2]) for n in root[1:] if name(n) == 'net' and len(n) > 2}
    result = {}
    for uid in selected:
        zones = [n for n in root[1:] if name(n) == 'zone' and field(n, 'uuid')
                 and str(field(n, 'uuid')[1]) == uid]
        if len(zones) != 1:
            raise ValueError('Selected refill zone is missing, ambiguous, or not a board zone: ' + uid)
        zone = zones[0]; locked = field(zone, 'locked'); net = field(zone, 'net')
        if ('locked' in zone or locked and (len(locked) == 1 or locked[1] != 'no')
                or field(zone, 'keepout') is not None or net is None):
            raise ValueError('Selected refill zone is locked or is a rule area: ' + uid)
        net_name = semantic_string(net[1]) if isinstance(net[1], Quoted) else net_names.get(str(net[1]))
        if not net_name:
            raise ValueError('Selected refill zone has no identifiable net: ' + uid)
        result[uid] = dict(net=net_name, locked=False,
            definition_sha256=hashlib.sha256(json.dumps(canonical(zone)).encode()).hexdigest())
    return result


def bind_refill_selection(args, inspection, board, targets, selected):
    """Read-only necessary prechecks; every child still needs native fill proof."""
    if not selected:
        return None
    if (not isinstance(selected, list) or any(not isinstance(x, str) or not x for x in selected)
            or len(selected) != len(set(selected))):
        raise ValueError('Batch refill requires unique explicit zone UUIDs')
    route_layers=getattr(args,'route_layers',None) or [args.layer]
    via_limit=getattr(args,'max_new_vias',0)
    layered=route_layers != [args.layer] or bool(via_limit)
    if (getattr(args, 'candidate_strategy', None) not in (('auto','geometry-multilayer') if layered else ('pns','auto'))
            or getattr(args, 'signal_kind', None) != 'ordinary-single-ended'):
        raise ValueError('Batch refill requires ordinary targets; layered refill uses auto/geometry-multilayer with fixed original routing')
    native = inspection.get('preflight', {}).get('measurement', {}).get('snapshot')
    if not isinstance(native, dict) or args.layer not in native.get('copper_layers', []):
        raise ValueError('Batch refill needs the bound native snapshot and layer')
    choices = native.get('zone_refill_choices', [])
    displayed = inspection.get('zone_refill_choices', [])
    definitions = _refill_definitions(board, selected)
    bindings = []
    rules = native.get('routing_rules', {})
    for uid in selected:
        matching = [z for z in choices if z.get('uuid') == uid]
        if len(matching) != 1 or [z for z in displayed if z.get('uuid') == uid] != matching:
            raise ValueError('Selected refill zone differs from the bound inspection choices: ' + uid)
        zone = matching[0]
        if (zone.get('net') != definitions[uid]['net'] or not set(route_layers).intersection(zone.get('layers', []))
                or zone.get('locked', False) or zone.get('source_owner_uuid') is not None):
            raise ValueError('Selected refill zone identity/layer/owner is unsupported: ' + uid)
        for key in ('local_clearance_mm', 'min_thickness_mm'):
            if type(zone.get(key)) not in (int, float) or not math.isfinite(zone[key]) or zone[key] < 0:
                raise ValueError('Missing native refill rule: ' + key)
        bounds = zone.get('bounds_mm')
        if (not isinstance(bounds, list) or len(bounds) != 4
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in bounds)
                or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
            raise ValueError('Invalid refill bounds in native inspection')
        net_rules = rules.get('netclasses_mm', {}).get(zone['net'])
        if not isinstance(net_rules, dict) or not isinstance(rules.get('board_minima_mm'), dict):
            raise ValueError('Missing native zone netclass or board minima')
        bindings.append(dict(uuid=uid, **definitions[uid], layers=list(zone['layers']),
            local_clearance_mm=zone['local_clearance_mm'], min_thickness_mm=zone['min_thickness_mm'],
            effective_netclass=copy.deepcopy(net_rules), board_minima=copy.deepcopy(rules['board_minima_mm'])))
    from .discovery import validate_endpoints, region
    from .zone_refill import contract as refill_contract
    for target in targets:
        pads = target.get('pad_uuids', [])
        ends = target.get('endpoints')
        if ends is None and (len(pads) != 2 or len(set(pads)) != 2):
            raise ValueError('Batch refill requires two distinct original pads per target')
        roi = region(target['region_mm'] if args.auto_region else args.roi)
        task = dict(target_net=target['net'], target_pad_uuids=pads,
                    allowed_layer=args.layer, region_mm=roi,
                    zone_refill=dict(schema_version=1, zone_uuids=list(selected)))
        from .zone_refill import bind_selection
        task['permissions']=dict(schema_version=1,route_layers=route_layers,max_new_vias=via_limit,via_type='through')
        task['zone_refill']=bind_selection(task,selected,choices,layered)
        if ends is not None:
            if not isinstance(ends, list) or len(ends) != 2:
                raise ValueError('Batch refill requires two bound original endpoints')
            task.update(target_endpoints=copy.deepcopy(ends),
                        target_item_ids=[e['item_id'] for e in ends],
                        target_pad_uuids=[e['uuid'] for e in ends if e['kind'] == 'pad'])
        refill_contract(task)
        validate_endpoints(task, native)
        for uid in selected:
            zone = next(z for z in choices if z['uuid'] == uid)
            if zone['net'] == target['net']:
                raise ValueError('Selected zone is not foreign to every batch target: ' + uid)
            box = zone['bounds_mm']
            if max(box[0], roi[0]) >= min(box[2], roi[2]) or max(box[1], roi[1]) >= min(box[3], roi[3]):
                raise ValueError('Selected zone bounds do not intersect every target ROI: ' + uid)
    return dict(schema_version=1, zone_uuids=list(selected), zones=bindings, permission=task['zone_refill'],
                bounds_precheck_only=True, native_filled_intersection_required_per_child=True)


def check_chain(report):
    """Recheck all intermediate accepted inputs/candidates, not only the last."""
    assert_unchanged(Path(report["source_board"]), report["source_hashes"])
    current = report["source_board"]
    chain = report.get("accepted_chain", [])
    if not chain:
        raise ValueError("Batch has no accepted repair chain")
    for link in chain:
        path = Path(link["result"])
        if file_hash(path) != link["sha256"]:
            raise ValueError("Accepted child report changed: " + str(path))
        child = read(path)
        if (child.get("status") != "accepted" or child.get("accepted") is not True
                or child.get("exit_code") != 0 or child["source_board"] != current):
            raise ValueError("Broken accepted repair chain")
        if report.get('zone_refill') and child.get('task', {}).get('zone_refill') != report['zone_refill']:
            raise ValueError('Accepted child lost the explicitly selected refill zones')
        assert_unchanged(Path(current), child["source_hashes"])
        if source_files(Path(child["candidate"])) != child["candidate_hashes"]:
            raise ValueError("Intermediate candidate/context changed")
        current = child["candidate"]
    if current != report["candidate"]:
        raise ValueError("Final candidate differs from accepted chain")


def match_target(original, listing, native):
    """Never substitute a newly offered connection for the user's selection."""
    matches = [x for x in listing["targets"] if x["id"] == original["id"]]
    if len(matches) == 1 and matches[0] == original and original.get("supported"):
        return "ready"
    components = []
    if original.get('endpoints'):
        ids=[e['item_id'] for e in original['endpoints']]
        if not all(i in native['items'] and native['items'][i]['net']==original['net'] for i in ids):
            return 'target_changed'
        return 'already_connected' if len({native['component_of'][i] for i in ids})==1 else 'target_changed'
    for uid in original["pad_uuids"]:
        rows = [(k, x) for k, x in native["items"].items()
                if x["kind"] == "pad" and x["uuid"] == uid and x["net"] == original["net"]]
        if len(rows) != 1:
            return "target_changed"
        components.append(native["component_of"][rows[0][0]])
    return "already_connected" if len(set(components)) == 1 else "target_changed"


def batch_text(report):
    lines = ["# PCB Local 批量修复", "", "状态：" + report["status"],
             "队列结果：" + report.get("batch_outcome", "stopped"), report.get("reason", ""), "",
             "原板：" + report.get("source_board", ""),
             "最终已验收候选：" + str(report.get("candidate")), "",
             "| 目标 | 网络 | 状态 | 原因 |", "|---|---|---|---|"]
    for row in report["items"]:
        values = [row["target"]["id"], row["target"]["net"], row["status"], row.get("reason", "")]
        lines.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |")
    for row in report['items']:
        if row.get('permission_reduction'):
            lines += ['', row['target']['id']+': 原走线选择数量超过上限，自动缩减为零原线修改权限；未扩大授权。']
        if row.get('diagnosis'):
            lines += ['',row['target']['id']+': '+row['diagnosis']['summary'],row['diagnosis']['suggestion'],
                      '完整对象、DRC 与策略证据：'+str(row.get('task_dir'))+'/report.md']
    lines += ["", "各项任务路径、原生验收与累积链见 result.json；accepted 表示候选已验收，队列可能仅部分完成。",
              "取消或安全检查中止后不会自动打开候选；此前已验收的独立子任务仍保留。", ""]
    return "\n".join(lines)


def batch_workflow(args):
    from .cli import workflow, EXIT
    ids = args.targets
    if not 1 <= len(ids) <= 64 or len(set(ids)) != len(ids):
        raise ValueError("Select 1–64 unique target IDs")
    job = Job(args.out_dir, timeout=len(ids) * 480 + 30)
    source = args.board.expanduser().resolve()
    report = {"schema_version": 1, "version": __version__, "command": "batch",
              "task_dir": str(job.folder), "source_board": str(source), "source_hashes": {},
              "status": "error", "accepted": False, "candidate": None,
              "accepted_chain": [], "items": [], "batch_outcome": "stopped"}
    joint = None
    from .preparation_cache import NetclassPreparationCache
    preparation_cache=NetclassPreparationCache()
    try:
        if getattr(args, 'order', 'selected') == 'joint':
            from .joint_repair import JointCoordinator
            joint = JointCoordinator(ids, getattr(args, 'candidate_strategy', None))
            report['joint_repair'] = joint.evidence
        check_output_location(source, job.folder)
        hashes = source_files(source, check=job.check)
        report["source_hashes"] = hashes
        previous = read(args.inspection)
        listing = previous.get("discovery", {})
        if (previous.get("status") != "complete" or not previous.get("source_files_untouched")
                or previous.get("source_board") != str(source) or previous.get("source_hashes") != hashes
                or listing.get("allowed_layer") != args.layer
                or (args.auto_region and listing.get("mode") != "automatic-regions")
                or (not args.auto_region and (listing.get("mode") == "automatic-regions" or listing.get("region_mm") != args.roi))):
            raise Halt("unsupported", "Batch inspection is stale or belongs to another input/scope/layer")
        for uid in ids:
            matches = [t for t in listing["targets"] if t["id"] == uid and t.get("supported")]
            if len(matches) != 1:
                raise Halt("unsupported", "Selected target is absent or unsupported: " + uid)
            report["items"].append({"target": matches[0], "status": "pending"})
        refill_ids = getattr(args, 'refill_zone', None) or []
        try:
            refill_binding = bind_refill_selection(args, previous, source,
                [row['target'] for row in report['items']], refill_ids)
        except (ValueError, KeyError, TypeError) as exc:
            raise Halt('unsupported', str(exc))
        if refill_binding:
            report['zone_refill'] = refill_binding['permission']
            report['zone_refill_binding'] = refill_binding
        requested=getattr(args,'accept_recommendation',None) or []
        recommendations=previous.get('modification_recommendations',[])
        selected_proposals=[p for p in recommendations if p['id'] in requested]
        if len(selected_proposals)!=len(requested) or len(requested)!=len(set(requested)) or any(p['target_id'] not in ids for p in selected_proposals) or len({p['target_id'] for p in selected_proposals})!=len(selected_proposals):
            raise Halt('unsupported','Recommendation selection must identify at most one reviewed proposal per selected target')
        report['reviewed_recommendations']=selected_proposals
        report['recommendation_mode']=getattr(args,'recommendation_mode','shove')
        report['requested_order']=list(ids)
        if getattr(args,'order','selected')=='constrained-first':
            from .queue_order import order_targets
            ordered,evidence=order_targets([r['target'] for r in report['items']],previous['preflight']['measurement']['snapshot'],args.layer)
            by_id={r['target']['id']:r for r in report['items']}
            report['items']=[by_id[t['id']] for t in ordered];report['ordering']=evidence
        report['execution_order']=[r['target']['id'] for r in report['items']]
        write_json(job.folder / "request.json", {"inspection": str(args.inspection.resolve()),
                   "targets": [row["target"] for row in report["items"]], "source_hashes": hashes,
                   "zone_refill": report.get('zone_refill'), "zone_refill_binding": refill_binding,
                   "reviewed_recommendations": selected_proposals, "recommendation_mode": report["recommendation_mode"]})
        current = source
        index = 1
        serial = 0
        while index <= len(report['items']):
            row = report['items'][index-1]
            serial += 1
            checkpoint = joint.before(report, index, current) if joint else None
            job.check()
            assert_unchanged(source, hashes)
            if report["accepted_chain"]:
                check_chain(report)
            job.record.update(phase="item-%d-of-%d" % (index, len(ids)), current_index=index, total=len(ids),
                              target_id=row["target"]["id"])
            job.save()
            row["status"] = "inspecting"
            write_json(job.folder / "queue.json", report)
            inspect_dir = job.folder / ("%02d-inspect" % serial)
            common = dict(vars(args), board=current, _parent_check=job.check, _preparation_cache=preparation_cache, net=None, pads=None, target=None)
            inspect_args = Namespace(**dict(common, command="inspect", out_dir=inspect_dir, inspection=None, refill_zone=None))
            code = joint.call(workflow, inspect_args, row['target']['id']) if joint else workflow(inspect_args)
            fresh = read(inspect_dir / "result.json")
            row["inspection"] = str(inspect_dir)
            job.check()
            if code or fresh.get("status") != "complete":
                row.update(status=fresh["status"], reason=fresh.get("reason", "Inspection failed"))
                raise Halt(fresh["status"], "Queue stopped: " + row["reason"])
            native = fresh["preflight"]["measurement"]["snapshot"]
            if getattr(args,'order','selected')=='coordinated':
                # Reuse this required inspection. Only reorder remaining
                # original selected tasks; never add an attempt or authority.
                from .queue_coordination import choose_next
                pending=report['items'][index-1:]
                ready=[item['target'] for item in pending
                       if match_target(item['target'],fresh['discovery'],native)=='ready']
                if ready:
                    chosen,decision=choose_next(ready,native,args.layer,
                        region_mm=None if args.auto_region else args.roi,check=job.check)
                    offset=next(i for i,item in enumerate(pending) if item['target']['id']==chosen['id'])
                    selected_row=report['items'].pop(index-1+offset)
                    report['items'].insert(index-1,selected_row)
                    if selected_row is not row:row['status']='pending'
                    row=selected_row;row.update(status='inspecting',inspection=str(inspect_dir))
                    decision['inspection']=str(inspect_dir)
                    report.setdefault('coordination',[]).append(decision)
                    report['execution_order']=[r['target']['id'] for r in report['items']]
                    job.record['target_id']=row['target']['id'];job.save()
            if refill_binding:
                try:
                    rebound = bind_refill_selection(args, fresh, current, [row['target']], refill_ids)
                    if rebound != refill_binding:
                        raise ValueError('Selected refill zone definitions, identities or rules changed')
                except (ValueError, KeyError, TypeError) as exc:
                    row.update(status='protection_violation', reason=str(exc))
                    raise Halt('protection_violation', 'Queue stopped: ' + str(exc))
            disposition = match_target(row["target"], fresh["discovery"], native)
            if (args.auto_region and listing.get("automatic_region_policy") != fresh["discovery"].get("automatic_region_policy")):
                disposition = "target_changed"
            if disposition != "ready":
                row.update(status=disposition, reason=("Selected endpoints are already connected" if disposition == "already_connected"
                           else "Original selected connection no longer offered unchanged; no replacement attempted"))
                index += 1
                continue
            reviewed = [p for p in selected_proposals if p['target_id'] == row['target']['id']]
            for proposal in reviewed:
                renewed = [p for p in fresh.get('modification_recommendations',[]) if p['id'] == proposal['id']]
                if renewed != [proposal]:
                    row.update(status='unsupported', reason='Reviewed recommendation changed; inspect and explicitly approve the new exact object set')
                    raise Halt('unsupported', 'Queue stopped: '+row['reason'])
            repair_dir = job.folder / ("%02d-repair" % serial)
            row["status"] = "repairing"
            write_json(job.folder / "queue.json", report)
            repair_args = Namespace(**dict(common, command="repair", out_dir=repair_dir,
                                          refill_zone=list(refill_ids) or None,
                                          accept_recommendation=[p["id"] for p in selected_proposals if p["target_id"]==row["target"]["id"]] or None,
                                          target=row["target"]["id"], inspection=inspect_dir / "result.json"))
            code = joint.call(workflow, repair_args, row['target']['id']) if joint else workflow(repair_args)
            child = read(repair_dir / "result.json")
            row.update(status=child["status"], reason=child.get("reason", ""), task_dir=str(repair_dir), diagnosis=child.get("diagnosis"),
                       refill_operation_selection=child.get('refill_operation_selection'),
                       permission_reduction=child.get('permission_reduction'))
            if refill_binding and (child.get('task') is not None or child.get('accepted')):
                if child.get('task', {}).get('zone_refill') != report['zone_refill']:
                    row.update(status='protection_violation',
                               reason='Child task lost the explicit batch refill-zone selection')
                    raise Halt('protection_violation', row['reason'])
            if code == 0 and child["status"] == "accepted" and child.get("accepted") is True:
                current = Path(child["candidate"])
                report["candidate"] = str(current)
                report["accepted_chain"].append({"result": str(repair_dir / "result.json"),
                                               "sha256": file_hash(repair_dir / "result.json")})
                check_chain(report)
            elif child["status"] not in {"no_progress", "unsupported", "rejected"}:
                stopped = child["status"]
                if stopped not in EXIT or stopped in {"accepted", "complete"}:
                    stopped = "error"
                    row.update(status='error',reason='Child exit code and reported status disagree; candidate not accepted')
                raise Halt(stopped, "Queue stopped: " + row["reason"])
            if joint:
                joint.record(checkpoint, report, child)
                if child['status'] in {'no_progress', 'unsupported', 'rejected'}:
                    retry = joint.backtrack(report)
                    if retry is not None:
                        index, previous_board = retry
                        current = Path(previous_board)
                        continue
            index += 1
        job.check()
        if joint:
            joint.finish(report)
            if report['candidate']:
                current = Path(report['candidate'])
        done = all(row["status"] in {"accepted", "already_connected"} for row in report["items"])
        report.update(status="accepted" if report["accepted_chain"] else "no_progress",
                      batch_outcome="all_selected_resolved" if done else "partial",
                      reason=("Bounded joint candidate search finished; consult branch and budget evidence" if joint
                              else "Sequential queue finished; consult each selected item's status"))
    except Halt as exc:
        report.update(status=exc.status, reason=str(exc))
    except Exception as exc:
        report.update(status="error", reason=type(exc).__name__ + ": " + str(exc))
    finally:
        if joint and report['status'] not in ('accepted', 'no_progress'):
            joint.evidence['status'] = report['status']
        try:
            if report["source_hashes"]:
                assert_unchanged(source, report["source_hashes"])
                report["source_files_untouched"] = True
            if report["accepted_chain"]:
                check_chain(report)
                report["candidate_hashes"] = source_files(Path(report["candidate"]))
            if report["status"] == "accepted":
                if not report["accepted_chain"]:
                    raise ValueError("Cannot accept a batch without a validated candidate chain")
                job.check()
        except Halt as exc:
            report.update(status=exc.status, reason=str(exc))
        except Exception as exc:
            report.update(status="protection_violation", reason=str(exc), source_files_untouched=False)
        for row in report["items"]:
            if row["status"] in {"pending", "inspecting", "repairing"}:
                row.update(status="not_run", reason="Queue stopped before this item completed")
        report["accepted"] = report["status"] == "accepted"
        report["exit_code"] = EXIT.get(report["status"], 50)
        report["counts"] = {key: sum(row["status"] == key for row in report["items"])
                            for key in sorted({row["status"] for row in report["items"]})}
        try:
            if source.is_file():
                from .visual_report import write_visual_report
                report['visualization']=write_visual_report(report,job.folder/'comparison.html')
        except Exception as exc:
            report['visualization']={'status':'unavailable','reason':str(exc),'display_only':True}
        visual_link='\n[查看修复对比](comparison.html)\n' if report.get('visualization',{}).get('path') else ''
        (job.folder / "report.md").write_text(batch_text(report)+visual_link, encoding="utf-8")
        write_json(job.folder / "result.json", report)
        job.finish(report["status"])
    print(json.dumps({"status": report["status"], "batch_outcome": report["batch_outcome"],
                      "counts": report["counts"], "result": str(job.folder / "result.json")}, ensure_ascii=False))
    return report["exit_code"]
