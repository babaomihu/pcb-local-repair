"""Conservative shortening of one newly added, unbranched straight-track chain.

This is an optional proposal after ordinary acceptance. It never edits original
copper, drops a side connection, widens permissions or substitutes for native
validation. All geometry work and validation consume the caller's remaining
generation and verification budgets. Unsupported candidates stay unchanged.
"""
import argparse
import copy
from decimal import Decimal
import json
import math
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcb_local.jobs import Halt, write_json
from pcb_local.validation import canonical, field, name, parse_sexpr, read_board
from pcb_local.workspace import copy_project, source_files


def _point(values):
    result = []
    for value in values:
        number = Decimal(str(value)) * 1000000
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError('Quality optimization requires exact native-nanometre vertices')
        result.append(int(number))
    return tuple(result)


def chain(before, candidate, task, model):
    """Return only a complete, simple, additive chain between the exact anchors."""
    if task.get('modification') or task.get('zone_refill'):
        raise ValueError('Quality optimization currently requires additive fixed-copper tasks')
    old, new = read_board(before), read_board(candidate)
    if any(uid not in new['routing'] or canonical(node) != canonical(new['routing'][uid])
           for uid, node in old['routing'].items()):
        raise ValueError('Original routing changed; quality optimization is ineligible')
    added = {uid: n for uid, n in new['routing'].items() if uid not in old['routing']}
    if not 2 <= len(added) <= 512:
        raise ValueError('Quality optimization requires 2–512 newly added segments')
    graph, widths = {}, set()
    for uid, node in added.items():
        if (name(node) != 'segment' or new['identities'][uid]['net'] != task['target_net'] or
                str(field(node, 'layer')[1]) != task['allowed_layer'] or
                any(name(child) not in ('start', 'end', 'width', 'layer', 'net', 'uuid', 'tstamp')
                    for child in node[1:])):
            raise ValueError('Only ordinary same-net same-layer unlocked added segments are supported')
        a, b = [_point(field(node, key)[1:3]) for key in ('start', 'end')]
        width = _point([field(node, 'width')[1]])[0]
        if a == b or width <= 0:
            raise ValueError('Degenerate added track')
        widths.add(width)
        graph.setdefault(a, []).append((b, uid))
        graph.setdefault(b, []).append((a, uid))
    if len(widths) != 1 or any(len(v) not in (1, 2) for v in graph.values()):
        raise ValueError('Variable widths or branched added routing are not optimized')
    terminals = [p for p, values in graph.items() if len(values) == 1]
    anchors = [_point(row['center']) for row in model['endpoints']]
    if len(anchors) != 2 or set(terminals) != set(anchors):
        raise ValueError('Added chain terminals do not exactly match the task endpoints')
    points, ordered, seen = [anchors[0]], [], set()
    while points[-1] != anchors[1]:
        options = [(p, uid) for p, uid in graph[points[-1]] if uid not in seen]
        if len(options) != 1:
            raise ValueError('Added route is cyclic or ambiguous')
        p, uid = options[0]
        seen.add(uid); ordered.append(uid); points.append(p)
    if seen != set(added):
        raise ValueError('Additional disconnected or closed routing must not be discarded')
    return [[x / 1e6, y / 1e6] for x, y in points], ordered, next(iter(widths)) / 1e6


def eligibility(before, candidate, task):
    """Cheap file-only exclusions before starting any native geometry process."""
    old, new = read_board(before), read_board(candidate)
    added = {uid: node for uid, node in new['routing'].items() if uid not in old['routing']}
    if not 2 <= len(added) <= 512:
        return 'Requires 2–512 newly added straight segments'
    if any(uid not in new['routing'] or canonical(node) != canonical(new['routing'][uid])
           for uid, node in old['routing'].items()):
        return 'Original routing was modified; optional additive quality is ineligible'
    widths = set()
    policy=task.get('permissions',{})
    layers=policy.get('route_layers',[task['allowed_layer']])
    layered=len(layers)>1
    vias=0
    for uid, node in added.items():
        if new['identities'][uid]['net'] != task['target_net']:
            return 'Additional non-target-net routing is outside quality scope'
        if name(node)=='via' and layered:
            vias+=1
            if list(map(str,field(node,'layers')[1:]))!=['F.Cu','B.Cu']:
                return 'Only ordinary through vias are supported by layered quality'
            continue
        if name(node) != 'segment': return 'New vias/arcs are outside straight-chain quality scope'
        if str(field(node, 'layer')[1]) not in layers:
            return 'Multilayer routing is outside authorized quality layers'
        widths.add(Decimal(str(field(node, 'width')[1])))
    if vias>policy.get('max_new_vias',0):return 'New via count exceeds original quality task permission'
    if len(widths) != 1: return 'Variable-width routing is outside quality scope'
    return None


