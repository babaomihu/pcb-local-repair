"""Bounded multilayer visibility proposals from native per-layer witnesses.

No grid coarsening, obstacle removal or permission expansion. Via sites are a
finite deterministic sample of full-stack drill-safe space, not a completeness
claim. All proposals still require the original native whole-board acceptance.
"""
import argparse
import copy
import heapq
import json
import math
from pathlib import Path
import sys
import time
import uuid
from decimal import Decimal

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_local.geometry_search import configuration_space, SearchLimit, candidate as single_candidate
from pcb_local.task_policy import permissions
from pcb_local.workspace import source_files
from pcb_local.validation import parse_sexpr, name, field
from pcb_local.scope_candidate import _serialize

MAX_NODES = 2400
MAX_STATES = 50000


def fixed_via_bridges(model, check=lambda: None):
    """Cross-layer witnesses for unchanged, same-net ordinary through vias.

    A coordinate or a same-net copper polygon alone is not a layer bridge.
    Every physical layer must independently report the same native identity,
    span, centre, drill and normal padstack. No blind/buried/removable layers
    are inferred. The search uses only authorized layers at those centres.
    """
    stack = model['copper_layers']
    observations = {}
    for layer in stack:
        check()
        rows = {}
        for row in model['via_models'][layer].get('vias', []):
            check()
            uid = row.get('uuid')
            if not isinstance(uid, str) or not uid or uid in rows:
                raise ValueError('Missing or duplicate fixed-via witness identity')
            rows[uid] = row
        observations[layer] = rows
    primary = model['task']['allowed_layer']
    keys = ('uuid', 'net', 'center', 'diameter_mm', 'drill_mm', 'layers',
            'copper_layers', 'standard_through', 'locked')
    bridges = {}
    candidate_ids = sorted({uid for rows in observations.values() for uid, row in rows.items()
        if row.get('net') == model['task']['target_net'] and row.get('standard_through') is True})
    for uid in candidate_ids:
        check()
        row = observations[primary].get(uid)
        if row is None or row.get('net') != model['task']['target_net'] or row.get('standard_through') is not True:
            raise ValueError('Fixed-via primary-layer witness is missing or inconsistent')
        if (row.get('layers') != [stack[0], stack[-1]] or row.get('copper_layers') != stack or
                type(row.get('locked')) is not bool):
            raise ValueError('Incomplete ordinary full-stack fixed-via witness')
        try:
            center = row['center']
            exact = [Decimal(str(x)) * 1000000 for x in center]
            diameter, drill = float(row['diameter_mm']), float(row['drill_mm'])
            if (len(exact) != 2 or any(not x.is_finite() or x != x.to_integral_value() for x in exact) or
                    not math.isfinite(diameter) or not math.isfinite(drill) or not 0 < drill < diameter):
                raise ValueError('Invalid fixed-via dimensions/centre')
        except (KeyError, TypeError, ArithmeticError) as exc:
            raise ValueError('Invalid fixed-via dimensions/centre') from exc
        for layer in stack:
            other = observations[layer].get(uid)
            if other is None or any(other.get(k) != row.get(k) for k in keys):
                raise ValueError('Fixed-via native witnesses disagree across physical layers')
            # Routing-space witnesses must independently contain the same via.
            if layer in model['routing_models']:
                candidates = [v for v in model['routing_models'][layer].get('vias', []) if v.get('uuid') == uid]
                if len(candidates) != 1 or any(candidates[0].get(k) != row.get(k) for k in keys):
                    raise ValueError('Fixed-via routing and drill witnesses disagree')
        point = tuple(float(x / 1000000) for x in exact)
        if point in bridges:
            raise ValueError('Ambiguous coincident fixed-via bridges')
        bridges[point] = dict(uuid=uid, net=row['net'], center=list(point),
            copper_layers=list(stack), locked=row['locked'], diameter_mm=diameter, drill_mm=drill)
    return bridges


def search(model, timeout=30):
    """Clip only explicitly selected layer fills; keep native endpoints fixed."""
    deadline=time.monotonic()+timeout
    def check():
        if time.monotonic()>=deadline:
            raise SearchLimit('Multilayer refill preparation exhausted the original allocation', 'deadline')
    check()
    task=model['task'];proofs=[]
    if task.get('zone_refill'):
        from pcb_local.zone_refill import layer_contract, search_model
        selected=layer_contract(task)
        model=copy.deepcopy(model)
        for group in ('routing_models','via_models'):
            for layer, source in list(model[group].items()):
                check()
                if source['task'] != dict(task,allowed_layer=layer):
                    raise ValueError('Refill layer witness differs from the bound task')
                if not any(layer in layers for layers in selected.values()):
                    continue  # Unselected physical layers retain their complete fixed geometry.
                clipped, proof=search_model(source)
                model[group][layer]=clipped
                if proof:proofs.append(dict(group=group,layer=layer,proof=proof))
    result=_search_core(model,max(0.,deadline-time.monotonic()))
    if proofs:
        result['zone_refill_search']=dict(layers=proofs,original_task_unchanged=model['task']==task,
                                        native_scope_verification_required=True)
    return result


