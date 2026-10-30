"""Native geometry -> disposable PNS problem -> original-board routing delta.

KiCad 9 never rewrites a user's KiCad 10 board. Only a witnessed 2/4/6/8-layer
problem with one editable routing layer and fixed copper/vias is sent to PNS; its routing delta is applied to the original
AST and must then pass native KiCad 10 validation under original project rules.
"""
import copy
import json
from pathlib import Path
import uuid
from .modification import contract
from .layer_stack import supported_stack, serialized_copper_id
from .validation import Quoted, canonical, field, name, parse_sexpr, read_board, semantic_string
from .scope_candidate import _serialize, _routing_uuid
from .workspace import copy_project


class UnsupportedGeometry(ValueError):
    def __init__(self,message,items):
        super().__init__(message)
        self.items=[dict(uuid=p['uuid'],net=p.get('net',''),kind=p.get('kind','via'),
            layer=p.get('problem_layer',p.get('layers')),position_mm=p.get('center'),reason=message) for p in items]


def make_problem(board, task, model, netclasses, folder, projection=None):
    from .remote_vias import filtered
    if (projection or {}).get('remote_vias') is not None and model.get('task') != task:
        raise ValueError('PNS task differs from native model authority')
    model = filtered(model, (projection or {}).get('remote_vias'))
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=False)
    copper_layers = model.get('copper_layers', ['F.Cu','B.Cu'])
    if not supported_stack(copper_layers) or task['allowed_layer'] not in copper_layers:
        raise ValueError('PNS requires a witnessed 2/4/6/8-layer stack and an enabled task layer')
    models = {task['allowed_layer']:model,**model.get('auxiliary_layers',{})}
    if 'copper_layers' in model and set(models)!=set(copper_layers):
        raise ValueError('PNS requires native fixed geometry for every copper layer')
    projections = {task['allowed_layer']:projection or {'source_zone_uuids': [], 'tiles': []},**(projection or {}).get('auxiliary_layers',{})}
    if set(projections)!=set(models):raise ValueError('Missing per-layer fixed copper projection')
    primitives = [dict(p,problem_layer=layer) for layer,m in models.items() for p in m['primitives']]
    vias = {v['uuid']: v for m in models.values() for v in m.get('vias', [])}
    zones = {uid for p in projections.values() for key in ('source_zone_uuids','source_arc_uuids','source_custom_pad_uuids','source_graphic_uuids') for uid in p.get(key,[])}
    if any(not v['standard_through'] or v['layers'] != [copper_layers[0],copper_layers[-1]] for v in vias.values()):
        raise UnsupportedGeometry('PNS fixed vias require uniform full-stack through padstacks without secondary drills',[v for v in vias.values() if not v['standard_through'] or v['layers'] != [copper_layers[0],copper_layers[-1]]])
    if any('native_pad' in p and p['native_pad'] is None and p['uuid'] not in zones for p in primitives):
        raise UnsupportedGeometry('Unsupported native padstack or drilled/offset custom pad',[p for p in primitives if 'native_pad' in p and p['native_pad'] is None and p['uuid'] not in zones])
    pad_rows = {p['uuid']: p for p in primitives if p.get('native_pad')}
    arc_rows = {p['uuid']: p for p in primitives if p.get('native_arc')}
    if not set(arc_rows) <= {uid for p in projections.values() for uid in p.get('source_arc_uuids', [])}:
        raise ValueError('Fixed arcs require witnessed native envelope projection')
    for pad in pad_rows.values():
        spec = pad['native_pad']
        if spec['attribute'] in ('thru_hole', 'np_thru_hole') and (
                spec['copper_layers'] != copper_layers):
            raise ValueError('PNS drilled pads require uniform pads spanning the complete witnessed stack')
        if spec['attribute'] in ('smd', 'connect') and len(spec['copper_layers']) != 1:
            raise ValueError('PNS surface pad has unsupported copper layers')
    if any(p["kind"] not in {"circle", "rect", "segment", "keepout"} and p['uuid'] not in vias and
           p['uuid'] not in pad_rows and p['uuid'] not in arc_rows and
           p['uuid'] not in zones for p in primitives):
        raise ValueError("PNS adapter requires witnessed native pads, arcs, tracks and fixed through vias; unsupported copper is never omitted")
    selected = set(contract(task)["routing_uuids"])
    track_ids = {p["uuid"] for p in primitives if p["kind"] == "segment" and p["problem_layer"] == task["allowed_layer"]}
    if not selected <= track_ids:
        raise ValueError("Authorized modified track is absent from the native PNS problem")
    from .partial_routing import validated_plans, expand
    subdivisions=validated_plans(model)
    if subdivisions:
        models=dict(models);models[task['allowed_layer']]=expand(model,subdivisions)
        primitives=[dict(p,problem_layer=layer) for layer,m in models.items() for p in m['primitives']]
        selected.difference_update(p['parent_uuid'] for p in subdivisions)
        selected.update(p['middle_uuid'] for p in subdivisions)
    net_names = sorted({p["net"] for p in primitives if p["net"]})
    codes = {net:i+1 for i, net in enumerate(net_names)}; codes[""] = 0
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    layer = quote(task["allowed_layer"])
    serialized_layers=' '.join('(%d %s signal)' % (serialized_copper_id(n),quote(n)) for i,n in enumerate(copper_layers))
    rows = ['(kicad_pcb (version 20241229) (generator "pcb_local_native_problem")',
        '(general (thickness 1.6)) (layers '+serialized_layers+' (44 "Edge.Cuts" user)) (setup (pad_to_mask_clearance 0))']
    rows += ['(net %d %s)' % (code,quote(net)) for net,code in codes.items()]
    for i,edge in enumerate(model.get('outline_shapes') or [dict(kind='segment',start=a,end=b,width_mm=.05) for a,b in model['outline_edges']]):
        uid=str(uuid.uuid5(uuid.NAMESPACE_URL,'pcb-local-pns-edge:'+str(i)))
        kind=edge['kind'];width=edge['width_mm']
        if kind=='segment':form='gr_line (start %.6f %.6f) (end %.6f %.6f)' % (*edge['start'],*edge['end'])
        elif kind=='arc':form='gr_arc (start %.6f %.6f) (mid %.6f %.6f) (end %.6f %.6f)' % (*edge['start'],*edge['mid'],*edge['end'])
        elif kind=='circle':form='gr_circle (center %.6f %.6f) (end %.6f %.6f)' % (*edge['center'],*edge['end'])
        else:raise ValueError('Unsupported exact native outline shape')
        rows.append('(%s (stroke (width %.6f) (type default)) (layer "Edge.Cuts") (uuid "%s"))' % (form,width,uid))
    exported_pads=set()
    for p in primitives:
        layer=quote(p['problem_layer'])
        if p['uuid'] in vias or p['uuid'] in zones:
            continue
        if p['kind'] == 'hole' and p['uuid'] in pad_rows:
            continue  # The corresponding native pad exports its drilled hole.
        if p['uuid'] in pad_rows:
            if p['uuid'] in exported_pads:continue
            exported_pads.add(p['uuid'])
            spec = p['native_pad']; extra = ''
            drill = spec['drill']; offset = spec['offset']
            if any(drill) or any(offset):
                size = ('oval %.6f %.6f' % tuple(drill)) if spec['drill_shape'] == 'oval' else '%.6f' % drill[0]
                extra += '(drill %s (offset %.6f %.6f))' % (size, *offset)
            if spec['shape'] == 'trapezoid':
                extra += '(rect_delta %.6f %.6f)' % tuple(spec['delta'])
            if spec['shape'] == 'roundrect':
                extra += '(roundrect_rratio %.12g)' % spec['roundrect_ratio']
                if spec['chamfer_corners']:
                    corners = [label for bit,label in enumerate(('top_left','top_right','bottom_left','bottom_right')) if spec['chamfer_corners'] & (1<<bit)]
                    extra += '(chamfer_ratio %.12g) (chamfer %s)' % (spec['chamfer_ratio'], ' '.join(corners))
            layers = '"*.Cu" "*.Mask"' if spec['attribute'] in ('thru_hole','np_thru_hole') else layer
            fp = str(uuid.uuid5(uuid.NAMESPACE_URL, "pcb-local-pns-pad:"+p["uuid"]))
            rows.append('(footprint "PCBLocal:NativePad" (layer %s) (uuid "%s") (at %.6f %.6f) '
                '(pad "1" %s %s (at 0 0 %.6f) (size %.6f %.6f) %s (layers %s) (net %d %s) (clearance %.6f) (uuid "%s")))' %
                (layer,fp,*p['center'],spec['attribute'],spec['shape'],-p['angle_degrees'],*p['size'],extra,layers,codes[p['net']],quote(p['net']),p['clearance_mm'],p['uuid']))
        elif p['kind']=='keepout':
            polygons=' '.join('(polygon (pts '+ ' '.join('(xy %.6f %.6f)'%tuple(xy) for xy in ring)+'))' for polygon in p['polygons'] for ring in [polygon['shell'],*polygon['holes']])
            rows.append('(zone (net 0)(net_name "")(layer %s)(uuid "%s")(hatch edge .5)(connect_pads (clearance 0))(min_thickness .01)(keepout (tracks not_allowed)(vias not_allowed)(pads allowed)(copperpour allowed)(footprints allowed))(fill (thermal_gap .2)(thermal_bridge_width .2)) %s)'%(layer,p['uuid'],polygons))
        elif p["kind"] == "segment":
            rows.append('(segment (start %.6f %.6f) (end %.6f %.6f) (width %.6f) (layer %s) (net %d) (uuid "%s")%s)' %
                        (*p["start"], *p["end"], p["width_mm"], layer, codes[p["net"]], p["uuid"],
                         ' (locked yes)' if p["uuid"] not in selected else ''))
        else:
            fp = str(uuid.uuid5(uuid.NAMESPACE_URL, "pcb-local-pns-pad:"+p["uuid"]))
            rows.append('(footprint "PCBLocal:Primitive" (layer %s) (uuid "%s") (at %.6f %.6f) (attr smd) '
                        '(pad "1" smd %s (at 0 0 %.6f) (size %.6f %.6f) (layers %s) (net %d %s) (clearance %.6f) (uuid "%s")))' %
                        (layer, fp, *p["center"], p["kind"], -p["angle_degrees"], *p["size"], layer,
                         codes[p["net"]], quote(p["net"]), p["clearance_mm"], p["uuid"]))
    for anchor in [dict(a,problem_layer=l) for l,p in projections.items() for a in p.get('anchors',[])]:
        layer=quote(anchor['problem_layer'])
        fp = str(uuid.uuid5(uuid.NAMESPACE_URL, 'pcb-local-custom-anchor:'+anchor['uuid']))
        rows.append('(footprint "PCBLocal:CustomAnchor" (layer %s) (uuid "%s") (at %.6f %.6f) (attr smd) '
            '(pad "1" smd circle (at 0 0) (size .000002 .000002) (layers %s) (net %d %s) (clearance %.6f) (uuid "%s")))' %
            (layer,fp,*anchor['center'],layer,codes[anchor['net']],quote(anchor['net']),anchor['clearance_mm'],anchor['uuid']))
    for v in vias.values():
        rows.append('(via (at %.6f %.6f) (size %.6f) (drill %.6f) (layers "F.Cu" "B.Cu") (net %d) (uuid "%s") (locked yes))' %
                    (*v['center'], v['diameter_mm'], v['drill_mm'], codes[v['net']], v['uuid']))
    for tile in [dict(t,problem_layer=l) for l,p in projections.items() for t in p['tiles']]:
        layer=quote(tile['problem_layer'])
        fp = str(uuid.uuid5(uuid.NAMESPACE_URL, 'pcb-local-fill-fp:'+tile['uuid']))
        x,y = tile['center']
        pts = ' '.join('(xy %.6f %.6f)' % (a-x,b-y) for a,b in tile['points'])
        rows.append('(footprint "PCBLocal:FixedFill" (layer %s) (uuid "%s") (at %.6f %.6f) (attr smd) '
                    '(pad "1" smd custom (at 0 0) (size .000002 .000002) (layers %s) (net %d %s) '
                    '(clearance %.6f) (uuid "%s") (options (clearance outline) (anchor circle)) '
                    '(primitives (gr_poly (pts %s) (width 0) (fill yes)))))' %
                    (layer,fp,x,y,layer,codes[tile['net']],quote(tile['net']),tile['clearance_mm'],tile['uuid'],pts))
    for barrier in [dict(b,problem_layer=l) for l,p in projections.items() for b in p.get('region_barriers',[])]:
        layer=quote(barrier['problem_layer'])
        pts=' '.join('(xy %.6f %.6f)' % tuple(p) for p in barrier['polygons'][0]['shell'])
        rows.append('(zone (net 0) (net_name "") (layer %s) (uuid "%s") (hatch edge .5) '
                    '(connect_pads (clearance 0)) (min_thickness .01) '
                    '(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed)) '
                    '(fill (thermal_gap .2) (thermal_bridge_width .2)) (polygon (pts %s)))' % (layer,barrier['uuid'],pts))
    problem = folder / "board.kicad_pcb"
    root = parse_sexpr('\n'.join(rows)+'\n)\n')[0]
    for fp_node in (n for n in root[1:] if name(n)=='footprint'):
        pad_node=next(n for n in fp_node[1:] if name(n)=='pad')
        uid=str(field(pad_node,'uuid')[1])
        fp_node.append(['property',Quoted('Reference'),Quoted(uid),['at','0','0'],['layer',Quoted('F.SilkS')],['hide','yes'],['effects',['font',['size','.1','.1'],['thickness','.01']]]])
    problem.write_text(_serialize(root)+'\n')
    project = json.loads(Path(board).with_suffix('.kicad_pro').read_text())
    # KiCad 9's netclass reader requires priority; missing it drops an entire
    # class to defaults. Materialize only metadata from native KiCad 10.
    for nc in project.get("net_settings", {}).get("classes", []):
        if "priority" not in nc:
            nc["priority"] = netclasses["class_priorities"][nc["name"]]
    project.setdefault("net_settings", {})["meta"] = {"version": 4}
    rules = project.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
    for native, authored in (("minimum_clearance_mm", "min_clearance"), ("minimum_track_width_mm", "min_track_width"), ("edge_clearance_mm", "min_copper_edge_clearance")):
        rules[authored] = model[native]
    if 'hole_clearance_mm' in model:
        rules['min_hole_clearance'] = model['hole_clearance_mm']
    problem.with_suffix('.kicad_pro').write_text(json.dumps(project,indent=2)+'\n')
    return problem


