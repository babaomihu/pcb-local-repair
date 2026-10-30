"""Optional shortening of additive layer chains and isolated new-via positions.

Every original object remains fixed. Each chain first uses legal chords of its
vertices. A bounded new-via pass requires complete physical-layer hole witnesses
and strictly decreasing total length. Supporting-line intersections may spend
those savings to reduce segment count while keeping total length <= raw. An
extra contact freezes the chain/via.
This proposes copper only; trace_quality performs full native acceptance.
"""
import copy
from decimal import Decimal
import math
from pathlib import Path
import time

from pcb_local.jobs import Halt
from pcb_local.validation import canonical, field, name, parse_sexpr, read_board
from pcb_local.workspace import copy_project, source_files


def partition(before, candidate, task, model, check=lambda: None):
    from pcb_local.trace_quality import _point
    old, new = read_board(before), read_board(candidate)
    if task.get('modification') or task.get('zone_refill'):
        raise ValueError('Layered quality requires additive fixed-copper tasks')
    if old['static'] != new['static'] or any(
            uid not in new['routing'] or canonical(n) != canonical(new['routing'][uid])
            for uid, n in old['routing'].items()):
        raise ValueError('Original definitions changed; layered quality is ineligible')
    added = {u:n for u,n in new['routing'].items() if u not in old['routing']}
    if not 2 <= len(added) <= 512:
        raise ValueError('Layered quality requires 2–512 newly added routing objects')
    layers = task['permissions']['route_layers']
    if set(model['routing_models']) != set(layers):
        raise ValueError('Missing per-layer quality witnesses')
    graphs = {}; segments = {}; via_points = {}; widths = set()
    for uid,n in new['routing'].items():
        check()
        if name(n) == 'via' and new['identities'][uid]['net'] == task['target_net']:
            span = list(map(str, field(n,'layers')[1:]))
            if span != ['F.Cu','B.Cu']:
                if uid in added: raise ValueError('Only ordinary through vias are supported')
                continue
            via_points[uid] = _point(field(n,'at')[1:3])
        if uid not in added: continue
        if new['identities'][uid]['net'] != task['target_net'] or name(n) not in ('segment','via'):
            raise ValueError('Layered quality only rewrites target-net straight segments')
        if name(n) == 'via': continue
        if any(name(child) not in ('start','end','width','layer','net','uuid','tstamp') for child in n[1:]):
            raise ValueError('Unsupported segment attributes cannot be rewritten')
        layer = str(field(n,'layer')[1])
        if layer not in layers: raise ValueError('Added segment uses an unauthorized layer')
        a,b = [_point(field(n,k)[1:3]) for k in ('start','end')]
        width = _point(field(n,'width')[1:2])[0]
        if a==b or width<=0: raise ValueError('Degenerate added segment')
        widths.add(width); segments[uid] = n
        graph = graphs.setdefault(layer,{})
        graph.setdefault(a,[]).append((b,uid));graph.setdefault(b,[]).append((a,uid))
    if len(widths)!=1: raise ValueError('Variable-width layered candidates are not optimized')
    width=next(iter(widths))/1e6;chains=[]
    for layer,graph in graphs.items():
        # Degree != 2 vertices and every fixed/new via centre are immutable
        # anchors. Splitting junctions preserves all side branches as well.
        anchors={p for p,v in graph.items() if len(v)!=2}|(set(via_points.values())&set(graph))
        seen=set()
        for start in sorted(anchors):
            for neighbor,uid in sorted(graph[start]):
                if uid in seen:continue
                points=[start];ids=[];point=start
                while True:
                    check();seen.add(uid);ids.append(uid);points.append(neighbor);point=neighbor
                    if point in anchors:break
                    options=[x for x in graph[point] if x[1] not in seen]
                    if len(options)!=1:raise ValueError('Ambiguous/cyclic layered quality chain')
                    neighbor,uid=options[0]
                chains.append(dict(layer=layer,points=[[x/1e6,y/1e6] for x,y in points],uuids=ids,
                    fixed_contact_ids=[u for u,p in via_points.items() if p in (points[0],points[-1])]))
        layer_ids={u for entries in graph.values() for _,u in entries}
        if seen!=layer_ids:raise ValueError('Closed or disconnected loop cannot be discarded')
    return chains,width,segments,via_points


