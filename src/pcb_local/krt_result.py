"""Permission-checked additive KRT copper, serialized onto the original copy."""
import json
import math
from pathlib import Path
import uuid

from .task_policy import permissions
from .validation import parse_sexpr,name,semantic_string,Quoted
from .workspace import copy_project


def checked_result(result, nid, task, copper_layers, width, via_size, via_drill):
    policy=permissions(task,copper_layers);segments=[];vias=[]
    for s in result.get('new_segments',[]):
        values=(s.start_x,s.start_y,s.end_x,s.end_y,s.width)
        if (not all(math.isfinite(v) for v in values) or s.net_id!=nid or
            s.layer not in policy['route_layers'] or abs(s.width-width)>1e-9):
            raise ValueError('Local KRT changed requested net, layer or width')
        segments.append(dict(start=[s.start_x,s.start_y],end=[s.end_x,s.end_y],width=s.width,layer=s.layer))
    for v in result.get('new_vias',[]):
        if (not all(math.isfinite(x) for x in (v.x,v.y,v.size,v.drill)) or v.net_id!=nid or
            v.via_type!='through' or list(v.layers)!=['F.Cu','B.Cu'] or abs(v.size-via_size)>1e-9 or abs(v.drill-via_drill)>1e-9 or
            set(policy['route_layers'])!=set(copper_layers)):
            raise ValueError('Local KRT proposed unauthorized via dimensions or span')
        vias.append(dict(position=[v.x,v.y],size=v.size,drill=v.drill))
    if len(vias)>policy['max_new_vias']:raise ValueError('Local KRT exceeded original via budget')
    if not segments:raise ValueError('Local KRT returned no segments')
    return dict(segments=segments,vias=vias)


def candidate(before,task,result,folder):
    before=Path(before);text=before.read_text(encoding='utf-8');root=parse_sexpr(text)[0]
    codes=[n[1] for n in root[1:] if name(n)=='net' and len(n)>=3 and semantic_string(n[2])==task['target_net']]
    if len(codes)==1 and str(codes[0]).isdigit():token=str(codes[0])
    elif not codes and not any(name(n)=='net' for n in root[1:]):
        def has_net(node):
            return isinstance(node,list) and (name(node)=='net' and len(node)==2 and isinstance(node[1],Quoted) and semantic_string(node[1])==task['target_net'] or any(has_net(c) for c in node[1:] if isinstance(c,list)))
        if not has_net(root):raise ValueError('No supported named target net representation')
        token=json.dumps(task['target_net'],ensure_ascii=False)
    else:raise ValueError('Ambiguous target net representation')
    rows=[]
    for s in result['segments']:
        start=[round(x*1e6)/1e6 for x in s['start']];end=[round(x*1e6)/1e6 for x in s['end']]
        if start==end:continue
        rows.append('(segment (start %.9f %.9f) (end %.9f %.9f) (width %.9f) (layer %s) (net %s) (uuid "%s"))'%(*start,*end,s['width'],json.dumps(s['layer']),token,uuid.uuid4()))
    for v in result['vias']:
        p=[round(x*1e6)/1e6 for x in v['position']]
        rows.append('(via (at %.9f %.9f) (size %.9f) (drill %.9f) (layers "F.Cu" "B.Cu") (net %s) (uuid "%s"))'%(*p,v['size'],v['drill'],token,uuid.uuid4()))
    if not rows:raise ValueError('No nonzero copper after native quantization')
    end=text.rfind(')')
    if end<0 or text[end+1:].strip():raise ValueError('Unexpected trailing board data')
    board,_=copy_project(before,folder)
    board.write_text(text[:end]+'\n'+'\n'.join(rows)+'\n'+text[end:],encoding='utf-8')
    return board
