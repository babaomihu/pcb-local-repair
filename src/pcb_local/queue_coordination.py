"""Bounded, dynamic queue coordination; estimates never authorize routing.

The caller supplies the already-required fresh native inspection before each
child. We choose one original selected target, with no route attempts or ROI,
permission, endpoint or budget changes. Sparse polyline templates estimate
shared-channel loss; they are not paths handed to an engine or feasibility
proofs. In particular the snapshot lacks full pad/via/arc/fill geometry.
"""
import math
from .endpoints import catalog
from .queue_order import _point, _segment_distance, _endpoints

MAX_TARGETS = 64
MAX_SEGMENTS = 2000
MAX_NEIGHBOURS = 8


def _dimension(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError('Coordination requires finite native rule dimensions')
    return float(value)


def _rules(snapshot, net):
    rules = snapshot['routing_rules']
    floors, nc = rules['board_minima_mm'], rules['netclasses_mm'][net]
    width = max(_dimension(floors['track_width']), _dimension(nc['track_width']))
    clearance = max(_dimension(floors['clearance']), _dimension(nc['clearance']))
    if width <= 0:
        raise ValueError('Coordination requires positive native track width')
    return width, clearance


def _bound_points(target, available, layer):
    ends, points = _endpoints(target)
    identities = []
    for end, point in zip(ends, points):
        matches = [e for e in available if e['uuid'] == end.get('uuid')
                   and e['selector'] == end.get('selector', 'center')]
        if len(matches) != 1:
            raise ValueError('Coordination endpoint identity is missing or ambiguous')
        actual = matches[0]
        if actual['net'] != target['net'] or actual['layer'] != layer or tuple(actual['position_mm']) != point:
            raise ValueError('Coordination endpoint net/layer/position changed')
        for key in ('item_id', 'kind', 'selector', 'net', 'layer'):
            if key in end and end[key] != actual[key]:
                raise ValueError('Coordination endpoint binding changed: ' + key)
        identities.append(actual['item_id'])
    if len(set(identities)) != 2:
        raise ValueError('Coordination requires distinct original endpoint objects')
    return points


def _templates(a, b, roi, width):
    """Five deterministic hints inside the *original* copper-envelope ROI."""
    x0, y0, x1, y1 = roi
    inset = width / 2 + .000001
    if not all(x0 + inset <= p[0] <= x1 - inset and y0 + inset <= p[1] <= y1 - inset for p in (a, b)):
        return []
    horizontal = abs(b[0] - a[0]) >= abs(b[1] - a[1])
    axis = 1 if horizontal else 0
    low, high = roi[axis] + inset, roi[axis + 2] - inset
    paths = [[a, b]]
    for fraction in (0., .333333333333, .666666666667, 1.):
        lane = low + fraction * (high - low)
        p, q = list(a), list(b)
        p[axis] = q[axis] = lane
        paths.append([a, tuple(p), tuple(q), b])
    unique = {}
    for path in paths:
        points = [path[0]]
        for point in path[1:]:
            if point != points[-1]:
                points.append(point)
        if len(points) >= 2:
            unique.setdefault(tuple(points), sum(math.dist(p, q) for p, q in zip(points, points[1:])))
    return [dict(points=list(path), length_mm=length) for path, length in
            sorted(unique.items(), key=lambda p: (p[1], p[0]))]


def _touches(path, other, clearance):
    return any(_segment_distance(a, b, c, d) <= clearance
               for a, b in zip(path, path[1:]) for c, d in zip(other, other[1:]))


def choose_next(targets, native_snapshot, layer, *, region_mm=None, check=None):
    """Return (one unchanged input target, JSON audit evidence).

    Call again on *remaining* selected targets and fresh native copper after a
    child. `region_mm` supplies the original common ROI for manual selections;
    automatic selections keep their own `region_mm`. No task is removed here.
    Missing identity/rule evidence raises ValueError, never invents dimensions.
    """
    if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= MAX_TARGETS:
        raise ValueError('Coordination requires 1–64 selected targets')
    ids = [t.get('id') for t in targets]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Coordination target IDs must be nonempty and unique')
    if layer not in native_snapshot.get('copper_layers', []):
        raise ValueError('Coordination layer is absent from the native snapshot')
    available = list(catalog(native_snapshot, layer, check).values())
    segments, unmodelled = [], {}
    for key, item in sorted(native_snapshot['items'].items()):
        if check: check()
        if layer not in item.get('copper_layers', []):
            continue
        kind = item.get('kind', 'unknown')
        if kind != 'segment':
            unmodelled[kind] = unmodelled.get(kind, 0) + 1
            continue
        try:
            _, clearance = _rules(native_snapshot, item['net'])
            width = _dimension(item['width_mm'])
            own = _dimension(item.get('own_clearance_mm', 0))
            a, b = _point(item['start_mm']), _point(item['end_mm'])
        except (KeyError, ValueError, TypeError):
            unmodelled['invalid_segment_or_rule'] = unmodelled.get('invalid_segment_or_rule', 0) + 1
            continue
        segments.append((key, item['net'], a, b, width, max(clearance, own)))
    total_segments = len(segments)
    segments = segments[:MAX_SEGMENTS]
    rows = []
    for target in targets:
        if check: check()
        a, b = _bound_points(target, available, layer)
        try:
            width, clearance = _rules(native_snapshot, target['net'])
        except (KeyError, TypeError) as exc:
            raise ValueError('Coordination needs native rules for every selected net') from exc
        roi = target.get('region_mm', region_mm)
        if not isinstance(roi, (list, tuple)) or len(roi) != 4:
            raise ValueError('Coordination requires the original fixed ROI')
        roi = [float(v) for v in roi]
        if not all(math.isfinite(v) for v in roi) or roi[0] >= roi[2] or roi[1] >= roi[3]:
            raise ValueError('Coordination ROI is invalid')
        if not all(roi[0] <= p[0] <= roi[2] and roi[1] <= p[1] <= roi[3] for p in (a, b)):
            raise ValueError('Coordination endpoint is outside fixed ROI')
        paths = _templates(a, b, roi, width)
        for path in paths:
            blockers = []
            for index, (key, net, c, d, other_width, other_clearance) in enumerate(segments):
                if check and index % 128 == 0: check()
                if net == target['net']:
                    continue
                distance = width/2 + other_width/2 + max(clearance, other_clearance)
                # Inexpensive safe rejection before exact segment distances.
                if (max(c[0], d[0]) + distance < roi[0] or min(c[0], d[0]) - distance > roi[2]
                        or max(c[1], d[1]) + distance < roi[1] or min(c[1], d[1]) - distance > roi[3]):
                    continue
                if _touches(path['points'], [c, d], distance):
                    blockers.append(key)
            path['segment_blockers'] = blockers[:16]
            path['segment_blocker_count'] = len(blockers)
        clear = [p for p in paths if not p['segment_blocker_count']]
        rows.append(dict(id=target['id'], net=target['net'], width_mm=width,
                         clearance_mm=clearance, region_mm=roi, templates=paths,
                         segment_clear_template_count=len(clear), clear=clear,
                         shared_corridor_targets=[], pair_losses=[]))
    for i, row in enumerate(rows):
        if check: check()
        if not row['clear']:
            continue
        predicted = row['clear'][0]['points']
        for j, other in enumerate(rows):
            if i == j or row['net'] == other['net'] or not other['clear']:
                continue
            distance = (row['width_mm'] + other['width_mm'])/2 + max(row['clearance_mm'], other['clearance_mm'])
            lost = sum(_touches(predicted, p['points'], distance) for p in other['clear'])
            if lost:
                row['shared_corridor_targets'].append(other['id'])
                row['pair_losses'].append(dict(target_id=other['id'], templates_before=len(other['clear']),
                    predicted_templates_lost=lost, all_templates_lost=lost == len(other['clear'])))
        # Keep pair evaluation evidence bounded. Prefer the potentially most
        # constrained neighbours; ties retain selected order, never net names.
        row['pair_losses'].sort(key=lambda r: (not r['all_templates_lost'], -r['predicted_templates_lost']/r['templates_before']))
        row['pair_sampling_truncated'] = len(row['pair_losses']) > MAX_NEIGHBOURS
        row['pair_losses'] = row['pair_losses'][:MAX_NEIGHBOURS]
    for row in rows:
        losses = row['pair_losses']
        row['predicted_exhausted_neighbours'] = sum(x['all_templates_lost'] for x in losses)
        row['predicted_fraction_lost'] = sum(x['predicted_templates_lost']/x['templates_before'] for x in losses)
        row['ranking_key'] = [not bool(row['clear']), row['predicted_exhausted_neighbours'],
                              row['predicted_fraction_lost'], -len(row['shared_corridor_targets']),
                              row['segment_clear_template_count']]
        row['predicted_template'] = row['clear'][0]['points'] if row['clear'] else None
        del row['clear']
    chosen = min(range(len(rows)), key=lambda i: (*rows[i]['ranking_key'], i))
    evidence = dict(schema_version=1, policy='dynamic-channel-loss-v1', layer=layer,
        remaining_selected_order=ids, selected_next=ids[chosen], scores=rows,
        segments_considered=len(segments), segments_eligible=total_segments,
        segment_sampling_truncated=total_segments > MAX_SEGMENTS, unmodelled_objects=unmodelled,
        physical_blockage_proven=False, repair_count_gain_proven=False,
        extra_attempts=0, extra_native_inspections=0, selection_unchanged=True,
        endpoints_unchanged=True, regions_unchanged=True, permissions_unchanged=True, budgets_unchanged=True,
        limitations='Heuristic straight/dogleg templates only; not engine routes. Models current straight-track clearance, but not full pad/via/arc geometry, holes, filled copper, keepouts or board outline. Clear templates do not prove feasibility and exhausted templates do not prove physical blockage. Actual router may choose another path; all outputs still require full native acceptance.')
    return targets[chosen], evidence
