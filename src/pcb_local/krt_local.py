"""Isolated adapter for pinned KRT's existing endpoint-override A* interface."""
import argparse
import json
import math
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_local.workspace import sha256, source_files
from pcb_local.krt_result import candidate,checked_result
from pcb_local.task_policy import permissions


def validate_endpoints(request, pcb):
    """Rebind native endpoints to parsed objects before passing override points.

    KRT has no pad UUID field; pad copper centres must match the native anchor
    as well as reference/number. Offset pads remain unsupported here.
    """
    from pcb_local.validation import read_board,field
    from geometry_utils import via_copper_layers
    task=request['task'];layer=task['allowed_layer']
    ids=[n for n,v in pcb.nets.items() if v.name==task['target_net']]
    if len(ids)!=1:raise ValueError('Ambiguous KRT target net')
    nid=ids[0];pads=pcb.pads_by_net.get(nid,[])
    if len(pads)!=request['source_net_pad_count']:raise ValueError('KRT/native target pad inventory mismatch')
    bindings=task.get('target_endpoints')
    if bindings:
        if len(bindings)!=2 or len(request['endpoints'])!=2 or len({b['uuid'] for b in bindings})!=2:
            raise ValueError('Two distinct native endpoint bindings required')
        for b,e in zip(bindings,request['endpoints']):
            if (any(b[k]!=e[k] for k in ('uuid','kind','net','position_mm')) or
                b['net']!=task['target_net'] or b['layer']!=layer or layer not in e['copper_layers'] or
                b['selector'] not in (('start','end') if b['kind']=='segment' else ('center',))):
                raise ValueError('Local KRT request and native endpoint binding disagree')
    raw=read_board(Path(request['board'])) if task.get('target_endpoints') else None
    for e in request['endpoints']:
        kind=e.get('kind','pad')
        if kind=='pad':
            matches=[p for p in pads if p.component_ref==e['reference'] and p.pad_number==e['number']
                     and math.dist([p.global_x,p.global_y],e['position_mm'])<=1e-8
                     and (layer in p.layers or '*.Cu' in p.layers)]
            if len(matches)!=1:raise ValueError('KRT/native pad position or identity mismatch')
        elif kind=='segment':
            matches=[s for s in pcb.segments if s.uuid==e['uuid']]
            if len(matches)!=1:raise ValueError('KRT/native segment identity mismatch')
            s=matches[0];node=raw['routing'][e['uuid']]
            if (s.net_id!=nid or s.layer!=layer or [s.start_x,s.start_y]!=e['start_mm'] or
                [s.end_x,s.end_y]!=e['end_mm'] or s.width!=float(field(node,'width')[1]) or s.width!=e['width_mm']):
                raise ValueError('KRT/native segment geometry or net mismatch')
            binding=next(b for b in task['target_endpoints'] if b['uuid']==e['uuid'])
            actual=e['start_mm'] if binding['selector']=='start' else e['end_mm'] if binding['selector']=='end' else None
            if actual!=e['position_mm']:raise ValueError('KRT native segment selector mismatch')
        elif kind=='via':
            matches=[v for v in pcb.vias if v.uuid==e['uuid']]
            if len(matches)!=1:raise ValueError('KRT/native via identity mismatch')
            v=matches[0];node=raw['routing'][e['uuid']]
            if (v.net_id!=nid or [v.x,v.y]!=e['position_mm'] or v.via_type!='through' or
                e.get('standard_through') is not True or
                set(via_copper_layers(v,pcb.board_info.copper_layers))!=set(e['copper_layers']) or
                layer not in e['copper_layers'] or v.size!=float(field(node,'size')[1]) or v.drill!=float(field(node,'drill')[1])):
                raise ValueError('KRT/native through-via geometry or span mismatch')
        else:raise ValueError('Unsupported local KRT native endpoint')
    return nid,pads


def segment_path(segments, net_id, layer, width):
    """Do not invent bridges, discard branches, or accept a narrower result."""
    points=[]
    for s in segments:
        values=(s.start_x,s.start_y,s.end_x,s.end_y,s.width)
        if not all(math.isfinite(v) for v in values) or s.net_id!=net_id or s.layer!=layer or abs(s.width-width)>1e-9:
            raise ValueError('Local KRT changed the requested net/layer/width')
        start=[s.start_x,s.start_y];end=[s.end_x,s.end_y]
        if points and math.dist(points[-1],start)>1e-9:
            raise ValueError('Local KRT returned noncontiguous segments; no bridge may be invented')
        if not points:points.append(start)
        points.append(end)
    if len(points)<2:
        raise ValueError('Local KRT returned no segments')
    return points