def outline_identity(model):
    """Canonical exact edge geometry; IDs/owners change only in solver copies."""
    if 'outline_shapes' not in model:
        return sorted(json.dumps(e) for e in model['outline_edges'])
    result=[]
    for edge in model['outline_shapes']:
        kind=edge['kind'];row={k:edge[k] for k in ('kind','width_mm')}
        if kind=='segment':row['ends']=sorted([edge['start'],edge['end']])
        elif kind=='arc':row.update(ends=sorted([edge['start'],edge['end']]),mid=edge['mid'])
        elif kind=='circle':row.update(center=edge['center'],end=edge['end'])
        else:raise ValueError('Unsupported native outline identity')
        result.append(json.dumps(row,sort_keys=True))
    return sorted(result)


def geometry_identity(model):
    """Re-reading the PNS problem with native 10 must reproduce these values."""
    keys = {"uuid", "net", "kind", "center", "size", "angle_degrees", "start", "end", "width_mm", "clearance_mm", "track_width_mm", "polygons", "diameter_mm", "hole_clearance_mm", "native_pad", "native_arc"}
    return dict(copper_layers=model.get("copper_layers"), primitives=sorted((json.dumps({k:v for k,v in p.items() if k in keys},sort_keys=True) for p in model["primitives"])),
                vias=sorted(json.dumps(v,sort_keys=True) for v in model.get('vias', [])),
                edges=outline_identity(model),
                minima=[model.get(k) for k in ("minimum_clearance_mm", "minimum_track_width_mm", "edge_clearance_mm", "hole_clearance_mm")])