def reposition_new_vias(plans,new_vias,model,width,check=lambda:None):
    """Bounded local moves of new vias only, with full-stack hole witnesses.

Only two simple incident chains are considered. Any extra copper contact
freezes the via. Every changed incident segment and quantized hole centre must
pass the original conservative model; the final candidate is still unaccepted.
    """
    from shapely.geometry import Point,LineString
    from pcb_local.geometry_search import configuration_space
    from pcb_local.trace_quality import _shape
    if not new_vias or set(model.get('via_models',{}))!=set(model.get('copper_layers',[])) or 'hole_to_hole_clearance_mm' not in model:
        return {},dict(status='not_attempted',reason='No complete full-stack new-hole witness')
    positions={u:list(map(float,field(n,'at')[1:3])) for u,n in new_vias.items()}
    free_by_layer={};via_free={};moves={};attempts=0
    gap=float(model['hole_to_hole_clearance_mm'])
    for layer,m in model['routing_models'].items():
        projected=copy.deepcopy(m);projected['endpoints']=copy.deepcopy(model['endpoints'])
        projected['minimum_track_width_mm']=max(width,projected['minimum_track_width_mm'])
        for e in projected['endpoints']:e['track_width_mm']=max(width,e['track_width_mm'])
        free_by_layer[layer]=configuration_space(projected,check)[0]
    # At most four holes are refined. This is an optional, bounded quality pass,
    # not a new search budget or permission to move original vias.
    for uid,n in list(sorted(new_vias.items()))[:4]:
        if any(name(c)=='locked' or c=='locked' for c in n[1:]):continue
        check();old=positions[uid];radius=float(field(n,'size')[1])/2;drill=float(field(n,'drill')[1])
        contacts=[]
        for i,c in enumerate(plans):
            if c['result'][0]==old:contacts.append((i,0,1))
            if c['result'][-1]==old:contacts.append((i,-1,-2))
        if len(contacts)!=2 or any(plans[i].get('kept_reason') for i,_,_ in contacts):continue
        # Old same-net contacts belong to the accepted result too. They cannot
        # be detached by moving a newly created via away from them.
        extra_contact=False
        for m in model['routing_models'].values():
            for p in m['primitives']:
                check()
                if p['net']==model['task']['target_net'] and _shape(p).distance(Point(old))<=radius+.00001:
                    extra_contact=True;break
            if extra_contact:break
        if extra_contact:continue
        for i,c in enumerate(plans):
            if i not in {x[0] for x in contacts} and LineString(c['result']).distance(Point(old))<=radius+width/2+.00001:
                extra_contact=True;break
        if extra_contact:continue
        key=(radius,drill)
        if key not in via_free:
            space=None
            for m in model['via_models'].values():
                projected=copy.deepcopy(m);projected['endpoints']=copy.deepcopy(model['endpoints'])
                projected['minimum_track_width_mm']=radius*2
                for e in projected['endpoints']:e['track_width_mm']=radius*2
                for p in projected['primitives']:
                    p['blocks_same_net']=True
                    if p['kind']=='hole':p['hole_clearance_mm']=max(p['hole_clearance_mm'],gap)
                area=configuration_space(projected,check)[0]
                space=area if space is None else space.intersection(area)
            via_free[key]=space
        neighbors=[plans[i]['result'][near] for i,_,near in contacts]
        line=LineString(neighbors);choices=[]
        if line.length:
            choices.append(list(line.interpolate(line.project(Point(old))).coords[0]))
        choices.append([(neighbors[0][j]+neighbors[1][j])/2 for j in range(2)])
        for neighbor in neighbors:
            for fraction in (.25,.5,.75):choices.append([old[j]+fraction*(neighbor[j]-old[j]) for j in range(2)])
        best=old;best_cost=sum(math.dist(old,p) for p in neighbors)
        for raw in choices:
            check();q=[round(v,6) for v in raw];attempts+=1
            cost=sum(math.dist(q,p) for p in neighbors)
            if cost>=best_cost-1e-9 or not via_free[key].covers(Point(q)):continue
            if any(math.dist(q,p)<(drill+float(field(new_vias[u],'drill')[1]))/2+gap+.00002 for u,p in positions.items() if u!=uid):continue
            if any(not free_by_layer[plans[i]['layer']].covers(LineString([q,plans[i]['result'][near]])) for i,_,near in contacts):continue
            best=q;best_cost=cost
        if best!=old:
            positions[uid]=best;moves[uid]=best
            for i,end,_ in contacts:plans[i]['result'][end]=list(best)
    return moves,dict(status='proposed' if moves else 'unchanged',candidates_checked=attempts,moved_new_via_count=len(moves),new_via_positions_mm=moves,
        policy='Only new holes; full-stack free space, original hole gap, fixed originals and strictly decreasing adjacent length')


