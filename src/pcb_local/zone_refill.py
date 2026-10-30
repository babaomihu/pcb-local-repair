"""Explicit, ROI-bounded search permission for selected native zone fills.

Only solver obstacles change here. Original zone definitions and fill caches are
never edited; the independent native refill witness decides candidate eligibility.
"""
import copy
import math


def bind_selection(task, ids, native_choices, layered=False):
    """Bind user-selected UUIDs to the explicitly authorized routing layers."""
    if not layered:
        return dict(schema_version=1, zone_uuids=list(ids))
    from .task_policy import permissions
    layers=permissions(task)['route_layers']
    if len({z['uuid'] for z in native_choices}) != len(native_choices):
        raise ValueError('Native refill identities are ambiguous')
    choices={z['uuid']:z for z in native_choices}
    value=dict(schema_version=2,zones=[dict(uuid=uid,layers=[l for l in layers
               if l in choices.get(uid,{}).get('layers',[])]) for uid in ids])
    layer_contract(dict(task,zone_refill=value))
    return value


def contract(task):
    return list(layer_contract(task))


def layer_contract(task):
    """Bind each zone to explicitly permitted layers; v1 semantics stay fixed."""
    value = task.get('zone_refill')
    if value is None:
        return {}
    from .task_policy import permissions
    policy = permissions(task)
    if isinstance(value, dict) and type(value.get('schema_version')) is int and value['schema_version'] == 2:
        if set(value) != {'schema_version', 'zones'} or not isinstance(value['zones'], list) or not value['zones']:
            raise ValueError('Invalid layered zone refill permission')
        selected = {}
        for row in value['zones']:
            if (not isinstance(row, dict) or set(row) != {'uuid', 'layers'}
                    or not isinstance(row['uuid'], str) or not row['uuid'] or row['uuid'] in selected
                    or not isinstance(row['layers'], list) or not row['layers']
                    or any(not isinstance(l, str) for l in row['layers'])
                    or len(set(row['layers'])) != len(row['layers'])
                    or not set(row['layers']) <= set(policy['route_layers'])):
                raise ValueError('Invalid or unapproved zone/layer pair')
            selected[row['uuid']] = list(row['layers'])
        _region(task)
        return selected
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'zone_uuids'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['zone_uuids'], list) or not value['zone_uuids']
            or any(not isinstance(x, str) or not x for x in value['zone_uuids'])
            or len(set(value['zone_uuids'])) != len(value['zone_uuids'])):
        raise ValueError('Invalid explicit zone refill permission')
    if policy['route_layers'] != [task['allowed_layer']] or policy['max_new_vias']:
        raise ValueError('Explicit zone refill requires a single routing layer without new vias')
    # Endpoint identity is independently bound by endpoints.validate(), the
    # native model and the loaded PNS worker. Pads, fixed line ends and existing
    # standard through vias use the same fill permission: it grants no motion
    # of those endpoint objects and no changes to other layers' filled copper.
    # Do not substitute a pad-only test for those actual geometry witnesses.
    _region(task)
    return {uid: [task['allowed_layer']] for uid in value['zone_uuids']}


def _region(task):
    roi = task['region_mm']
    if len(roi) != 4 or not all(math.isfinite(x) for x in roi) or roi[0] >= roi[2] or roi[1] >= roi[3]:
        raise ValueError('Invalid zone refill region')


def search_model(source):
    """Keep all nonselected copper; cut only selected fill inside the task ROI."""
    selected = [uid for uid, layers in layer_contract(source['task']).items()
                if source['task']['allowed_layer'] in layers]
    if not selected:
        return source, None
    import shapely
    from shapely.geometry import box, Polygon
    from .pns_geometry import copper
    task = source['task']
    region = box(*task['region_mm'])
    rows = {p['uuid']: p for p in source['primitives'] if p.get('source_kind') == 'zone_fill'}
    if not set(selected) <= set(rows):
        raise ValueError('Selected zone is absent from the allowed layer')
    result = copy.deepcopy(source)
    proof = []
    for uid in selected:
        row = rows[uid]
        if (row['net'] == task['target_net'] or not row['net'] or row.get('locked', True)
                or row.get('source_owner_uuid') is not None):
            raise ValueError('Only unlocked board zones on a foreign net may participate in refill')
        before = copper(row['polygons'])
        removed = before.intersection(region)
        if removed.is_empty:
            raise ValueError('Selected zone has no filled copper in the authorized region')
        after = before.difference(region)
        required = after
        # Clipping an integer segment can produce sub-nanometre intersections.
        # Grow and snap OUTWARD, then prove coverage; never round missing copper
        # away. Extra solver obstacle is conservative, not a rule relaxation.
        if not after.is_empty:
            snapped = shapely.set_precision(after, .000001)
            if not snapped.equals(after):
                after = shapely.set_precision(after.buffer(.000002, join_style='mitre'), .000001)
                if not after.is_valid or not after.covers(required):
                    raise ValueError('Integer refill obstacle cover lost protected copper')
        polygons = []
        for poly in ([] if after.is_empty else [after] if after.geom_type == 'Polygon' else after.geoms):
            if poly.geom_type != 'Polygon':
                raise ValueError('Refill obstacle clipping produced unsupported geometry')
            shell = [[round(x, 6), round(y, 6)] for x, y in list(poly.exterior.coords)[:-1]]
            holes = [[[round(x, 6), round(y, 6)] for x, y in list(r.coords)[:-1]] for r in poly.interiors]
            if not Polygon(shell, holes).equals(poly):
                raise ValueError('Refill boundary intersection exceeds native integer precision')
            polygons.append(dict(shell=shell, holes=holes))
        if not copper(polygons).equals(after) or (not required.is_empty and not after.covers(required)) or not region.covers(before.difference(after)):
            raise ValueError('Refill obstacle clipping escaped its permission')
        next(p for p in result['primitives'] if p.get('source_kind') == 'zone_fill' and p['uuid'] == uid)['polygons'] = polygons
        proof.append(dict(uuid=uid, net=row['net'], layer=task['allowed_layer'], removed_search_area_mm2=before.difference(after).area,
                          outside_region_copper_preserved=True, extra_obstacle_area_mm2=after.difference(required).area))
    return result, dict(schema_version=1, zone_uuids=selected, region_mm=task['region_mm'], zones=proof,
                        source_refilled_sha256=source['refilled_geometry_sha256'],
                        policy='Search-only local fill permission; native refill, exact outside/other-zone equality, connectivity and full DRC remain mandatory')