def _shape(item):
    """Conservative original-copper envelope for same-net branch preservation."""
    import shapely
    from shapely import affinity
    from shapely.geometry import Point, LineString, box
    from pcb_local.geometry_search import native_polygon
    inflation = 1 / math.cos(math.pi / 128)
    def buffer(shape, r):
        return shape.buffer((r + .00001) * inflation, quad_segs=32)
    if 'polygons' in item:
        return shapely.union_all([native_polygon(p) for p in item['polygons']])
    if item['kind'] == 'segment':
        return buffer(LineString([item['start'], item['end']]), item['width_mm'] / 2)
    if item['kind'] in ('circle', 'hole'):
        diameter = item.get('diameter_mm', (item.get('size') or [0])[0])
        return buffer(Point(item['center']), diameter / 2)
    if item['kind'] == 'rect':
        x, y = item['center']; sx, sy = [s / 2 for s in item['size']]
        return affinity.rotate(box(x-sx, y-sy, x+sx, y+sy), item['angle_degrees'], origin=(x, y))
    raise ValueError('Unknown same-net copper cannot be ignored during quality optimization')


def shorten(points, width, model, check=lambda: None, *, keep_native_edges=False):
    """Farthest-visible shortcuts between existing vertices, never new waypoints."""
    from shapely.geometry import LineString
    from pcb_local.geometry_search import configuration_space
    old_line = LineString(points)
    if not old_line.is_simple:
        raise ValueError('Self-crossing added routing is not optimized')
    terminals = {row.get('uuid') for row in model['endpoints']}
    branch_objects = []
    for item in model['primitives']:
        check()
        if item['net'] != model['task']['target_net'] or item.get('uuid') in terminals:
            continue
        if _shape(item).distance(old_line) <= width / 2 + .00001:
            branch_objects.append(item.get('uuid'))
    if branch_objects:
        raise ValueError('Preserving additional same-net contact objects: ' + ', '.join(map(str, branch_objects)))
    projected = copy.deepcopy(model)
    projected['minimum_track_width_mm'] = max(projected['minimum_track_width_mm'], width)
    for endpoint in projected['endpoints']:
        endpoint['track_width_mm'] = max(endpoint['track_width_mm'], width)
    free, _, _, _ = configuration_space(projected, check)
    if not keep_native_edges and not free.covers(old_line):
        raise ValueError('Accepted chain is outside the conservative quality model; keep its native result')
    result = [points[0]]; current = 0
    while current < len(points) - 1:
        check()
        for target in range(len(points) - 1, current, -1):
            check()
            if (keep_native_edges and target==current+1 or
                    free.covers(LineString([points[current], points[target]]))):
                result.append(points[target]); current = target; break
        else:
            raise ValueError('No witnessed next edge; original candidate retained')
    return result


