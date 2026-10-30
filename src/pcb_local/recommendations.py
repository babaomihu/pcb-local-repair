"""Reviewable exact-object modification proposals. Generating one grants nothing."""
import copy
import hashlib
import json
import math
from .modification import fits, witness
from .discovery import select_task
from .signal_scope import restrictions


def point_segment(p,a,b):
    d=[b[i]-a[i] for i in range(2)];n=sum(v*v for v in d)
    t=max(0,min(1,sum((p[i]-a[i])*d[i] for i in range(2))/n)) if n else 0
    return math.dist(p,[a[i]+t*d[i] for i in range(2)])


def segments_distance(a,b,c,d):
    def cross(p,q,r):return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    if cross(a,b,c)*cross(a,b,d)<=0 and cross(c,d,a)*cross(c,d,b)<=0 and all(max(min(a[i],b[i]),min(c[i],d[i]))<=min(max(a[i],b[i]),max(c[i],d[i])) for i in range(2)):
        return 0.
    return min(point_segment(a,c,d),point_segment(b,c,d),point_segment(c,a,b),point_segment(d,a,b))


def recommend(snapshot, listing, check=None):
    output=[];items=snapshot['items'];scope_reasons=restrictions(snapshot)
    for target in listing['targets']:
        if check:check()
        if not target['supported']:continue
        ends=target.get('endpoints') or target['pads']
        if len(ends)!=2:continue
        task=select_task(listing,target=target['id']);points=[p['position_mm'] for p in ends]
        width=max((p.get('netclass') or {}).get('track_width_mm',0) for p in ends)
        gap=max((p.get('netclass') or {}).get('clearance_mm',0) for p in ends)
        groups={};hits={};local_groups={};local_hits={};crossing=set()
        for uid,row in items.items():
            if check:check()
            if row['net']==target['net'] or row['kind']!='segment' or scope_reasons.get(row['net']):continue
            comp=snapshot['component_of'][uid];whole=fits(row,task)
            clearance=max(gap,(row.get('netclass') or {}).get('clearance_mm',0),row.get('own_clearance_mm',0),
                          snapshot.get('routing_rules',{}).get('board_minima_mm',{}).get('clearance',0))
            intersects=segments_distance(*points,row['start_mm'],row['end_mm'])<=width/2+row['width_mm']/2+clearance
            if whole:
                groups.setdefault(comp,[]).append(uid)
                if intersects:hits.setdefault(comp,[]).append(uid)
            from .local_segments import reconstruct
            try:plan=reconstruct(snapshot,task,uid)
            except (ValueError,KeyError,TypeError):continue
            local_groups.setdefault(comp,[]).append(uid)
            if not whole:crossing.add(uid)
            if intersects:local_hits.setdefault(comp,[]).append(uid)
        for comp in sorted(hits):
            selected=sorted(groups[comp])
            if not selected or len(selected)>8:continue
            proposal=dict(schema_version=1,mode='shove',routing_uuids=selected,
                nets=sorted({items[u]['net'] for u in selected}),max_modified_tracks=len(selected))
            trial=copy.deepcopy(task);trial['modification']=proposal
            try:witness(snapshot,trial)
            except ValueError:continue
            objects=[dict(uuid=u,**{k:items[u][k] for k in ('kind','net','start_mm','end_mm','width_mm','copper_layers','locked')}) for u in selected]
            payload=dict(target_id=target['id'],region_mm=task['region_mm'],layer=task['allowed_layer'],modification=proposal,objects=objects,direct_corridor_hits=sorted(hits[comp]))
            payload['id']=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:20]
            payload['authority']='proposal_only; explicit acceptance required; exact listed straight tracks only'
            payload['reason']='Eligible anchored track component intersects the direct target corridor; possible benefit, not proof of a solution'
            output.append(payload)
        for comp in sorted(local_hits):
            selected=sorted(local_groups[comp])
            if not selected or len(selected)>8 or not set(selected)&crossing:continue
            trial=copy.deepcopy(task)
            from .local_segments import select
            try:select(snapshot,trial,selected)
            except (ValueError,KeyError,TypeError):continue
            plans={p['parent_uuid']:p for p in trial['partial_segments']}
            objects=[dict(uuid=u,**{k:items[u][k] for k in ('kind','net','start_mm','end_mm','width_mm','copper_layers','locked')},
                          editable_start_mm=plans[u]['children'][1]['start'],editable_end_mm=plans[u]['children'][1]['end']) for u in selected]
            payload=dict(target_id=target['id'],region_mm=task['region_mm'],layer=task['allowed_layer'],
                modification=trial['modification'],partial_segments=trial['partial_segments'],scope='local_middle',
                objects=objects,direct_corridor_hits=sorted(local_hits[comp]))
            payload['id']=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:20]
            payload['authority']='proposal_only; explicit acceptance required; only listed internal middles, exact original tails fixed'
            payload['reason']='Anchored crossing tracks may obstruct the target corridor; only their exact ROI-internal middles are eligible, not proof of a solution'
            output.append(payload)
    return output


def apply(snapshot, task, current, previous, accepted_ids, mode):
    if len(accepted_ids)!=1 or len(set(accepted_ids))!=1:
        raise ValueError('One reviewed recommendation is required per connection')
    uid=accepted_ids[0];old=[r for r in previous if r['id']==uid];fresh=[r for r in current if r['id']==uid]
    if len(old)!=1 or old!=fresh or old[0]['target_id']!=task['id']:
        raise ValueError('Recommendation absent, changed, or belongs to a different target')
    if mode not in ('shove','ripup','shove+ripup'):raise ValueError('Unknown recommendation operation')
    if old[0].get('scope')=='local_middle':
        if mode!='shove':raise ValueError('Local-middle recommendations currently support only witnessed PNS shove')
        task['partial_segments']=copy.deepcopy(old[0]['partial_segments'])
    task['modification']=copy.deepcopy(old[0]['modification']);task['modification']['mode']=mode
    return witness(snapshot,task)
