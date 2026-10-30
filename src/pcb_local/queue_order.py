"""Deterministic ordering of already-selected repairs, without new authority.

This is a scheduling heuristic from native item positions and straight endpoint
corridors, not a path-feasibility or physical-blockage proof. It never changes
an endpoint, ROI, target selection, permission or routing/validation budget.
"""
import math

MAX_TARGETS = 64
MAX_SCORED_OBJECTS = 20000


def _point(value):
    try:
        point = tuple(float(x) for x in value)
    except (TypeError, ValueError):
        raise ValueError('Queue endpoint coordinates must be finite x/y pairs')
    if len(point) != 2 or not all(math.isfinite(x) for x in point):
        raise ValueError('Queue endpoint coordinates must be finite x/y pairs')
    return point


def _point_distance(p, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    den = dx*dx + dy*dy
    t = min(1., max(0., ((p[0]-a[0])*dx + (p[1]-a[1])*dy)/den)) if den else 0.
    return math.dist(p, (a[0]+t*dx, a[1]+t*dy))


def _segment_distance(a, b, c, d):
    def cross(p, q, r):
        return (q[0]-p[0])*(r[1]-p[1]) - (q[1]-p[1])*(r[0]-p[0])
    crosses = cross(a,b,c)*cross(a,b,d) < 0 and cross(c,d,a)*cross(c,d,b) < 0
    if crosses:
        return 0.
    return min(_point_distance(a,c,d), _point_distance(b,c,d), _point_distance(c,a,b), _point_distance(d,a,b))


def _endpoints(target):
    values = target.get('endpoints') or target.get('target_endpoints') or target.get('pads')
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError('Queue ordering requires exactly two identity-bound endpoints per target')
    return values, [_point(x.get('position_mm')) for x in values]


def _nonnegative(value, default=0.):
    try:
        result=float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) and result >= 0 else default


