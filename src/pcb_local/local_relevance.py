"""Prove whole fixed copper objects cannot affect an authorized local repair.

This filters solver copies only. The complete original board, connectivity and
DRC remain authoritative. No clipping, centre tests or target-net pruning.
"""
import hashlib
import json
from decimal import Decimal


def fixed_copper_relevance(model):
    from .geometry_search import native_polygon
    def number(value):
        n=Decimal(str(value))
        if not n.is_finite() or n<0:raise ValueError('Unknown/nonfinite local relevance rule bound')
        return n
    task=model['task'];roi=[Decimal(str(x)) for x in task['region_mm']]
    if len(roi)!=4 or any(not x.is_finite() for x in roi) or roi[0]>=roi[2] or roi[1]>=roi[3]:
        raise ValueError('Invalid local relevance ROI')
    # Netclass/own clearances come from the native snapshot. Conditional custom
    # rules are refused before this model is produced. Take a whole-model upper
    # bound, including old track widths, rather than only the target line width.
    clearance=max(number(model[k]) for k in ('minimum_clearance_mm','hole_clearance_mm','edge_clearance_mm'))
    half=number(model['minimum_track_width_mm'])/2
    for p in model['primitives']:
        clearance=max(clearance,number(p.get('clearance_mm',0)),number(p.get('hole_clearance_mm',0)))
        half=max(half,number(p.get('track_width_mm',0))/2,number(p.get('width_mm',0))/2)
    guard=clearance+half+Decimal('0.000102')
    box=[roi[0]-guard,roi[1]-guard,roi[2]+guard,roi[3]+guard]
    protected_nets=set(task.get('modification',{}).get('nets',[]))|{task.get('target_net')}
    protected_ids=set(task.get('modification',{}).get('routing_uuids',[]))|set(task.get('target_pad_uuids',[]))
    protected_ids.update(e['uuid'] for e in task.get('target_endpoints',[]))
    rows=[]
    for p in model['primitives']:
        if p.get('source_kind') not in ('fixed_graphic','zone_fill'):continue
        coords=[]
        for poly in p['polygons']:
            native_polygon(poly)  # Invalid/unwitnessed shapes are never hidden.
            for ring in [poly['shell'],*poly['holes']]:
                for xy in ring:
                    pair=[Decimal(str(v)) for v in xy]
                    if len(pair)!=2 or any(not v.is_finite() for v in pair):raise ValueError('Invalid fixed copper coordinate')
                    coords.append(pair)
        bounds=[min(x[0] for x in coords),min(x[1] for x in coords),max(x[0] for x in coords),max(x[1] for x in coords)] if coords else None
        separated=bool(bounds and (bounds[2]<box[0] or bounds[0]>box[2] or bounds[3]<box[1] or bounds[1]>box[3]))
        protected=not task.get('target_net') or p['net'] in protected_nets or p['uuid'] in protected_ids
        omit=separated and not protected
        rows.append(dict(uuid=p['uuid'],net=p['net'],source_kind=p['source_kind'],
            source_sha256=hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            bounds_mm=[float(x) for x in bounds] if bounds else None,omitted=omit,
            reason='whole_native_copper_bbox_strictly_outside_guard' if omit else 'target_or_authorized_context' if protected else 'overlaps_or_touches_guard' if bounds else 'empty_native_fill'))
    return dict(schema_version=1,layer=task.get('allowed_layer'),region_mm=task['region_mm'],
        guard_mm=float(guard),guarded_region_mm=[float(x) for x in box],objects=rows,
        omitted_uuids=sorted(r['uuid'] for r in rows if r['omitted']),
        policy='Whole fixed non-target/non-authorized copper objects only; crossing and touching objects retained; no clipping; full original-board validation remains required')
