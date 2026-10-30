"""Independent final-board preservation of solver-declared fixed tails.

The native generator separately reconstructs margins. This gate checks exact
original collinearity and tail preservation; it grants no additional authority.
"""
from fractions import Fraction
from .partial_segments import verify_plan


def check(before, after, task):
    from .validation import field, name, canonical
    from .modification import contract
    errors=[];plans=task.get('partial_segments',[]);seen=set()
    authorized=set(contract(task)['routing_uuids'])
    for plan in plans:
        try:
            uid=plan['parent_uuid']
            if uid not in authorized or uid in seen:raise ValueError('Unauthorized or duplicate partial parent')
            seen.add(uid);node=before['routing'][uid]
            if name(node)!='segment':raise ValueError('Partial parent is not a straight segment')
            row=dict(uuid=uid,kind='segment',net=before['identities'][uid]['net'],
                start=list(map(float,field(node,'start')[1:3])),end=list(map(float,field(node,'end')[1:3])),
                width_mm=float(field(node,'width')[1]),problem_layer=str(field(node,'layer')[1]))
            verify_plan(row,task['region_mm'],float(Fraction(plan['margin_nm'])/1000000),plan,
                        allow_crossing=plan.get('schema_version')==2)
            ids={p['uuid'] for p in plan['children']}
            if ids & before['stored_uuids']:raise ValueError('Partial child collides with original identity')
            if uid in after['routing']:
                if canonical(after['routing'][uid])!=canonical(node) or ids & after['stored_uuids']:
                    raise ValueError('Unreplaced partial parent changed or duplicated')
                continue
            for child in (plan['children'][0],plan['children'][-1]):
                tail=after['routing'][child['uuid']]
                if (name(tail)!='segment' or after['identities'][child['uuid']]['net']!=row['net']
                        or str(field(tail,'layer')[1])!=row['problem_layer']
                        or float(field(tail,'width')[1])!=row['width_mm']
                        or any(list(map(float,field(tail,key)[1:3]))!=child[key] for key in ('start','end'))):
                    raise ValueError('Fixed partial tail changed')
                if plan.get('schema_version')==2:
                    # Crossing tails may lie outside the ROI. Their entire
                    # authored metadata must therefore remain original as well.
                    without_geometry=lambda n:[x for x in n if name(x) not in ('uuid','tstamp','start','end')]
                    if canonical(without_geometry(tail))!=canonical(without_geometry(node)):
                        raise ValueError('Fixed crossing tail metadata changed')
        except (ValueError,KeyError,TypeError,IndexError,ZeroDivisionError) as exc:
            errors.append(dict(parent_uuid=plan.get('parent_uuid'),reason=str(exc)))
    return errors
