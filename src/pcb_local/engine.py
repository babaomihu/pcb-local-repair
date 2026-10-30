"""Configure existing KRT conservatively; acceptance belongs to KiCad."""
from pathlib import Path
import json
import math
import re
from .task_policy import permissions
from .modification import contract
from .jobs import Halt


def check_layer_rule_floor(board, floor):
    # KRT layer rules replace its per-net map; the existing CLI has no
    # per-layer override. Do not silently route below the board minimum.
    path = Path(board).with_suffix('.kicad_dru')
    if not path.is_file():
        return
    from .validation import parse_sexpr, name
    for rule in parse_sexpr(path.read_text()):
        if name(rule) != 'rule':
            continue
        for entry in rule[2:]:
            if name(entry) == 'constraint' and len(entry) >= 3 and entry[1] == 'clearance':
                bounds = [b for b in entry[2:] if isinstance(b, list) and b and b[0] == 'min']
                if len(bounds) != 1 or len(bounds[0]) != 2:
                    raise Halt('unsupported', 'KRT clearance minimum cannot be certified')
                bound = bounds[0]
                match = re.fullmatch(r'(\d+(?:\.\d*)?|\.\d+)(mm|mil|in)', str(bound[1]))
                if not match:
                    raise Halt('unsupported', 'KRT clearance rule cannot be certified')
                value = float(match[1]) * {'mm': 1, 'mil': .0254, 'in': 25.4}[match[2]]
                if value < floor:
                    raise Halt('unsupported', 'KRT layer rule would override native board clearance minimum; use another candidate strategy')


def routing_plan(native, nets):
    """Candidate parameters only; never rewrite authored design rules.

    KRT supports individual clearance maps but only one explicit width/via
    override. Use the conservative maximum for selected nets when necessary.
    """
    try:
        rules = native['routing_rules']
        floors = rules['board_minima_mm']
        classes = rules['netclasses_mm']
        keys = ('clearance', 'track_width', 'via_diameter', 'via_drill')
        values = [floors[k] for k in (*keys, 'via_annular_width')]
        values += [row[k] for row in classes.values() for k in keys]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in values):
            raise ValueError('Invalid native dimension')
        selected = [classes[n] for n in nets]
        clearance = {n: max(row['clearance'], floors['clearance']) for n, row in classes.items()}
        result = {'schema_version': 1, 'native_rules': rules, 'selected_nets': nets,
                  'net_clearances_mm': clearance, 'clearance_mm': clearance[''],
                  'policy': 'Native board minima and effective classes; conservative run-only overrides; full native acceptance required'}
        for key in ('track_width', 'via_diameter', 'via_drill'):
            # Override only if a selected class/default would violate the floor.
            if min([classes[''][key], *[r[key] for r in selected]]) < floors[key]:
                result[key + '_mm'] = max(floors[key], *[r[key] for r in selected])
        if any(k in result for k in ('via_diameter_mm', 'via_drill_mm')) or any(
                r['via_diameter'] < r['via_drill'] + 2*floors['via_annular_width'] for r in [classes[''], *selected]):
            drill = max(floors['via_drill'], *[r['via_drill'] for r in selected])
            result['via_drill_mm'] = drill
            result['via_diameter_mm'] = max(floors['via_diameter'], drill + 2*floors['via_annular_width'], *[r['via_diameter'] for r in selected])
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise Halt('verification_incomplete', 'Missing/invalid native routing-rule evidence: ' + str(exc)) from exc


def check_terminal_layers(native, nets, policy):
    # KRT routes whole nets, not only our selected pair. Its single-layer
    # option still permits access vias to off-layer SMD pads. Refuse that
    # candidate strategy before launch when no vias were authorized.
    if policy['max_new_vias'] == 0:
        for item in native['items'].values():
            if item.get('kind') == 'pad' and item.get('net') in nets and not set(item['copper_layers']).intersection(policy['route_layers']):
                raise Halt('unsupported', 'KRT whole-net routing may require unauthorized access vias for off-layer terminals; use a local endpoint strategy')