def propose(before, candidate, task, model, folder, timeout=5):
    """Create at most one independent proposal; never accept it here."""
    if 'routing_models' in model:
        from pcb_local.layered_quality import propose as layered_propose
        return layered_propose(before,candidate,task,model,folder,timeout)
    started = time.monotonic()
    baseline, original = source_files(before), source_files(candidate)
    def check():
        if time.monotonic() - started >= timeout:
            raise TimeoutError('Quality generation sub-budget exhausted')
    evidence = dict(status='unchanged', method='farthest-visible-existing-vertex-shortcuts',
                    requires_native_verification=True)
    try:
        check()
        points, identities, width = chain(before, candidate, task, model)
        result = shorten(points, width, model, check)
        length = lambda ps: sum(math.dist(a, b) for a, b in zip(ps, ps[1:]))
        evidence.update(before_length_mm=length(points), after_length_mm=length(result),
                        before_segments=len(points)-1, after_segments=len(result)-1)
        if length(result) > length(points) + 1e-9:
            raise ValueError('Optimization increased length')
        if len(result) >= len(points) and length(result) >= length(points) - 1e-9:
            return evidence
        check()
        from pcb_local.scope_candidate import _serialize
        roots = parse_sexpr(candidate.read_text())
        nodes = {str((field(n, 'uuid') or field(n, 'tstamp'))[1]): n
                 for n in roots[0][1:] if name(n) == 'segment'}
        replacements = []
        for uid, a, b in zip(identities, result, result[1:]):
            node = copy.deepcopy(nodes[uid])
            for key, point in [('start', a), ('end', b)]:
                field(node, key)[1:3] = [str(Decimal(n) / 1000000) for n in _point(point)]
            replacements.append(node)
        roots[0][:] = [roots[0][0]] + [n for n in roots[0][1:] if not
            (name(n) == 'segment' and str((field(n, 'uuid') or field(n, 'tstamp'))[1]) in identities)] + replacements
        proposed, _ = copy_project(candidate, folder)
        proposed.write_text(_serialize(roots[0]) + '\n')
        old, new = read_board(candidate), read_board(proposed)
        if old['static'] != new['static'] or any(canonical(node) != canonical(new['routing'].get(uid))
            for uid, node in old['routing'].items() if uid not in identities):
            raise Halt('protection_violation', 'Quality rewrite altered protected definitions')
        check()
        evidence.update(status='proposed', candidate_board=str(proposed), candidate_hashes=source_files(proposed),
                        input_candidate_hashes=original, rewritten_added_uuids=identities,
                        retained_new_uuids=identities[:len(result)-1])
        return evidence
    finally:
        if source_files(before) != baseline or source_files(candidate) != original:
            raise Halt('protection_violation', 'Quality generation mutated an input')


class QualityJob:
    def __init__(self, parent):
        self.parent = parent; self.folder = parent.folder / 'trace-quality'
        self.folder.mkdir(exist_ok=False)
    def check(self): self.parent.check()
    def run(self, label, argv, **kwargs): return self.parent.run('quality-' + label, argv, **kwargs)