def _search_core(model, timeout=30):
    import numpy as np
    import shapely
    from shapely.geometry import Point
    from shapely.ops import nearest_points
    deadline=time.monotonic()+timeout
    def check():
        if time.monotonic()>=deadline:
            raise SearchLimit('Multilayer visibility exhausted its original generation allocation', 'deadline')
    check()
    task=model['task'];policy=permissions(task,model['copper_layers'])
    if task.get('modification'):
        raise ValueError('Requires additive routing permission')
    layers=policy['route_layers'];primary=layers.index(task['allowed_layer'])
    if set(model['routing_models'])!=set(layers) or set(model['via_models'])!=set(model['copper_layers']):
        raise ValueError('Incomplete per-layer track or via witness')
    bridges=fixed_via_bridges(model,check)
    endpoints=model['endpoints'];start,end=[tuple(p['center']) for p in endpoints]
    diameter=float(model['via_rules']['diameter_mm']);drill=float(model['via_rules']['drill_mm'])
    hole_gap=float(model['hole_to_hole_clearance_mm'])
    if not all(math.isfinite(x) and x>=0 for x in (diameter,drill,hole_gap)) or not 0<drill<diameter:
        raise ValueError('Invalid native via rule dimensions')
    free=[];width=0.;clearance=0.
    for layer in layers:
        m=copy.deepcopy(model['routing_models'][layer]);m['endpoints']=copy.deepcopy(endpoints)
        area,w,c,_=configuration_space(m,check);free.append(area);width=max(width,w);clearance=max(clearance,c)
    # Use one width on every layer. Rebuild if layer-specific minima increase it.
    for i,layer in enumerate(layers):
        m=copy.deepcopy(model['routing_models'][layer]);m['endpoints']=copy.deepcopy(endpoints)
        m['minimum_track_width_mm']=width
        free[i]=configuration_space(m,check)[0]
    via_free=None
    for layer in model['copper_layers']:
        m=copy.deepcopy(model['via_models'][layer]);m['endpoints']=copy.deepcopy(endpoints)
        for p in m['endpoints']:p['track_width_mm']=diameter
        m['minimum_track_width_mm']=diameter
        # Via drills must avoid even same-net holes; conservatively avoid all
        # same-net copper too. Reusing an existing witnessed via is a separate
        # zero-new-hole transition and never creates a drill at that location.
        for item in m['primitives']:
            item['blocks_same_net']=True
            if item['kind']=='hole':
                item['hole_clearance_mm']=max(float(item['hole_clearance_mm']),hole_gap)
        area=configuration_space(m,check)[0]
        via_free=area if via_free is None else via_free.intersection(area)
    def parts(area):
        if area.is_empty:return []
        if area.geom_type=='Polygon':return [area]
        return [p for p in getattr(area,'geoms',[]) if p.geom_type=='Polygon']
    def vertices(area):
        return {tuple(p) for poly in parts(area) for ring in [poly.exterior,*poly.interiors] for p in ring.coords[:-1]}
    # Equal witnessed routing spaces are interchangeable for this add-only
    # target. Keep the primary layer as representative; via safety above still
    # includes EVERY physical layer, including those collapsed from search.
    representatives=[];collapsed={}
    for i in [primary]+[j for j in range(len(layers)) if j!=primary]:
        check()
        equivalent=next((j for j in representatives if free[i].equals(free[j])),None)
        if equivalent is None:representatives.append(i)
        else:collapsed[layers[i]]=layers[equivalent]
    layers=[layers[i] for i in representatives];free=[free[i] for i in representatives];primary=0
    # Only centres reachable on two authorized routing layers can serve as
    # bridges. Outside-ROI fixed vias stay in the native model/obstacle proof,
    # but must not consume the local graph's vertex budget.
    bridges={point:row for point,row in bridges.items()
        if sum(area.covers(Point(point)) for area in free)>=2}
    sites=vertices(via_free) if policy['max_new_vias'] else set()
    for poly in parts(via_free) if policy['max_new_vias'] else []:
        sites.add(tuple(poly.representative_point().coords[0]))
        for p in (start,end):sites.add(tuple(nearest_points(Point(p),poly)[1].coords[0]))
    coords_set={start,end}|sites|set(bridges)
    for area in free:coords_set.update(vertices(area))
    nodes=[start,end]+sorted(coords_set-{start,end})
    if len(nodes)>MAX_NODES:raise SearchLimit('Multilayer visibility vertex bound exceeded', 'vertex_limit', observed=len(nodes), maximum=MAX_NODES)
    coords=np.asarray(nodes,dtype=float)
    if not np.isfinite(coords).all():raise ValueError('Nonfinite native geometry')
    for area in free:shapely.prepare(area)
    shapely.prepare(via_free)
    valid=[shapely.covers(area,shapely.points(coords)) for area in free]
    via_ok=shapely.covers(via_free,shapely.points(coords))
    bridge_nodes={i:bridges[p] for i,p in enumerate(nodes) if p in bridges}
    if not valid[primary][0] or not valid[primary][1]:
        return dict(status='no_path',reason='Endpoint outside conservative primary-layer space')
    visible_cache={}
    def neighbors(layer,node):
        key=(layer,node)
        if key not in visible_cache:
            check();indices=np.flatnonzero(valid[layer]);indices=indices[indices!=node]
            pairs=np.stack([np.repeat(coords[node][None,:],len(indices),axis=0),coords[indices]],axis=1)
            good=shapely.covers(free[layer],shapely.linestrings(pairs))
            visible_cache[key]=[(int(j),math.dist(nodes[node],nodes[int(j)])) for j in indices[good]]
        return visible_cache[key]
    # Reverse resource-aware distances on the same visibility graph. This
    # optimistic problem ignores pairwise NEW-hole conflicts and path history,
    # but retains existing obstacles and the remaining transition budget. It
    # prevents Euclidean A* from burning labels toward an unusable near shortcut.
    cap=policy['max_new_vias'];lower={(primary,1,0):0.};reverse=[(0.,primary,1,0)];preexpanded=0
    while reverse:
        check();value,layer,node,used=heapq.heappop(reverse)
        if value!=lower.get((layer,node,used)):continue
        preexpanded+=1
        if preexpanded>MAX_STATES:raise SearchLimit('Reverse visibility resource-state bound exceeded', "state_limit", maximum=MAX_STATES)
        successors=[(layer,j,used,d) for j,d in neighbors(layer,node)]
        if node in bridge_nodes:
            successors.extend((other,node,used,0.) for other in range(len(layers)) if other!=layer and valid[other][node])
        if used<cap and via_ok[node]:
            successors.extend((other,node,used+1,diameter) for other in range(len(layers)) if other!=layer and valid[other][node])
        for other,j,k,delta in successors:
            alt=value+delta;key=(other,j,k)
            if alt<lower.get(key,math.inf):
                if key not in lower and len(lower)>=MAX_STATES or len(reverse)>=MAX_STATES:
                    raise SearchLimit("Reverse visibility retained-state bound exceeded", "state_limit", maximum=MAX_STATES)
                lower[key]=alt;heapq.heappush(reverse,(alt,other,j,k))
    def heuristic(layer,node,remaining):
        return min((lower.get((layer,node,k),math.inf) for k in range(remaining+1)),default=math.inf)
    if not math.isfinite(heuristic(primary,0,cap)):
        return dict(status='no_path',reason='No route on bounded visibility graph within via budget',reverse_expanded=preexpanded,vertices=len(nodes))
    initial=(primary,0,())
    costs={initial:0.};parent={};queue=[(heuristic(primary,0,cap),0.,initial)]
    expanded=0
    while queue:
        check();_,cost,state=heapq.heappop(queue)
        if cost!=costs.get(state):continue
        layer,node,holes=state
        if layer==primary and node==1:
            chain=[state]
            while chain[-1]!=initial:chain.append(parent[chain[-1]])
            chain.reverse()
            path=[dict(layer=layers[s[0]],point_mm=nodes[s[1]]) for s in chain]
            length=sum(math.dist(a['point_mm'],b['point_mm']) for a,b in zip(path,path[1:]))
            used_bridges=sorted({a[1] for a,b in zip(chain,chain[1:]) if a[0]!=b[0] and a[1] in bridge_nodes})
            return dict(status='found',method='native-full-stack-resource-visibility',path=path,
                        via_positions_mm=[nodes[j] for j in holes],width_mm=width,clearance_mm=clearance,
                        reused_vias=[bridge_nodes[j] for j in used_bridges],
                        via_diameter_mm=diameter,via_drill_mm=drill,length_mm=length,
                        expanded=expanded,reverse_expanded=preexpanded,vertices=len(nodes),equivalent_layer_representatives=collapsed,via_limit=policy['max_new_vias'],
                        limitation='Finite full-stack via sites; failure is not proof of physical infeasibility')
        expanded+=1
        if expanded+preexpanded>MAX_STATES or len(costs)>MAX_STATES:
            raise SearchLimit('Multilayer visibility resource-state bound exceeded', "state_limit", maximum=MAX_STATES)
        successors=[((layer,j,holes),d) for j,d in neighbors(layer,node)]
        if node in bridge_nodes:
            successors += [((other,node,holes),0.) for other in range(len(layers)) if other!=layer and valid[other][node]]
        if via_ok[node] and node not in holes:
            new=tuple(sorted((*holes,node)))
            if len(new)<=policy['max_new_vias'] and all(j==node or math.dist(nodes[j],nodes[node])>=drill+hole_gap+.00002 for j in holes):
                successors += [((other,node,new),0. if node in holes else diameter) for other in range(len(layers)) if other!=layer and valid[other][node]]
        for nxt,delta in successors:
            value=cost+delta
            estimate=heuristic(nxt[0],nxt[1],cap-len(nxt[2]))
            if math.isfinite(estimate) and value<costs.get(nxt,math.inf):
                if nxt not in costs and len(costs)>=MAX_STATES or len(queue)>=MAX_STATES:
                    raise SearchLimit("Forward visibility retained-state bound exceeded", "state_limit", maximum=MAX_STATES)
                costs[nxt]=value;parent[nxt]=state
                heapq.heappush(queue,(value+estimate,value,nxt))
    return dict(status='no_path',reason='No route on bounded conservative layer/via-site graph',expanded=expanded,vertices=len(nodes))