def segment_identity(parsed, uid):
    node = parsed["routing"][uid]
    if name(node) == 'via':
        return ('via', parsed['identities'][uid]['net'],
                tuple(float(v) for v in field(node, 'at')[1:3]),
                float(field(node, 'size')[1]), float(field(node, 'drill')[1]),
                tuple(str(v) for v in field(node, 'layers')[1:]),
                canonical([n for n in node[1:] if not isinstance(n, list)]))
    if name(node) == 'arc':
        return ('arc', parsed['identities'][uid]['net'], str(field(node, 'layer')[1]),
                float(field(node, 'width')[1]),
                tuple(tuple(float(v) for v in field(node,k)[1:3]) for k in ('start','mid','end')))
    if name(node) != "segment":
        raise ValueError("PNS problem/output contains unsupported non-straight routing")
    ends = sorted(tuple(float(v) for v in field(node, k)[1:3]) for k in ("start", "end"))
    return (parsed["identities"][uid]["net"], str(field(node, "layer")[1]), float(field(node, "width")[1]), ends)


def compose(board, problem, loaded, proposal, task, folder, partial_plans=None):
    original, expected, baseline, result = [read_board(Path(p)) for p in (board,problem,loaded,proposal)]
    if baseline['static'] != result['static']:
        raise ValueError('PNS proposal changed its fixed obstacle or task boundary definitions')
    if set(expected["routing"]) != set(baseline["routing"]) or any(segment_identity(expected,u) != segment_identity(baseline,u) for u in expected["routing"]):
        raise ValueError("PNS loaded geometry differs from the witnessed native routing problem")
    selected = set(contract(task)["routing_uuids"])
    partial_plans=partial_plans or []
    if partial_plans != task.get('partial_segments',[]):
        raise ValueError('Missing independently validated partial segment plans')
    partial_children={}; parents={}
    for plan in partial_plans:
        parent=plan['parent_uuid']
        if parent not in selected or parent not in original['routing']:
            raise ValueError('Partial parent is not original authorized routing')
        node=original['routing'][parent];sig=plan['source_signature']
        if (name(node)!='segment' or str(field(node,'layer')[1])!=sig['layer']
                or original['identities'][parent]['net']!=sig['net']
                or round(float(field(node,'width')[1])*1e6)!=sig['width_nm']
                or any([round(float(v)*1e6) for v in field(node,key)[1:3]]!=sig[key+'_nm'] for key in ('start','end'))):
            raise ValueError('Partial parent differs from original serialized geometry')
        for child in plan['children']:
            uid=child['uuid']
            if uid in original['stored_uuids'] or uid in partial_children:
                raise ValueError('Partial segment identity collides with original board')
            identity=(sig['net'],sig['layer'],sig['width_nm']/1e6,sorted(tuple(child[k]) for k in ('start','end')))
            if uid not in expected['routing'] or segment_identity(expected,uid)!=identity:
                raise ValueError('Partial problem child differs from its witnessed geometry')
            partial_children[uid]=parent
        parents[parent]=plan
        selected.remove(parent);selected.add(plan['middle_uuid'])
    changed = {u for u in set(baseline["routing"]) & set(result["routing"]) if segment_identity(baseline,u) != segment_identity(result,u)}
    removed = set(baseline["routing"]) - set(result["routing"])
    if (changed | removed) - selected:
        raise ValueError("PNS changed locked or unauthorized original routing")
    added = set(result["routing"]) - set(baseline["routing"])
    moved_parents={partial_children[u] for u in changed|removed if u in partial_children}
    import_ids=added|changed
    for parent in moved_parents:
        import_ids.update(c['uuid'] for c in parents[parent]['children'] if c['uuid'] in result['routing'])
    roots = parse_sexpr(Path(board).read_text()); root = roots[0]
    netcodes = {semantic_string(n[2]):str(n[1]) for n in root[1:] if name(n)=="net" and len(n)>2}
    replacements = []
    for uid in sorted(import_ids):
        if name(result['routing'][uid]) != 'segment':
            raise ValueError('PNS must not create or modify vias in a single-layer shove task')
        if uid in original["stored_uuids"] and uid not in changed:
            raise ValueError("PNS minted an existing original identity")
        node = copy.deepcopy(result["routing"][uid]); net = result["identities"][uid]["net"]
        if uid in partial_children:
            # Preserve original metadata; solver-only locked tails must not
            # silently become newly locked user objects in the composed board.
            authored=copy.deepcopy(original['routing'][partial_children[uid]])
            for key in ('uuid','start','end','width','layer'):
                old=field(authored,key);new=field(node,key)
                if old is None or new is None:raise ValueError('Incomplete partial segment serialization')
                old[:]=copy.deepcopy(new)
            node=authored
        n = field(node,"net")
        if netcodes:
            if net not in netcodes:
                raise ValueError("PNS proposed an unknown original net")
            n[:] = ["net",netcodes[net]]
        else:
            n[:] = ["net",Quoted(net.replace('\\','\\\\').replace('"','\\"'))]
        replacements.append(node)
    root[:] = [root[0]] + [n for n in root[1:] if not (name(n)=="segment" and _routing_uuid(n) in changed|removed|moved_parents)] + replacements
    candidate,_ = copy_project(Path(board),Path(folder))
    candidate.write_text(_serialize(root)+'\n')
    composed = read_board(candidate)
    if composed["static"] != original["static"]:
        raise ValueError("PNS delta changed original nonrouting definitions")
    return candidate