def route(request, krt_root):
    sys.path.insert(0,str(krt_root/'py_router'))
    sys.path.insert(0,str(krt_root/'rust_router'))
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig,GridCoord
    from obstacle_map import build_base_obstacle_map,_add_rectangular_edge_obstacles
    from single_ended_routing import route_net_with_obstacles
    from kicad_dru import install_layer_clearances,install_track_clearances
    from pour_guard import install_pour_barriers
    from fab_tiers import set_escalation_policy, enforce_fab_floors
    board=Path(request['board']);task=request['task'];plan=request['routing_plan']
    if sha256(board)!=request['board_sha256']:raise ValueError('Local KRT source board changed')
    pcb=parse_kicad_pcb(str(board))
    nid,pads=validate_endpoints(request,pcb);layer=task['allowed_layer']
    policy=permissions(task,pcb.board_info.copper_layers)
    floors=plan['native_rules']['board_minima_mm'];nc=plan['native_rules']['netclasses_mm'][task['target_net']]
    width=max(floors['track_width'],nc['track_width'])
    drill=max(floors['via_drill'],nc['via_drill'])
    clearances={n:plan['net_clearances_mm'][v.name] for n,v in pcb.nets.items() if v.name in plan['net_clearances_mm']}
    cfg=GridRouteConfig(track_width=width,clearance=clearances[nid],
        via_size=max(floors['via_diameter'],nc['via_diameter'],drill+2*floors['via_annular_width']),via_drill=drill,
        layers=policy['route_layers'],max_rip_up_count=0,board_edge_clearance=floors['board_edge_clearance'],
        hole_clearance=floors['hole_clearance'],hole_to_hole_clearance=floors['hole_to_hole_clearance'])
    # Optional patched KRT resource labels constrain search, rather than only
    # rejecting an over-budget result. Single-layer search retains its old path.
    # Fail closed on a mismatched binary through the patched setter call.
    cfg.local_max_new_vias = policy['max_new_vias'] if len(cfg.layers) > 1 else None
    # Match the existing CLI's off policy and physical fabrication floors;
    # bypassing its CLI must not implicitly enable a size-descent ladder.
    set_escalation_policy('off')
    clamps=enforce_fab_floors(len(pcb.board_info.copper_layers),**{k:getattr(cfg,k) for k in
        ('track_width','clearance','via_size','via_drill','hole_to_hole_clearance','board_edge_clearance')})
    for key,value in clamps.items():setattr(cfg,key,value)
    cfg.via_size=max(cfg.via_size,cfg.via_drill+2*floors['via_annular_width'])
    width=cfg.track_width
    clearances[nid]=max(clearances[nid],cfg.clearance)
    cfg.set_net_clearances(clearances,[nid])
    install_layer_clearances(cfg,None,str(board),pcb)
    install_track_clearances(cfg,None,str(board),pcb,routed_net_ids=[nid])
    pour_report=install_pour_barriers(cfg,pcb,[nid],[task['target_net']])
    # Full board copper/holes/keepouts remain. This fence is an additional
    # search constraint; final copper-envelope permission checks are unchanged.
    obstacles=build_base_obstacle_map(pcb,cfg,[nid],net_clearances=clearances)
    coord=GridCoord(cfg.grid_step);roi=task['region_mm']
    lo=coord.to_grid(*roi[:2]);hi=coord.to_grid(*roi[2:])
    cfg.local_via_budget_bounds = (*lo, *hi)
    _add_rectangular_edge_obstacles(obstacles,coord,len(cfg.layers),*lo,*hi,math.ceil(max(width,cfg.via_size if policy['max_new_vias'] else width)/2/cfg.grid_step)+1,1)
    rows=[(*coord.to_grid(*e['position_mm']),cfg.layers.index(layer),*e['position_mm']) for e in request['endpoints']]
    result=route_net_with_obstacles(pcb,nid,cfg,obstacles,bounds=(*lo,*hi),sources_override=[rows[0]],targets_override=[rows[1]])
    report=dict(mode='native_endpoints' if task.get('target_endpoints') else 'selected_pad_pair',source_net_pad_count=len(pads),endpoint_uuids=[e['uuid'] for e in request['endpoints']],
                full_board_objects_retained=True,grid_mm=cfg.grid_step,width_mm=width,clearance_mm=clearances[nid],
                profile=request['profile'],pour_guard=pour_report,
                escalation_policy='off',fabrication_clamps=clamps,
                search_via_limit=cfg.local_max_new_vias,
                policy='Pinned upstream A* with explicit endpoints and bounded via labels; native acceptance required')
    if not result or result.get('failed'):
        return None,dict(report,status='no_progress')
    if policy['route_layers']==[layer] and not policy['max_new_vias']:
        segment_path(result['new_segments'],nid,layer,width)
    copper=checked_result(result,nid,task,pcb.board_info.copper_layers,width,cfg.via_size,cfg.via_drill)
    return copper,dict(report,status='proposed',iterations=result.get('iterations'),segments=len(copper['segments']),vias=len(copper['vias']),route_layers=policy['route_layers'])


def main():
    p=argparse.ArgumentParser();p.add_argument('--request',type=Path,required=True);p.add_argument('--krt-root',type=Path,required=True)
    p.add_argument('--candidate',type=Path,required=True);p.add_argument('--summary',type=Path,required=True);a=p.parse_args()
    request=json.loads(a.request.read_text());board=Path(request['board']);original=source_files(board)
    try:
        result,report=route(request,a.krt_root)
        if source_files(board)!=original:raise ValueError('Local KRT modified source context')
        if result is not None:
            # Parent reserved engine-output for logs. Write the board into a
            # fresh child then move its complete context into that directory.
            import shutil
            made=candidate(board,request['task'],result,a.candidate.parent/'local-candidate')
            for item in made.parent.iterdir():
                dest=a.candidate.parent/item.name
                if dest.exists():raise ValueError('Candidate output path collision')
                shutil.move(str(item),str(dest))
            made.parent.rmdir()
        a.summary.write_text(json.dumps(report,indent=2)+'\n');return 0
    except Exception as exc:
        a.summary.write_text(json.dumps(dict(status='unsupported',reason=type(exc).__name__+': '+str(exc)),indent=2)+'\n');return 2

if __name__=='__main__':raise SystemExit(main())