def order_targets(targets, native_snapshot, layer):
    """Return (same selected target objects in scored order, audit evidence).

    Duplicate target IDs are rejected rather than silently deduplicated, because
    changing the user's chosen workload belongs to the caller. Equal heuristic
    scores retain the original input order. Evidence exposes finite sampling.
    """
    if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= MAX_TARGETS:
        raise ValueError('Queue ordering requires 1–64 selected targets')
    ids = [x.get('id') for x in targets]
    if any(not isinstance(uid, str) or not uid for uid in ids) or len(set(ids)) != len(ids):
        raise ValueError('Queue target IDs must be nonempty and unique')
    if layer not in native_snapshot.get('copper_layers', []):
        raise ValueError('Queue layer is absent from the native snapshot')
    items=native_snapshot.get('items', {})
    if not isinstance(items, dict):
        raise ValueError('Queue ordering requires native item identities')
    eligible=[(key,item) for key,item in sorted(items.items())
              if layer in item.get('copper_layers', []) and item.get('kind') in ('segment','arc','pad','via')]
    considered=eligible[:MAX_SCORED_OBJECTS]
    rows=[]
    geometries=[]
    for index,target in enumerate(targets):
        endpoint_rows,(a,b)=_endpoints(target)
        widths=[_nonnegative((x.get('netclass') or {}).get('track_width_mm'), .25) for x in endpoint_rows]
        clearances=[_nonnegative((x.get('netclass') or {}).get('clearance_mm'), .2) for x in endpoint_rows]
        # Defaults only rank tasks lacking display metadata; no rule is used to
        # generate or accept copper here. Actual routing reads original rules.
        corridor=max(widths)/2 + max(clearances)
        original_ids={x.get('uuid') for x in endpoint_rows}
        nearby=[];fixed_count=0
        for key,item in considered:
            if item.get('net') == target.get('net') or item.get('uuid') in original_ids:
                continue
            try:
                if item['kind']=='segment':
                    distance=_segment_distance(a,b,_point(item['start_mm']),_point(item['end_mm']))
                    threshold=corridor+_nonnegative(item.get('width_mm'))/2
                elif item['kind']=='arc':
                    # Curves cannot be represented by their chord. Use the
                    # native start/mid/end bounding box only as a nearby hint.
                    points=[_point(item[k]) for k in ('start_mm','mid_mm','end_mm')]
                    x0=min(p[0] for p in points);x1=max(p[0] for p in points)
                    y0=min(p[1] for p in points);y1=max(p[1] for p in points)
                    corners=[(x0,y0),(x1,y0),(x1,y1),(x0,y1)]
                    distance=0. if any(x0<=p[0]<=x1 and y0<=p[1]<=y1 for p in (a,b)) else min(_segment_distance(a,b,corners[j],corners[(j+1)%4]) for j in range(4))
                    threshold=corridor+_nonnegative(item.get('width_mm'))/2
                else:
                    distance=_point_distance(_point(item['position_mm']),a,b)
                    threshold=corridor
            except (KeyError,ValueError):
                continue
            if distance<=threshold:
                fixed=item['kind'] in ('pad','via','arc') or bool(item.get('locked'))
                fixed_count+=fixed
                nearby.append(dict(item_id=key,uuid=item.get('uuid'),kind=item['kind'],net=item.get('net'),
                                   fixed_type_or_locked=fixed,distance_mm=round(distance,6)))
        roi=target.get('region_mm')
        margin=None
        if roi is not None:
            if len(roi)!=4 or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in roi) or roi[0]>=roi[2] or roi[1]>=roi[3]:
                raise ValueError('Queue target has invalid fixed ROI')
            margin=min(min(p[0]-roi[0],roi[2]-p[0],p[1]-roi[1],roi[3]-p[1]) for p in (a,b))
            if margin<0:raise ValueError('Queue endpoint is outside its fixed ROI')
        rows.append(dict(id=target['id'],original_index=index,net=target.get('net'),nearby_object_count=len(nearby),
                         fixed_type_or_locked_count=fixed_count,nearby_objects=nearby[:32],
                         nearby_objects_truncated=len(nearby)>32,roi_endpoint_margin_mm=margin,
                         shared_corridor_targets=[]))
        geometries.append((a,b,corridor))
    for i,(a,b,width) in enumerate(geometries):
        for j in range(i+1,len(geometries)):
            c,d,other_width=geometries[j]
            if targets[i].get('net') != targets[j].get('net') and _segment_distance(a,b,c,d)<=width+other_width:
                rows[i]['shared_corridor_targets'].append(ids[j]);rows[j]['shared_corridor_targets'].append(ids[i])
    for row in rows:
        margin=row['roi_endpoint_margin_mm']
        tightness=0. if margin is None else 1./(1.+margin)
        score=4*row['fixed_type_or_locked_count']+2*row['nearby_object_count']+2*len(row['shared_corridor_targets'])+tightness
        row['score']=round(score,9)
        row['score_components']=dict(fixed_objects=4*row['fixed_type_or_locked_count'],
            nearby_objects=2*row['nearby_object_count'],shared_corridors=2*len(row['shared_corridor_targets']),
            roi_tightness=round(tightness,9))
        row['reason']='Higher native-object proximity, shared endpoint corridors and tighter fixed ROI are scheduled first; this is a heuristic, not proof of physical blockage'
    ordered=sorted(range(len(targets)),key=lambda i:(-rows[i]['score'],i))
    evidence=dict(schema_version=1,policy='constrained-first-v1',layer=layer,
                  original_order=ids,execution_order=[ids[i] for i in ordered],scores=rows,
                  objects_considered=len(considered),objects_eligible=len(eligible),
                  object_sampling_truncated=len(eligible)>MAX_SCORED_OBJECTS,
                  selection_unchanged=True,permissions_unchanged=True,regions_unchanged=True,
                  extra_attempts=0,physical_blockage_proven=False,
                  limitations='Straight endpoint corridors, segment distance, arc start/mid/end bounds and pad/via centers only. No filled-copper, pad outline, hole radius or complete routing-capacity model. Scores only order already-selected work; they grant no authority.')
    return [targets[i] for i in ordered],evidence