def _intersection(a,b,c,d):
    """Intersect supporting lines in integer nanometres, then quantize once."""
    from fractions import Fraction
    from pcb_local.trace_quality import _point
    a,b,c,d=map(_point,(a,b,c,d))
    u=[b[k]-a[k] for k in (0,1)];v=[d[k]-c[k] for k in (0,1)]
    det=u[0]*v[1]-u[1]*v[0]
    if not det:return None
    t=Fraction((c[0]-a[0])*v[1]-(c[1]-a[1])*v[0],det)
    return [round(a[k]+t*u[k])/1e6 for k in (0,1)]


def _changed_edges_witness(model,width,check):
    """Conservative board/complex geometry, exact circular/capsule distances.

The analytic alternative retains the SAME 10 nm extra safety margin. It avoids
false blockage from the circumscribed arc polygon, not physical clearances.
Unknown/complex objects and board-outline uncertainty keep their old model.
    """
    from shapely.geometry import Point,LineString
    from pcb_local.geometry_search import configuration_space,SAFETY_MM
    projected=copy.deepcopy(model)
    projected['minimum_track_width_mm']=max(width,projected['minimum_track_width_mm'])
    for e in projected['endpoints']:e['track_width_mm']=max(width,e['track_width_mm'])
    free,actual_width,clearance,_=configuration_space(projected,check)
    residual=copy.deepcopy(projected);analytic=[];remaining=[]
    for p in projected['primitives']:
        check()
        if p['net']==projected['task']['target_net'] and not p.get('blocks_same_net'):
            remaining.append(p);continue
        if p['kind']=='segment':
            shape=LineString([p['start'],p['end']]);radius=float(p['width_mm'])/2
        elif p['kind']=='circle' and len(set(p['size']))==1:
            shape=Point(p['center']);radius=float(p['size'][0])/2
        else:remaining.append(p);continue
        analytic.append((shape,actual_width/2+max(clearance,float(p['clearance_mm']))+radius+SAFETY_MM))
    residual['primitives']=remaining
    residual_free=configuration_space(residual,check)[0] if analytic else free
    def witness(line):
        check()
        if free.covers(line):return 'conservative_polygon'
        if not analytic or not residual_free.covers(line):return None
        for shape,distance in analytic:
            check()
            if line.distance(shape)<distance:return None
        return 'analytic_round_primitives_same_safety'
    return witness


