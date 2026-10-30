"""Conservative operation selection for local refill, never an acceptance gate.

Moving a track can refill its old clearance corridor beyond the task region.
Freeze boundary-sensitive whole tracks and independently rebuild the remaining
terminal witness. This heuristic cannot prove that every excluded motion would
fail, nor that a retained motion cannot disconnect a distant copper island.
"""
import copy
from decimal import Decimal
from .modification import contract, witness
from .zone_refill import contract as refill_contract


def _number(value):
    if type(value) not in (float, int):raise ValueError('Missing native refill influence parameter')
    result=Decimal(str(value))
    if not result.is_finite() or result < 0:raise ValueError('Invalid native refill influence parameter')
    return result


def select(snapshot, task, partial=False):
    """Return a new task with no greater permissions and an auditable decision."""
    zones=refill_contract(task);spec=contract(task)
    if not zones or not spec['routing_uuids']:
        return copy.deepcopy(task), None
    witness(snapshot,task)
    candidates=snapshot.get('zone_refill_choices',[])
    chosen=[]
    for uid in zones:
        matches=[z for z in candidates if z['uuid']==uid and task['allowed_layer'] in z['layers']]
        if len(matches)!=1:raise ValueError('Refill selection needs one native unlocked board zone per UUID')
        if matches[0]['net']==task['target_net']:raise ValueError('Target-net zones cannot be selected for refill')
        chosen.append(matches[0])
    rules=snapshot['routing_rules'];floor=_number(rules['board_minima_mm']['clearance'])
    # Minimum thickness is an additional conservative margin, not a claim of
    # an exact bound on the zone filler's morphological or island operations.
    zone_clearance=max(max(floor,_number(z['local_clearance_mm']),
                        _number(rules['netclasses_mm'][z['net']]['clearance'])) for z in chosen)
    thickness_margin=max(_number(z['min_thickness_mm']) for z in chosen)
    roi=list(map(lambda x:Decimal(str(x)),task['region_mm']))
    rows=[];retained=[];frozen=[];segments=[]
    for uid in spec['routing_uuids']:
        row=snapshot['items'][uid]
        margin=max(zone_clearance,_number(row['netclass']['clearance_mm']),_number(row['own_clearance_mm']))+thickness_margin+_number(row['width_mm'])/2
        a,b=row['start_mm'],row['end_mm']
        a=[Decimal(str(x)) for x in a];b=[Decimal(str(x)) for x in b]
        bounds=[min(a[0],b[0])-margin,min(a[1],b[1])-margin,max(a[0],b[0])+margin,max(a[1],b[1])+margin]
        inside=bounds[0]>=roi[0] and bounds[1]>=roi[1] and bounds[2]<=roi[2] and bounds[3]<=roi[3]
        plan=None
        if partial and not inside and spec['mode'] in ('shove','shove+ripup'):
            from .partial_segments import split_segment
            plan=split_segment(dict(uuid=uid,net=row['net'],start=row['start_mm'],end=row['end_mm'],
                width_mm=row['width_mm'],locked=row.get('locked',False),problem_layer=task['allowed_layer']),
                task['region_mm'],float(margin))
            if plan:segments.append(plan)
        (retained if inside or plan else frozen).append(uid)
        rows.append(dict(uuid=uid,net=row['net'],influence_bounds_mm=list(map(float,bounds)),
            margin_mm=float(margin),decision='retain' if inside else 'partial' if plan else 'freeze',
            reason='estimated_clearance_corridor_inside_roi' if inside else 'fixed_tails_with_movable_inset_middle' if plan else 'old_clearance_corridor_may_refill_outside_roi'))
    child=copy.deepcopy(task)
    if retained:
        child['modification']['routing_uuids']=retained
        child['modification']['nets']=sorted({snapshot['items'][u]['net'] for u in retained})
    else:
        child.pop('modification',None);child.pop('requested_modification_nets',None)
    if segments:child['partial_segments']=segments
    subset_witness=witness(snapshot,child)
    return child, dict(schema_version=1,status='narrowed' if frozen else 'unchanged',
        original_modification=copy.deepcopy(spec),effective_modification=contract(child),
        frozen_uuids=frozen,retained_uuids=retained,partial_uuids=[p['parent_uuid'] for p in segments],tracks=rows,region_mm=task['region_mm'],
        selected_zone_parameters=copy.deepcopy(chosen),terminal_witness=subset_witness,
        acceptance_unchanged=True,physical_impossibility_proven=False,
        policy='Conservative permission subset; selected boundary-sensitive tracks may have fixed tails and an exact movable middle. No claim of bounded global refill effects; full native acceptance is mandatory.')