def improve(job, config, board, candidate, task, report, original_code, original_verification,
            generation_budget, verification_budget, validator=None):
    """Try one optional optimization of an already accepted candidate.

    Return (candidate, code, verification). Caller must charge both measured
    stage times from report['trace_quality'] to its original cumulative budget.
    """
    from pcb_local.candidate_pipeline import validate, outcome
    validator = validator or validate
    accepted = candidate, original_code, original_verification
    evidence = dict(status='not_attempted', generation_seconds=0., verification_seconds=0.,
                    original_candidate=str(candidate), policy='Original accepted candidate retained; original budgets and native gate')
    report['trace_quality'] = evidence
    if outcome(original_code, original_verification)[0] != 'accepted':
        evidence['reason'] = 'Only a fully accepted candidate may be optimized'; return accepted
    if generation_budget <= 2 or verification_budget <= 2 or task.get('modification') or task.get('zone_refill'):
        evidence['reason'] = 'Outside additive quality scope or insufficient remaining original budget'; return accepted
    preparation_started = time.monotonic()
    reason = eligibility(board, candidate, task)
    if reason:
        evidence['generation_seconds'] = time.monotonic()-preparation_started
        if evidence['generation_seconds'] > generation_budget:
            raise Halt('timed_out', 'Quality eligibility check exceeded original remaining budget')
        evidence['reason'] = reason; return accepted
    # Only generation gets a small optional sub-budget; final validation still
    # uses the original complete validator and its remaining original allowance.
    allocation = min(5., generation_budget - 1.)
    baseline, original = source_files(board), source_files(candidate)
    sub = QualityJob(job)
    write_json(sub.folder/'task.json', task); write_json(sub.folder/'task-manifest.json', {'cases':[task]})
    try:
        start = preparation_started
        deadline = start + allocation
        def run(label, args):
            left = deadline - time.monotonic()
            if left <= .1: raise TimeoutError('Quality generation sub-budget exhausted')
            if sub.run(label, args, timeout=left): raise ValueError('Quality worker failed: ' + label)
        try:
            model = sub.folder/'model.json'
            argv = [config['kicad_python'], str(Path(__file__).with_name('geometry_snapshot.py')),
                    '--board', str(board), '--task', str(sub.folder/'task.json'), '--output', str(model)]
            if len(task.get('permissions',{}).get('route_layers',[]))>1:
                argv.append('--multilayer-model')
            measured = report.get('preflight', {}).get('measurement', {})
            if measured.get('refilled_board') and measured.get('refilled_sha256'):
                argv += ['--refilled-board', measured['refilled_board'], '--refilled-sha256', measured['refilled_sha256']]
            run('model', argv)
            data = json.loads(model.read_text())
            if data.get('status') != 'ready': raise ValueError(data.get('reason', 'Quality model unsupported'))
            run('proposal', [config['router_python'], str(Path(__file__)), '--before', str(board), '--candidate', str(candidate),
                '--task', str(sub.folder/'task.json'), '--model', str(model), '--out-dir', str(sub.folder/'proposal'),
                '--output', str(sub.folder/'proposal.json'), '--timeout', str(max(.01, deadline-time.monotonic()-.1))])
            evidence.update(json.loads((sub.folder/'proposal.json').read_text()))
        except (TimeoutError, ValueError) as exc:
            evidence.update(status='kept_original', reason=str(exc)); return accepted
        except Halt as exc:
            if exc.status != 'timed_out': raise
            # A sub-budget timeout may keep the accepted original, but a parent
            # task/cancellation deadline always propagates through check().
            sub.check(); evidence.update(status='kept_original', reason=str(exc)); return accepted
        finally:
            evidence['generation_seconds'] = time.monotonic()-start
        if evidence.get('status') != 'proposed': return accepted
        sub.check()
        proposed = Path(evidence['candidate_board'])
        if source_files(proposed) != evidence['candidate_hashes']:
            raise Halt('protection_violation', 'Quality candidate changed before validation')
        start = time.monotonic()
        try:
            code, verification = validator(sub, config, board, proposed, task, budget=verification_budget)
        finally:
            evidence['verification_seconds'] = time.monotonic()-start
        status, reason = outcome(code, verification)
        evidence.update(validation_status=status, verification_directory=str(sub.folder/'verification'), reason=reason)
        if source_files(proposed) != evidence['candidate_hashes']:
            raise Halt('protection_violation', 'Quality candidate changed during validation')
        if status in ('protection_violation', 'verification_incomplete'):
            raise Halt(status, reason)
        if evidence['verification_seconds'] > verification_budget:
            raise Halt('timed_out', 'Quality validation exceeded original remaining budget')
        sub.check()
        if status == 'accepted':
            evidence['status'] = 'accepted'; return proposed, code, verification
        evidence['status'] = 'kept_original'; return accepted
    except Halt as exc:
        evidence.update(status=exc.status, reason=str(exc))
        raise
    finally:
        try:
            unchanged = source_files(board) == baseline and source_files(candidate) == original
        except Exception:
            unchanged = False
        if not unchanged:
            evidence.update(status='protection_violation', reason='Quality optimization changed protected input')
            write_json(sub.folder/'quality.json', evidence)
            raise Halt('protection_violation', evidence['reason'])
        if evidence['generation_seconds'] > generation_budget:
            evidence.update(status='timed_out', reason='Quality generation exceeded original remaining budget')
            write_json(sub.folder/'quality.json', evidence)
            raise Halt('timed_out', evidence['reason'])
        write_json(sub.folder/'quality.json', evidence)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for option in ['before', 'candidate', 'task', 'model', 'out-dir', 'output']:
        p.add_argument('--'+option, type=Path, required=True)
    p.add_argument('--timeout', type=float, required=True)
    args = p.parse_args()
    try:
        evidence = propose(args.before, args.candidate, json.loads(args.task.read_text()),
            json.loads(args.model.read_text())['model'], args.out_dir, timeout=args.timeout)
    except (ValueError, TimeoutError) as exc:
        evidence = dict(status='kept_original', reason=str(exc))
    write_json(args.output, evidence)
    print(json.dumps(evidence)); return 0


if __name__ == '__main__': raise SystemExit(main())