def tighten_intersections(plans,model,width,check=lambda:None):
    """Spend only prior shortening credit on bounded supporting-line corners.

Independent implementation inspired by Freerouting/FASTR spatial line
repositioning. No copied source. No fixed board names or waypoint coordinates.
    """
    from shapely.geometry import LineString
    length=lambda p:sum(math.dist(a,b) for a,b in zip(p,p[1:]))
    raw=sum(length(c['points']) for c in plans)
    current=sum(length(c['result']) for c in plans)
    evidence=dict(status='unchanged',raw_length_mm=raw,starting_length_mm=current,
        candidates_checked=0,removed_segments=0,rounds=[],policy='Total length <= raw; same 10 nm safety; full native acceptance required')
    # A bounded scan only. Candidates beyond the original five-second quality
    # deadline are never granted a separate search budget.
    for index,c in enumerate(plans):
        check()
        if c.get('kept_reason') or len(c['result'])<4:continue
        # Preserve incidental contacts with other added chains, including those
        # which do not coincide with graph endpoints.
        if any(other['layer']==c['layer'] and index!=j and
                LineString(c['result']).distance(LineString(other['result']))<=width+.00001
                for j,other in enumerate(plans)):
            continue
        m=copy.deepcopy(model['routing_models'][c['layer']]);m['endpoints']=copy.deepcopy(model['endpoints'])
        witness=_changed_edges_witness(m,width,check)
        for iteration in range(8):
            check();p=c['result'];best=None
            for i in range(len(p)-3):
                for j in range(i+2,min(i+10,len(p)-1)):
                    check();q=_intersection(p[i],p[i+1],p[j],p[j+1])
                    if q is None or q in (p[i],p[j+1]):continue
                    evidence['candidates_checked']+=1
                    delta=length([p[i],q,p[j+1]])-length(p[i:j+2])
                    if current+delta>raw:continue
                    trial=p[:i+1]+[q]+p[j+1:]
                    if not LineString(trial).is_simple:continue
                    how=witness(LineString([p[i],q,p[j+1]]))
                    if not how:continue
                    rank=(-(j-i-1),delta,i,j)
                    if best is None or rank<best[0]:best=(rank,trial,delta,how)
            if best is None:break
            _,trial,delta,how=best
            removed=len(p)-len(trial);c['result']=trial;current+=delta
            evidence['removed_segments']+=removed
            evidence['rounds'].append(dict(layer=c['layer'],removed_segments=removed,length_delta_mm=delta,witness=how))
    evidence.update(status='proposed' if evidence['removed_segments'] else 'unchanged',final_length_mm=current,
        length_increase_vs_prior_quality_mm=current-evidence['starting_length_mm'])
    return evidence