def local_pair_request(board, task, native):
    """Use upstream endpoint overrides for a selected pair on a multi-pad net.

    All PCB objects remain in the solver model. No network relabeling, crop,
    access vias or preexisting-copper changes are authorized by this adapter.
    """
    from .workspace import sha256
    policy = permissions(task)
    if task.get('modification') and (not task.get('target_endpoints') or contract(task)['mode']!='ripup'):
        return None
    pads = [p for p in native['items'].values() if p.get('kind')=='pad' and p.get('net')==task['target_net']]
    if task.get('target_endpoints'):
        from .endpoints import validate
        try:
            permissions(task,native['copper_layers'])
            if not validate(task,native):raise ValueError('Endpoints already connected')
        except (ValueError,KeyError,TypeError) as exc:
            raise Halt('unsupported','Local KRT native endpoint binding failed: '+str(exc)) from exc
        ends=[dict(native['items'][e['item_id']],position_mm=e['position_mm']) for e in task['target_endpoints']]
        return dict(schema_version=1,board=str(Path(board).resolve()),board_sha256=sha256(board),task=task,
                    endpoints=ends,source_net_pad_count=len(pads),routing_plan=routing_plan(native,[task['target_net']]))
    # Multi-layer selected pad pairs also need the resource-bound local search.
    # Ordinary single-layer two-pad nets keep the established whole-net path.
    multilayer = len(policy['route_layers']) > 1
    if len(pads) <= 2 and not multilayer:
        return None
    ids = task.get('target_pad_uuids', [])
    if len(ids)!=2 or len(set(ids))!=2:
        return None
    ends=[]
    for uid in ids:
        found=[p for p in pads if p['uuid']==uid]
        if len(found)!=1 or task['allowed_layer'] not in found[0]['copper_layers']:
            raise Halt('unsupported','Local KRT target identity/layer is ambiguous')
        ends.append(found[0])
    return dict(schema_version=1,board=str(Path(board).resolve()),board_sha256=sha256(board),task=task,
                endpoints=ends,source_net_pad_count=len(pads),routing_plan=routing_plan(native,[task['target_net']]))


def command(config, before, after, task, summary, profile, native):
    env = {
        "KICAD_RIP_PREEXISTING": "0", "KICAD_FINALIZE_RIP": "0",
        "KICAD_RECONCILE_RIP_ESCALATION": "0", "KICAD_PLANE_FINALIZE": "0",
        "KICAD_RESCUE_CAP_MOVE": "0", "KICAD_BARE_PAD_ESCAPE": "0",
        "KICAD_CLEARANCE_LEGACY_CEILING": "0",
        "KICAD_PYTHON": config["kicad_python"], "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
        "KICAD_POUR_GUARD": "0" if profile == "external-pour-check" else "1",
    }
    nets = list(dict.fromkeys([task["target_net"], *contract(task)["nets"]]))
    local = local_pair_request(before, task, native)
    if task.get('target_endpoints') and local is None:
        raise Halt('unsupported','KRT native endpoints cannot yet restore ripped neighbour components')
    if local is None:
        check_terminal_layers(native, nets, permissions(task))
    plan = routing_plan(native, nets)
    check_layer_rule_floor(before, plan['native_rules']['board_minima_mm']['clearance'])
    plan_path = Path(summary).with_name('native-routing-plan.json')
    map_path = Path(summary).with_name('native-net-clearances.json')
    plan_path.write_text(json.dumps(plan, indent=2) + '\n')
    map_path.write_text(json.dumps(plan['net_clearances_mm'], indent=2) + '\n')
    if local is not None:
        local['profile']=profile
        request=Path(summary).with_name('local-target-request.json')
        request.write_text(json.dumps(local,indent=2)+'\n')
        return [config['router_python'],str(Path(__file__).with_name('krt_local.py')),
                '--request',str(request),'--krt-root',config['krt_root'],
                '--candidate',str(after),'--summary',str(summary)],env
    argv = [config["router_python"], str(Path(config["krt_root"]) / "py_router/route.py"),
            str(before), str(after), "--literal-nets", "--nets=" + nets[0], *nets[1:], "--layers", *permissions(task)["route_layers"],
            "--max-ripup", "0", "--no-stub-layer-swap", "--keep-input-copper",
            "--escalation", "off", "--no-fix-drc-settings", "--strict-sizes",
            "--no-power-tap-neckdown", "--json-out", str(summary)]
    argv += ['--clearance', str(plan['clearance_mm']), '--net-clearances', str(map_path)]
    for key, flag in (('track_width_mm', '--track-width'), ('via_diameter_mm', '--via-size'), ('via_drill_mm', '--via-drill')):
        if key in plan:
            argv += [flag, str(plan[key])]
    return argv, env
