"""Source-bound omission of irrelevant fixed vias from a disposable solver.

No original-board object is deleted. Geometric proof runs in the pinned geometry
worker before problem construction and independently again after native readback.
The CLI-side filter has no Shapely dependency and never grants edit authority.
"""
import copy
import hashlib
import json
import math


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def number(value, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value <= 0):
        raise ValueError('Invalid native remote-via constraint')
    return value


def point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2 or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise ValueError('Invalid native remote-via coordinate')
    return value


def layer_models(model):
    return {model['task']['allowed_layer']: model, **model.get('auxiliary_layers', {})}


def analyze(model):
    # Legacy models without unsupported vias need no new proof or dependencies.
    raw = [model, *model.get('auxiliary_layers', {}).values()]
    if not any(v.get('standard_through') is not True for m in raw for v in m.get('vias', [])):
        return None
    from shapely.geometry import box, Point
    import shapely
    from .geometry_search import native_polygon
    from .layer_stack import supported_stack

    layers = model['copper_layers']; models = layer_models(model); task = model['task']
    if not supported_stack(layers) or set(models) != set(layers):
        raise ValueError('Remote via requires every physical-layer witness')
    roi = task['region_mm']
    if len(roi) != 4:
        raise ValueError('Invalid remote-via region')
    point(roi[:2]); point(roi[2:])
    if roi[0] >= roi[2] or roi[1] >= roi[3]:
        raise ValueError('Invalid remote-via region')
    region = box(*roi)
    active = {task['target_net'], *task.get('modification', {}).get('nets', [])}
    protected = set(task.get('target_item_ids', [])) | set(task.get('target_pad_uuids', []))
    protected.update(p['uuid'] for p in model['endpoints'])
    for layer, m in models.items():
        if m['copper_layers'] != layers or m['task']['allowed_layer'] != layer:
            raise ValueError('Inconsistent remote-via native layer stack')
        for key in ('region_mm', 'target_net', 'modification'):
            if m['task'].get(key) != task.get(key):
                raise ValueError('Inconsistent remote-via task authority')
    clearance = max([number(m['minimum_clearance_mm']) for m in models.values()] +
                    [number(p['clearance_mm']) for m in models.values()
                     for p in m['primitives'] + m['endpoints'] if p.get('net') in active])
    hole_gap = max(number(m['hole_to_hole_clearance_mm']) for m in models.values())
    observations = {}
    for layer, m in models.items():
        seen = set()
        for via in m['vias']:
            uid = via['uuid']
            if not isinstance(uid, str) or not uid or uid in seen:
                raise ValueError('Duplicate or missing native via identity')
            seen.add(uid); observations.setdefault(uid, {})[layer] = via
    objects = []
    for uid, by_layer in sorted(observations.items()):
        first = next(iter(by_layer.values()))
        fields = ('uuid', 'net', 'center', 'drill_mm', 'layers', 'copper_layers', 'standard_through', 'locked')
        if all(v.get('standard_through') is True for v in by_layer.values()):
            continue
        if (not set(first['copper_layers']) <= set(layers) or
                len(set(first['copper_layers'])) != len(first['copper_layers']) or
                set(by_layer) != set(first['copper_layers']) or
                any(any(row[k] != first[k] for k in fields) for row in by_layer.values())):
            raise ValueError('Native via identity/span differs across layers')
        point(first['center']); number(first['drill_mm'], True)
        if type(first['locked']) is not bool or type(first['standard_through']) is not bool:
            raise ValueError('Missing native via state')
        if uid in protected or first['net'] in active:
            objects.append(dict(uuid=uid, eligible=False, reason='target_or_authorized_net'))
            continue
        checks = []
        for layer, via in sorted(by_layer.items()):
            rows = [p for p in models[layer]['primitives'] if p['uuid'] == uid]
            holes = [p for p in rows if p['kind'] == 'hole']
            copper = [p for p in rows if p['kind'] == 'polygon']
            if len(rows) != 2 or len(holes) != 1 or len(copper) != 1:
                raise ValueError('Via lacks unique native copper and hole witnesses')
            h, p = holes[0], copper[0]
            if (h['net'] != via['net'] or p['net'] != via['net'] or
                    h['center'] != via['center'] or h['diameter_mm'] != via['drill_mm']):
                raise ValueError('Native via geometry does not match identity')
            if not p['polygons']:
                raise ValueError('Missing native via copper')
            for poly in p['polygons']:
                for ring in [poly['shell'], *poly['holes']]:
                    for xy in ring: point(xy)
            shape = shapely.union_all([native_polygon(poly) for poly in p['polygons']])
            if shape.is_empty or not shape.is_valid:
                raise ValueError('Invalid native via copper envelope')
            copper_gap = max(clearance, number(p['clearance_mm']), number(via['clearance_mm']))
            drill_gap = max(clearance, number(h['hole_clearance_mm']), hole_gap)
            # Native approximation error and a strict 2 nm guard only enlarge
            # the protected influence. No centre-only or distance tolerance test.
            error = number(p['native_error_mm'])
            copper_distance = shape.distance(region)
            hole_distance = Point(h['center']).distance(region) - number(h['diameter_mm'], True)/2
            eligible = (copper_distance > copper_gap + error + .000002 and
                        hole_distance > drill_gap + .000002)
            checks.append(dict(layer=layer, copper_distance_mm=copper_distance,
                               required_copper_gap_mm=copper_gap, native_error_mm=error,
                               hole_distance_mm=hole_distance, required_hole_gap_mm=drill_gap,
                               eligible=eligible))
        eligible = all(c['eligible'] for c in checks)
        objects.append(dict(uuid=uid, net=first['net'], eligible=eligible,
                            reason='influence_disjoint_from_allowed_copper_region' if eligible else 'near_region',
                            layers=checks))
    return dict(schema_version=1, source_sha256=digest(model), objects=objects,
                omitted_uuids=[o['uuid'] for o in objects if o['eligible']],
                policy='Disposable solver only; inactive foreign vias, full physical-layer copper/hole proof; original board objects retained')


def filtered(model, proof):
    """Apply a bound worker proof; callers must also perform native verification."""
    if proof is None:
        return model
    if proof.get('schema_version') != 1 or proof.get('source_sha256') != digest(model):
        raise ValueError('Remote-via proof is not bound to the complete source model')
    ids = proof['omitted_uuids']
    eligible = [p['uuid'] for p in proof['objects'] if p['eligible']]
    if ids != eligible or len(set(ids)) != len(ids):
        raise ValueError('Remote-via omission list differs from proof')
    result = copy.deepcopy(model)
    for m in layer_models(result).values():
        m['vias'] = [v for v in m['vias'] if v['uuid'] not in ids]
        m['primitives'] = [p for p in m['primitives'] if p['uuid'] not in ids]
    return result
