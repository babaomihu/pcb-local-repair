"""Explicit per-object local-middle authority for crossing straight tracks."""
import copy
from decimal import Decimal
from .partial_segments import split_segment, verify_plan
from .task_policy import permissions
from .zone_refill import contract as refill_contract


class NoLocalMiddle(ValueError):
    pass


def number(value):
    if type(value) not in (int,float):raise ValueError('Missing native local-track constraint')
    n=Decimal(str(value))
    if not n.is_finite() or n<0:raise ValueError('Invalid native local-track constraint')
    return n


def reconstruct(snapshot,task,uid,plan=None):
    policy=permissions(task)
    if policy['route_layers']!=[task['allowed_layer']] or policy['max_new_vias']:
        raise ValueError('Local-middle motion requires one layer and fixed vias')
    row=snapshot['items'][uid]
    if row['kind']!='segment' or row.get('locked') or row['copper_layers']!=[task['allowed_layer']]:
        raise ValueError('Local-middle parent must be an unlocked straight track on the permitted layer')
    floor=number(snapshot['routing_rules']['board_minima_mm']['clearance'])
    clearance=max(floor,number(row['netclass']['clearance_mm']),number(row['own_clearance_mm']))
    thickness=Decimal(0)
    for zid in refill_contract(task):
        zones=[z for z in snapshot.get('zone_refill_choices',[]) if z['uuid']==zid and task['allowed_layer'] in z['layers']]
        if len(zones)!=1:raise ValueError('Missing native local-track zone witness')
        z=zones[0]
        if z['net']==task['target_net']:raise ValueError('Target-net zone cannot be refilled')
        clearance=max(clearance,number(z['local_clearance_mm']),
                      number(snapshot['routing_rules']['netclasses_mm'][z['net']]['clearance']))
        thickness=max(thickness,number(z['min_thickness_mm']))
    margin=float(clearance+thickness+number(row['width_mm'])/2)
    primitive=dict(uuid=uid,kind='segment',net=row['net'],start=row['start_mm'],end=row['end_mm'],
                   width_mm=row['width_mm'],locked=row.get('locked',False),problem_layer=task['allowed_layer'])
    if plan is not None:
        return verify_plan(primitive,task['region_mm'],margin,plan,allow_crossing=True)
    result=split_segment(primitive,task['region_mm'],margin,allow_crossing=True)
    if result is None:raise NoLocalMiddle('No exact interior movable segment with fixed original tails: '+uid)
    return result


def select(snapshot,task,ids,mode='shove'):
    if mode not in ('shove','shove+ripup'):
        raise ValueError('Local-middle operation requires shove or explicitly combined rebuild permission')
    if (not isinstance(ids,list) or not 1<=len(ids)<=32 or len(set(ids))!=len(ids)
            or any(not isinstance(uid,str) or not uid for uid in ids)):
        raise ValueError('Select 1–32 distinct original local-track UUIDs')
    if task.get('modification') or task.get('partial_segments'):
        raise ValueError('Explicit local-track selection cannot mix other modification permissions')
    child=copy.deepcopy(task)
    child['partial_segments']=[reconstruct(snapshot,child,uid) for uid in ids]
    child['modification']=dict(schema_version=1,mode=mode,routing_uuids=list(ids),
        nets=sorted({snapshot['items'][u]['net'] for u in ids}),max_modified_tracks=len(ids))
    from .modification import witness
    proof=witness(snapshot,child)
    task.clear();task.update(child)
    return proof


def restrict_for_refill(snapshot,task):
    """Narrow a reviewed local-middle plan for newly selected fill constraints."""
    child=copy.deepcopy(task);plans=[];frozen=[]
    for old in task.get('partial_segments',[]):
        if old.get('schema_version')!=2:raise ValueError('Expected only reviewed local-middle plans')
        try:new=reconstruct(snapshot,task,old['parent_uuid'])
        except NoLocalMiddle:
            frozen.append(old['parent_uuid']);continue
        if (new['source_signature']!=old['source_signature'] or new['cut_indices'][0]<old['cut_indices'][0]
                or new['cut_indices'][1]>old['cut_indices'][1]):
            raise ValueError('Refill adaptation would widen reviewed local-middle authority')
        plans.append(new)
    if plans:
        child['partial_segments']=plans
        child['modification']['routing_uuids']=[p['parent_uuid'] for p in plans]
        child['modification']['nets']=sorted({p['source_signature']['net'] for p in plans})
    else:
        child.pop('partial_segments',None);child.pop('modification',None)
    from .modification import witness
    proof=witness(snapshot,child)
    task.clear();task.update(child)
    return dict(frozen_uuids=frozen,retained_uuids=[p['parent_uuid'] for p in plans],
                terminal_witness=proof,policy='Only narrower internal intervals; exact tails grow or original parent freezes; no added authority')
