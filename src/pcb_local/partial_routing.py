"""Reconstruct solver-only subdivisions from independently read native copper."""
import copy
from decimal import Decimal
from .modification import contract
from .partial_segments import verify_plan
from .zone_refill import contract as refill_contract


def validated_plans(model):
    task=model['task']; plans=task.get('partial_segments',[])
    if not plans:return []
    spec=contract(task)
    if spec['mode'] not in ('shove','shove+ripup'):
        raise ValueError('Partial segments require existing shove permission')
    zones=refill_contract(task)
    if not zones and any(p.get('schema_version')!=2 for p in plans):
        raise ValueError('Partial refill segments require explicit zone permission')
    rows={p['uuid']:p for p in model['primitives']}
    if len({p['parent_uuid'] for p in plans})!=len(plans):
        raise ValueError('Duplicate partial parent identity')
    def number(value):
        if type(value) not in (int,float):raise ValueError('Missing native partial margin')
        n=Decimal(str(value))
        if not n.is_finite() or n<0:raise ValueError('Invalid native partial margin')
        return n
    selected_zones=[rows[z] for z in zones]
    if any(z.get('source_kind')!='zone_fill' or z.get('locked',True) or z.get('source_owner_uuid') is not None for z in selected_zones):
        raise ValueError('Partial segments require witnessed unlocked board zones')
    clearance=max([number(model['minimum_clearance_mm'])]+[number(z['clearance_mm']) for z in selected_zones])
    thickness=max([Decimal(0)]+[number(z['min_thickness_mm']) for z in selected_zones])
    derived=[];used=set(rows)
    for plan in plans:
        uid=plan['parent_uuid']
        if uid not in spec['routing_uuids']:raise ValueError('Partial parent is not authorized')
        row=dict(rows[uid],problem_layer=task['allowed_layer'])
        margin=max(clearance,number(row['clearance_mm']))+thickness+number(row['width_mm'])/2
        verified=verify_plan(row,task['region_mm'],float(margin),plan,
                             allow_crossing=plan.get('schema_version')==2)
        for child in verified['children']:
            if child['uuid'] in used:raise ValueError('Partial segment identity collision')
            used.add(child['uuid'])
        derived.append(verified)
    return derived


def expand(model, plans=None):
    plans=validated_plans(model) if plans is None else plans
    by_parent={p['parent_uuid']:p for p in plans}
    result=copy.deepcopy(model);result['primitives']=[]
    for row in model['primitives']:
        plan=by_parent.get(row['uuid'])
        result['primitives'].extend([dict(row,**child) for child in plan['children']] if plan else [copy.deepcopy(row)])
    return result
