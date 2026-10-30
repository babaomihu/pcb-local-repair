"""Bounded PNS orchestration; every proposal remains subject to the common gate."""
import json
import math
from pathlib import Path
import time
from .jobs import Halt, write_json
from .pns_adapter import make_problem, compose
from .task_policy import permissions
from .validation import protection, read_board, connectivity_delta
from .workspace import copy_project

PACKAGE = Path(__file__).resolve().parent


def optional_guidance(job, config, source, output, deadline):
    """A failed optional planner cannot replace the original native strategies."""
    def check():
        job.check()
        if time.monotonic() >= deadline:
            raise Halt('timed_out', 'PNS shared search budget exhausted during cursor guidance')
    check()
    allowance=min(5., deadline-time.monotonic())
    try:
        code=job.run('pns-guidance', [config['router_python'],str(PACKAGE/'pns_guidance.py'),
            '--source',str(source),'--output',str(output),'--timeout',str(allowance)], timeout=allowance)
        check()
        if code:
            raise ValueError('Optional guidance worker exited with code %s'%code)
        value=json.loads(output.read_text())
        if not isinstance(value,dict) or value.get('status') not in ('ready','no_guidance','unavailable'):
            raise ValueError('Invalid optional guidance response')
        plans=value.get('plans')
        if not isinstance(plans,list) or len(plans)>2:
            raise ValueError('Invalid bounded guidance plan list')
        used=set()
        for plan in plans:
            if (not isinstance(plan,dict) or plan.get('policy') not in ('guide-low','guide-high')
                    or plan['policy'] in used or plan.get('fixed_space_witness') is not True
                    or not isinstance(plan.get('waypoints'),list) or len(plan['waypoints'])!=2
                    or any(not isinstance(point,list) or len(point)!=2 or
                           any(type(x) not in (int,float) or not math.isfinite(x) for x in point)
                           for point in plan['waypoints'])):
                raise ValueError('Malformed optional guidance geometry')
            used.add(plan['policy'])
        if plans and value['status']!='ready':
            raise ValueError('Non-ready guidance supplied plans')
        return value
    except Halt as exc:
        check()
        if exc.status not in ('timed_out','error','unsupported'):
            raise
        reason=exc.status+': '+str(exc)
    except (OSError,ValueError,TypeError,OverflowError) as exc:
        check()
        reason=type(exc).__name__+': '+str(exc)
    value=dict(status='unavailable',plans=[],reason=reason,
               fallback='Original direct/incremental/edge plans retained',hard_timeout_seconds=allowance)
    try:
        write_json(output,value)
    except OSError as exc:
        # The outer report still retains this diagnosis if the optional
        # worker's output path is unusable; native fallback remains available.
        value['evidence_write_error']=str(exc)
    return value