def propose(before,candidate,task,model,folder,timeout=5):
    from pcb_local.trace_quality import _point, shorten
    from pcb_local.scope_candidate import _serialize
    started=time.monotonic();baseline,original=source_files(before),source_files(candidate)
    def check():
        if time.monotonic()-started>=timeout:raise TimeoutError('Layered quality generation sub-budget exhausted')
    evidence=dict(status='unchanged',method='per-layer-chords-via-refinement-line-intersections-and-tangent-arcs',requires_native_verification=True,
        fixed_vias=True,chain_results=[])
    try:
        chains,width,nodes,via_points=partition(before,candidate,task,model,check)
        before_ids=set(read_board(before)['routing'])
        new_via_nodes={u:n for u,n in read_board(candidate)['routing'].items() if name(n)=='via' and u not in before_ids}
        replacements=[];rewritten=[];retained=[];plans=[];length=lambda ps:sum(math.dist(a,b) for a,b in zip(ps,ps[1:]))
        total_before=total_after=0.;segments_before=segments_after=0
        for chain in chains:
            check();points=chain['points'];result=points;reason=None
            m=copy.deepcopy(model['routing_models'][chain['layer']])
            # Auxiliary layer snapshots may have zero pad endpoints; use the
            # primary target's native rule witnesses, preserving actual layer
            # primitive clearances. Via-centre contacts stay at the same point.
            m['endpoints']=copy.deepcopy(model['endpoints'])
            # A via may touch a segment away from its centre (no exact graph
            # vertex). Such an incidental attachment must not be shortened off.
            for uid,n in new_via_nodes.items():
                m['primitives'].append(dict(uuid=uid,kind='circle',net=task['target_net'],
                    center=[x/1e6 for x in via_points[uid]],size=[float(field(n,'size')[1])]*2,
                    clearance_mm=model['minimum_clearance_mm']))
            for uid in chain['fixed_contact_ids']:
                m['endpoints'].append(dict(uuid=uid,center=[x/1e6 for x in via_points[uid]],
                    track_width_mm=width,clearance_mm=model['minimum_clearance_mm']))
            try:
                if len(points)>2:result=shorten(points,width,m,check,keep_native_edges=True)
            except ValueError as exc:
                reason=str(exc)
            plans.append(dict(chain,result=copy.deepcopy(result),kept_reason=reason))
        moves,move_evidence=reposition_new_vias(plans,new_via_nodes,model,width,check)
        evidence.update(via_refinement=move_evidence,fixed_vias=not bool(moves))
        evidence['intersection_refinement']=tighten_intersections(plans,model,width,check)
        from pcb_local.arc_quality import propose as propose_arc
        arc_count=0
        for chain in plans:
            points,result=chain['points'],chain['result'];reason=chain.get('kept_reason')
            arc=None
            # Multiple chains may have incidental same-net contacts. Until a
            # swept-copper contact proof exists, keep those layers unchanged by
            # the arc pass. Earlier chord/intersection safeguards still apply.
            if not reason and sum(c['layer']==chain['layer'] for c in plans)==1:
                m=copy.deepcopy(model['routing_models'][chain['layer']])
                m['endpoints']=copy.deepcopy(model['endpoints'])
                # Preserve every original same-net contact; shorten() already
                # froze chains with incidental contacts, including added vias.
                arc=propose_arc(result,width,m,check)
            old_length,new_length=length(points),length(result)
            pieces=len(result)-1
            if arc:
                new_length=arc['length_mm'];pieces=3;arc_count+=1
            if len(result)>len(points):raise ValueError('Layered quality increased segment count')
            total_before+=old_length;total_after+=new_length
            segments_before+=len(points)-1;segments_after+=pieces-int(bool(arc))
            row=dict(layer=chain['layer'],before_segments=len(points)-1,after_segments=pieces-int(bool(arc)),after_arcs=int(bool(arc)),
                before_length_mm=old_length,after_length_mm=new_length,before_anchors=[points[0],points[-1]],after_anchors=[result[0],result[-1]],
                fixed_contact_ids=chain['fixed_contact_ids'])
            if arc:row['tangent_arc']=arc
            if reason:row['kept_reason']=reason
            evidence['chain_results'].append(row)
            if result==points and not arc:continue
            identities=chain['uuids'];rewritten.extend(identities)
            parts=([('segment',arc['start'],arc['tangent_start'],None),
                ('arc',arc['tangent_start'],arc['tangent_end'],arc['mid']),
                ('segment',arc['tangent_end'],arc['end'],None)] if arc else
                [('segment',a,b,None) for a,b in zip(result,result[1:])])
            for uid,(kind,a,b,mid) in zip(identities,parts):
                n=copy.deepcopy(nodes[uid]);retained.append(uid)
                n[0]=kind
                for key,p in [('start',a),('end',b)]:
                    field(n,key)[1:3]=[str(Decimal(v)/1000000) for v in _point(p)]
                if mid:n.append(['mid',*[str(Decimal(v)/1000000) for v in _point(mid)]])
                replacements.append(n)
        evidence.update(before_length_mm=total_before,after_length_mm=total_after,
            before_segments=segments_before,after_segments=segments_after,after_arcs=arc_count,unchanged_via_uuids=sorted(set(via_points)-set(moves)))
        if total_after>total_before+1e-9:raise ValueError('Layered quality increased total length')
        if not rewritten:return evidence
        for uid,q in moves.items():
            n=copy.deepcopy(new_via_nodes[uid]);field(n,'at')[1:3]=[str(Decimal(v)/1000000) for v in _point(q)]
            rewritten.append(uid);retained.append(uid);replacements.append(n)
        check();tree=parse_sexpr(candidate.read_text())[0];removed=set(rewritten)
        tree[:]=[tree[0]]+[n for n in tree[1:] if not(name(n) in ('segment','via') and
            str((field(n,'uuid') or field(n,'tstamp'))[1]) in removed)]+replacements
        proposed,_=copy_project(candidate,folder);proposed.write_text(_serialize(tree)+'\n')
        old,new=read_board(candidate),read_board(proposed)
        if old['static']!=new['static'] or any(canonical(n)!=canonical(new['routing'].get(u))
                for u,n in old['routing'].items() if u not in removed):
            raise Halt('protection_violation','Layered quality changed a protected object')
        check();evidence.update(status='proposed',candidate_board=str(proposed),candidate_hashes=source_files(proposed),
            input_candidate_hashes=original,rewritten_added_uuids=rewritten,retained_new_uuids=retained)
        return evidence
    finally:
        if source_files(before)!=baseline or source_files(candidate)!=original:
            raise Halt('protection_violation','Layered quality generation changed an input')