def candidate(before,task,result,folder):
    # Reuse the existing additive writer to preserve file/project context and
    # resolve numeric-vs-named KiCad net tokens, then emit the layer-aware path.
    path=result['path']
    pair=next(((a,b) for a,b in zip(path,path[1:]) if a['point_mm']!=b['point_mm']),None)
    if pair is None:raise ValueError('No nonzero segment in layered path')
    temporary=dict(path_mm=[pair[0]['point_mm'],pair[1]['point_mm']],width_mm=result['width_mm'])
    board=single_candidate(before,task,temporary,folder)
    roots=parse_sexpr(board.read_text());track=next(n for n in reversed(roots[0]) if name(n)=='segment')
    net=field(track,'net');text=before.read_text();items=[]
    from pcb_local.validation import Quoted
    def point(p):return [str(round(float(x),6)) for x in p]
    for a,b in zip(path,path[1:]):
        if a['layer']!=b['layer']:continue
        x,y=point(a['point_mm']),point(b['point_mm'])
        if x==y:continue
        items.append(['segment',['start',*x],['end',*y],['width',str(result['width_mm'])],['layer',Quoted(a['layer'])],net,['uuid',Quoted(str(uuid.uuid4()))]])
    for p in result['via_positions_mm']:
        items.append(['via',['at',*point(p)],['size',str(result['via_diameter_mm'])],['drill',str(result['via_drill_mm'])],['layers',Quoted('F.Cu'),Quoted('B.Cu')],net,['uuid',Quoted(str(uuid.uuid4()))]])
    end=text.rfind(')');board.write_text(text[:end]+'\n'+'\n'.join(_serialize(x) for x in items)+'\n'+text[end:])
    return board


def main():
    p=argparse.ArgumentParser();p.add_argument('--model',type=Path,required=True);p.add_argument('--board',type=Path,required=True);p.add_argument('--out-dir',type=Path,required=True);p.add_argument('--timeout',type=float,required=True);a=p.parse_args()
    a.out_dir.mkdir(parents=True,exist_ok=False);before=source_files(a.board);start=time.monotonic()
    try:
        doc=json.loads(a.model.read_text())
        if doc['status']!='ready':raise ValueError(doc.get('reason','Incomplete native model'))
        result=search(doc['model'],a.timeout)
        if result['status']=='found':
            board=candidate(a.board,doc['model']['task'],result,a.out_dir/'candidate')
            result.update(candidate_board=str(board),candidate_hashes=source_files(board),requires_native_verification=True)
    except SearchLimit as exc:result=exc.result()
    except Exception as exc:result=dict(status='unsupported',reason=type(exc).__name__+': '+str(exc))
    result.update(seconds=time.monotonic()-start,input_unchanged=source_files(a.board)==before)
    (a.out_dir/'search.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    return 0 if result['input_unchanged'] else 1

if __name__=='__main__':raise SystemExit(main())