def generate(job, config, board, task, native, report, budget=60):
    settings = config.get("pns")
    if not settings:
        raise Halt("unsupported", "PNS runtime is not configured; see advanced setup instructions")
    policy = permissions(task)
    if policy["route_layers"] != [task["allowed_layer"]] or policy["max_new_vias"]:
        raise Halt("unsupported", "Initial PNS integration is single-layer shove; use KRT for authorized multilayer/vias")
    deadline = time.monotonic() + budget
    report["candidate_generator"] = "pcbworld-pns-shove"
    evidence = {"status": "running", "attempts": [], "budget_seconds": budget,
                "policy": "Native single-layer problem with exact fixed-fill projection and locked existing vias; original KiCad board/rules remain authoritative"}
    report["pns"] = evidence
    def run(label, args):
        left = deadline - time.monotonic()
        if left <= .5:
            raise Halt("timed_out", "PNS shared search budget exhausted")
        return job.run(label, args, timeout=left)
    def model(path, output, measured=None):
        args = [config["kicad_python"], str(PACKAGE/"geometry_snapshot.py"), "--board", str(path),
                "--task", str(job.folder/"task.json"), "--output", str(output), "--pns-model"]
        if measured:
            args += ["--refilled-board", measured["refilled_board"], "--refilled-sha256", measured["refilled_sha256"]]
        if run(output.stem,args):
            raise Halt("error", "PNS geometry worker failed")
        value = json.loads(output.read_text())
        if value["status"] != "ready":
            evidence["unsupported_items"]=value.get("unsupported_items",[])
            reason=value.get('reason','Unsupported PNS geometry')
            evidence['unsupported_reason']=reason
            if value.get('outline_diagnostics_raw'):
                raw=job.folder/(output.stem+'-outline-raw.json')
                diagnosis=job.folder/(output.stem+'-outline-diagnostics.json')
                write_json(raw,value['outline_diagnostics_raw'])
                # Optional diagnosis must not overwrite the known modelling
                # failure. Cancellation and protection always remain decisive.
                try:
                    run(output.stem+'-outline-diagnostics',[config['router_python'],str(PACKAGE/'outline_diagnostics.py'),
                        '--analyze',str(raw),'--output',str(diagnosis)])
                    evidence['outline_diagnostics']=json.loads(diagnosis.read_text()) if diagnosis.exists() else {'status':'unavailable'}
                except Halt as exc:
                    if exc.status in ('cancelled','protection_violation','verification_incomplete'):raise
                    evidence['outline_diagnostics']={'status':'budget_exhausted' if exc.status=='timed_out' else 'unavailable','reason':str(exc)}
                except Exception as exc:
                    evidence['outline_diagnostics']={'status':'unavailable','reason':str(exc)}
            raise Halt("unsupported",reason)
        return value["model"]
    source_model = model(board,job.folder/"pns-source-model.json",report["preflight"]["measurement"])
    from .partial_routing import validated_plans
    subdivisions=validated_plans(source_model)
    evidence['partial_segments']=subdivisions
    netclasses = json.loads((job.folder/"netclasses.native.json").read_text())
    projection_path = job.folder/'pns-fill-projection.json'
    def geometry_worker(output, reread=None):
        args = [config['router_python'],str(PACKAGE/'pns_geometry.py'),
                '--source',str(job.folder/'pns-source-model.json'),'--projection',str(projection_path),
                '--output',str(output)]
        if reread:
            args += ['--reread',str(reread)]
        code = run(output.stem,args)
        value = json.loads(output.read_text()) if output.exists() else {}
        if code:
            raise Halt('unsupported',value.get('reason','PNS exact copper projection failed'))
        return value
    projection = geometry_worker(projection_path)
    evidence['remote_vias']=projection.get('remote_vias')
    if task.get('zone_refill'):
        evidence['zone_refill']=projection['zone_refill']
        evidence['policy']='Explicit selected-zone local refill search; all other copper remains fixed; full native refill scope witness required'
        report['candidate_generator']='pcbworld-pns-local-refill'
    evidence['direct_path_obstacles']=projection.get('direct_path_obstacles',{})
    evidence['local_relevance']={task['allowed_layer']:projection['local_relevance'],**{l:p['local_relevance'] for l,p in projection.get('auxiliary_layers',{}).items()}}
    try:
        problem = make_problem(board,task,source_model,netclasses,job.folder/"pns-problem",projection)
    except ValueError as exc:
        evidence['unsupported_items']=getattr(exc,'items',[])
        raise
    reread = model(problem,job.folder/"pns-problem-model.json")
    evidence['projection_roundtrip'] = geometry_worker(job.folder/'pns-problem-witness.json',job.folder/'pns-problem-model.json')
    evidence["native_problem_roundtrip"] = True
    # Empty native fills contain no copper and therefore have no exported pad.
    # Their definitions stay protected in the original, but cannot supply a
    # solver item from which to query an effective netclass.
    # Only query nets actually present in the independently witnessed solver
    # problem. A fully removed local fill may have no remaining solver item.
    models={task['allowed_layer']:reread,**reread.get('auxiliary_layers',{})}
    names={p['net'] for m in models.values() for p in m['primitives'] if p.get('net')}
    guidance_path = job.folder/'pns-guidance.json'
    guidance = optional_guidance(job, config, job.folder/'pns-source-model.json', guidance_path, deadline)
    evidence['cursor_guidance'] = guidance
    request = dict(board=str(problem), project=str(problem.with_suffix('.kicad_pro')),
                   cursor_guidance=guidance,
                   region_mm=task['region_mm'],
                   target_net=task["target_net"], allowed_layer=task['allowed_layer'],
                   endpoint_anchors=[p['center'] for p in source_model['endpoints']],
                   endpoint_refs=[p['uuid'] for p in source_model['endpoints']],
                   endpoints=[(p.get('native_pad') or {}).get('shape_center', p['center']) for p in source_model['endpoints']],
                   endpoint_bindings=task.get('target_endpoints'),
                   netclasses={n:netclasses["nets"][n]["values"] for n in names},
                   global_minima={"min_clearance_mm":source_model["minimum_clearance_mm"],
                       "min_track_width_mm":source_model["minimum_track_width_mm"],
                       "copper_edge_clearance_mm":source_model["edge_clearance_mm"]})
    request_path = job.folder/"pns-request.json"; write_json(request_path,request)
    output = job.folder/"pns-worker"
    code = run("pns-worker",[settings["python"],str(PACKAGE/"pns_worker.py"),"--request",str(request_path),
                            "--kicad-python",config['kicad_python'],
                            "--module-dir",settings["module_dir"],"--out-dir",str(output)])
    result = json.loads((output/"proposal.json").read_text()) if (output/"proposal.json").exists() else {}
    evidence["worker"] = result
    if code or result.get("status") not in {"proposed","no_progress"}:
        raise Halt("unsupported",result.get("error","PNS worker did not produce a valid proposal record"))
    if not result.get("final_restore_verified"):
        raise Halt("protection_violation","PNS could not restore its original transaction")
    loaded, restored = (read_board(Path(result[key])) for key in ("loaded_board", "restored_board"))
    if loaded != restored:
        raise Halt("protection_violation", "PNS save/restore did not preserve complete serialized problem semantics")
    original_rules = json.loads(Path(result["loaded_board"]).with_suffix('.kicad_pro').read_text())
    restored_rules = json.loads(Path(result["restored_board"]).with_suffix('.kicad_pro').read_text())
    for rules in (original_rules, restored_rules):
        rules.get("meta", {}).pop("filename", None)
    if original_rules != restored_rules:
        raise Halt("protection_violation", "PNS save/restore changed solver project rules")
    evidence["serialized_restore_verified"] = True
    # A KiCad 9 load/save must retain the actual fixed copper as interpreted
    # by native 10, not merely restore an already-misinterpreted baseline.
    model(Path(result['loaded_board']),job.folder/'pns-loaded-model.json')
    evidence['engine_geometry_roundtrip'] = geometry_worker(job.folder/'pns-loaded-witness.json',job.folder/'pns-loaded-model.json')
    from .candidate_pool import fingerprint, score
    from .workspace import source_files
    pool = dict(schema_version=1, status='collecting', candidates=[], duplicates=[],
                policy='Unique protected proposals, ranked by original edits/vias/length; full native gate alone accepts')
    report['candidate_pool'] = pool
    seen = {}
    for i, attempt in enumerate(result["attempts"]):
        job.check()
        if time.monotonic() >= deadline:
            raise Halt('timed_out', 'PNS shared generation budget exhausted while collecting candidates')
        if not attempt.get("committed"):
            continue
        row = {"direction":i,"eligible":False}; evidence["attempts"].append(row)
        try:
            candidate = compose(board,problem,result["loaded_board"],attempt["candidate_board"],task,
                                job.folder/"pns-candidates"/str(i),partial_plans=subdivisions)
            check = protection(read_board(board),read_board(candidate),board,candidate,task)
            row["protection"] = check
            if not all(check["checks"].values()):
                raise Halt('protection_violation', 'PNS composed proposal exceeds explicit modification permissions')
            key = fingerprint(board, candidate, task)
            row['fingerprint'] = key
            if key in seen:
                row.update(reason='Duplicate proposal geometry/context/task', duplicate_of=seen[key])
                pool['duplicates'].append(dict(attempt=i, duplicate_of=seen[key], fingerprint=key))
                continue
            seen[key] = i
            if task.get('zone_refill'):
                # Cached fills are stale by construction. Comparing them with
                # the refilled preflight can falsely report broken components.
                # This is only a proposal; the common gate must refill both
                # boards and prove original connectivity and exact fill scope.
                row.update(eligible=True, connectivity_status='deferred_to_mandatory_native_refill')
            else:
                snapshot = job.folder/("pns-connectivity-%d.json"%i)
                if run("pns-connectivity-%d"%i,[config["kicad_python"],str(PACKAGE/"native_snapshot.py"),str(candidate),str(snapshot)]):
                    raise Halt("error","PNS native connectivity probe failed")
                delta = connectivity_delta(native,json.loads(snapshot.read_text()),task)
                row["connectivity"] = delta
                if not delta['existing_components_preserved']:
                    raise Halt('protection_violation', 'PNS proposal damaged an existing native component')
                if not all(delta[k] for k in ("added_copper_connected_to_target","target_connected")):
                    row["reason"] = "PNS proposal did not complete the required native electrical relations"; continue
                row['eligible'] = True
            pool['candidates'].append(dict(attempt=i, board=str(candidate), hashes=source_files(candidate),
                fingerprint=key, score=score(board,candidate), status='pending'))
        except ValueError as exc:
            row["reason"] = str(exc)
            raise Halt('protection_violation', 'PNS proposal composition or identity check failed: '+str(exc)) from exc
    if time.monotonic() >= deadline:
        raise Halt('timed_out', 'PNS cumulative generation budget exceeded')
    pool['candidates'].sort(key=lambda row: (row['score'], row['attempt']))
    pool['status'] = 'proposed' if pool['candidates'] else 'no_progress'
    write_json(job.folder/'candidate-pool.json', pool)
    if pool['candidates']:
        row=pool['candidates'][0]
        evidence['status']='proposed'
        report['candidate_construction']=dict(status='ready',pns_attempt=row['attempt'],candidate_board=row['board'],requires_native_verification=True)
        return Path(row['board'])
    # Empty pools use the original no-progress validation path.
    report.pop('candidate_pool')
    evidence["status"] = "no_progress"
    candidate,_ = copy_project(board,job.folder/"scoping/candidate")
    return candidate
