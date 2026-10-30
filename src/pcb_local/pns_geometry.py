"""Exact fixed-fill projection for PNS, isolated in the pinned geometry runtime.

A custom pad per simple tile avoids PNS's first-outline-only complex pad fallback.
No synthetic pad or footprint is ever imported back into the user's board.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_local.geometry_search import native_polygon
from pcb_local.pns_adapter import geometry_identity
from pcb_local.local_relevance import fixed_copper_relevance
from pcb_local.local_copper import local_covers, projection_cost

MAX_FILL_VERTICES = 12000
MAX_TILES = 4096


def region_barriers(model):
    """Make the already-authorized ROI a solver constraint, never enlarge it."""
    from decimal import Decimal,ROUND_CEILING,ROUND_FLOOR
    def snap(value,upper):
        return float((Decimal(str(value))*1000000).to_integral_value(rounding=ROUND_CEILING if upper else ROUND_FLOOR)/1000000)
    a,b,c,d=model['task']['region_mm']
    x0,y0,x1,y1=snap(a,True),snap(b,True),snap(c,False),snap(d,False)
    if x0>=x1 or y0>=y1:raise ValueError('Authorized ROI has no native-integer interior')
    pts=[p for edge in model['outline_edges'] for p in edge]
    lo_x=min(x0,*(p[0] for p in pts))-1; hi_x=max(x1,*(p[0] for p in pts))+1
    lo_y=min(y0,*(p[1] for p in pts))-1; hi_y=max(y1,*(p[1] for p in pts))+1
    lo_x,lo_y,hi_x,hi_y=snap(lo_x,False),snap(lo_y,False),snap(hi_x,True),snap(hi_y,True)
    rects=[(lo_x,lo_y,x0,hi_y),(x1,lo_y,hi_x,hi_y),(x0,lo_y,x1,y0),(x0,y1,x1,hi_y)]
    return [dict(uuid=str(uuid.uuid5(uuid.NAMESPACE_URL,'pcb-local-roi-barrier:'+model['task'].get('allowed_layer','F.Cu')+':'+str(i))),
                 polygons=[dict(shell=[[a,b],[c,b],[c,d],[a,d]],holes=[])]) for i,(a,b,c,d) in enumerate(rects)]


def copper(polygons):
    import shapely
    return shapely.union_all([native_polygon(p) for p in polygons])


def anchored_tiles(triangles):
    """Merge an unanchorable sliver with a neighbour without deleting copper."""
    from shapely.geometry import Point
    def center(shape):
        for probe in (shape.centroid,shape.representative_point()):
            xy=[round(v,6) for v in probe.coords[0]]
            if shape.contains(Point(xy)) and shape.boundary.distance(Point(xy)) > .000002:
                return xy
        return None
    pieces=list(triangles)
    # Each merge removes one tile; the input cap bounds work and termination.
    while True:
        bad=next((i for i,p in enumerate(pieces) if center(p) is None),None)
        if bad is None:return [(p,center(p)) for p in pieces]
        shape=pieces[bad];choices=[]
        for i,other in enumerate(pieces):
            if i==bad:continue
            shared=shape.boundary.intersection(other.boundary).length
            if shared<=0:continue
            merged=shape.union(other)
            if merged.geom_type=='Polygon' and not merged.interiors and merged.is_valid:
                choices.append((-shared,i,merged))
        if not choices:raise ValueError('Native fill has a sliver without an exact hole-free neighbouring tile')
        _,other,merged=min(choices,key=lambda x:(x[0],x[1]))
        pieces=[p for i,p in enumerate(pieces) if i not in (bad,other)]+[merged]


def projected_copper(p):
    return p.get('source_kind') in ('zone_fill','fixed_graphic') or bool(p.get('native_arc') or p.get('custom_pad_projection'))


def _prepare_layer(model, force_cover=False):
    import shapely
    zones = [p for p in model['primitives'] if projected_copper(p)]
    relevance=fixed_copper_relevance(model)
    omitted=set(relevance['omitted_uuids'])
    source_count=sum(len(r) for p in zones for poly in p['polygons'] for r in [poly['shell'],*poly['holes']])
    local=local_covers(model,relevance,MAX_FILL_VERTICES,force=force_cover)
    local_by_id={r['uuid']:r for r in local['objects']}
    count=sum(len(r) for p in zones if p['uuid'] not in omitted for poly in local_by_id.get(p['uuid'],p)['polygons'] for r in [poly['shell'],*poly['holes']])
    if count > MAX_FILL_VERTICES:
        raise ValueError('PNS fixed-fill vertex limit exceeded')
    tiles = []; anchors = []
    for zone in zones:
        if zone['uuid'] in omitted:continue
        local_row=local_by_id.get(zone['uuid'])
        shape = copper(local_row['polygons'] if local_row else zone['polygons'])
        if zone.get('custom_pad_projection'):
            from shapely.geometry import Point
            probe=Point(zone['center'])
            if not shape.contains(probe) or shape.boundary.distance(probe)<=.000002:
                raise ValueError('Custom pad anchor lacks a witnessed interior connection point')
            anchors.append({k:zone[k] for k in ('uuid','net','center','clearance_mm','track_width_mm')})
        # Prove each native outline independently: unioning overlapping strokes
        # first introduces fractional-nanometre intersections. Equality of every
        # part proves equality of their union without rounding those crossings.
        pieces = [native_polygon(p) for p in zone['polygons']] if zone.get('source_kind')=='fixed_graphic' and not local_row else [shape]
        tile_index=0
        for outline_index,piece in enumerate(pieces):
            triangles=list(shapely.constrained_delaunay_triangles(piece).geoms)
            if len(tiles)+len(triangles)>MAX_TILES:
                raise ValueError('PNS fixed-fill tile limit exceeded')
            if not shapely.union_all(triangles).equals(piece):
                raise ValueError('Constrained triangulation did not preserve exact filled copper topology')
            triangles.sort(key=lambda t:tuple(sorted(t.exterior.coords)))
            for triangle,center in anchored_tiles(triangles):
                points=[[round(v,6) for v in xy] for xy in list(triangle.exterior.coords)[:-1]]
                from shapely.geometry import Polygon
                if not Polygon(points).equals(triangle):
                    raise ValueError('Filled copper exceeds native integer coordinate precision')
                tile=dict(uuid=str(uuid.uuid5(uuid.NAMESPACE_URL,'pcb-local-fill:'+model['task'].get('allowed_layer','F.Cu')+':'+zone['uuid']+':'+str(tile_index))),
                    source_uuid=zone['uuid'],net=zone['net'],clearance_mm=zone['clearance_mm'],
                    track_width_mm=zone['track_width_mm'],center=center,points=points)
                if zone.get('source_kind')=='fixed_graphic':tile['source_outline_index']=outline_index
                tiles.append(tile);tile_index+=1
    return {'schema_version': 1, 'tiles': tiles, 'source_zone_uuids': [z['uuid'] for z in zones if z.get('source_kind') == 'zone_fill'],
            'source_graphic_uuids': [z['uuid'] for z in zones if z.get('source_kind') == 'fixed_graphic'],
            'source_arc_uuids': [z['uuid'] for z in zones if z.get('native_arc')],
            'source_custom_pad_uuids': [z['uuid'] for z in zones if z.get('custom_pad_projection')], 'anchors': anchors,
            'region_barriers':region_barriers(model),
            'source_vertices': source_count, 'projected_source_vertices': count, 'local_relevance': relevance, 'local_copper':local, 'policy': 'Exact tiles or explicitly witnessed conservative local covers; fixed copper, original zones never replaced'}


def _verify_layer(source, projected, projection, force_cover=False, partial_plans=None):
    """Compare native KiCad rereads, including holes and per-zone copper/rules.

Tile geometry must be exactly equal at KiCad's integer precision; no area or
buffer tolerance may excuse a missing sliver, filled hole or changed network.
"""
    relevance=fixed_copper_relevance(source)
    if projection.get('local_relevance')!=relevance:
        raise ValueError('Fixed copper local relevance proof changed')
    omitted=set(relevance['omitted_uuids'])
    local=local_covers(source,relevance,MAX_FILL_VERTICES,force=force_cover)
    if projection.get('local_copper')!=local:raise ValueError('Local obstacle coverage proof changed')
    local_by_id={r['uuid']:r for r in local['objects']}
    tiles = {p['uuid']: p for p in projection['tiles']}
    if len(tiles)!=len(projection['tiles']):raise ValueError('Duplicate fixed copper tile identity')
    if any(t['source_uuid'] in omitted for t in tiles.values()):
        raise ValueError('Unexpected tile for omitted fixed copper')
    zones = {p['uuid']: p for p in source['primitives'] if projected_copper(p)}
    if any(t['source_uuid'] not in zones for t in tiles.values()):raise ValueError('Unknown fixed copper tile source')
    barriers={p['uuid']:p for p in region_barriers(source)}
    if projection['region_barriers'] != list(barriers.values()):
        raise ValueError('PNS task boundary projection changed')
    found=[p for p in projected['primitives'] if p['uuid'] in barriers]
    if len(found)!=len(barriers) or any(p['kind']!='keepout' or not p.get('blocks_same_net') or
            not copper(p['polygons']).equals(copper(barriers[p['uuid']]['polygons'])) for p in found):
        raise ValueError('Native PNS task boundary differs from authorized ROI')
    if set(zones) != set(projection['source_zone_uuids']) | set(projection.get('source_arc_uuids', [])) | set(projection.get('source_custom_pad_uuids', [])) | set(projection.get('source_graphic_uuids', [])):
        raise ValueError('Fixed-fill source identities mismatch')
    from pcb_local.partial_routing import expand
    expanded=expand(source,partial_plans or [])
    source_rest = dict(expanded, primitives=[p for p in expanded['primitives'] if p['uuid'] not in zones])
    projected_rest = dict(projected, primitives=[p for p in projected['primitives'] if p['uuid'] not in tiles and p['uuid'] not in barriers and p['uuid'] not in {a['uuid'] for a in projection.get('anchors', [])}])
    # The adapter deliberately locks EVERY existing via in the disposable PNS
    # problem. Expect that one strengthening, without dropping lock metadata
    # from identity comparison or permitting any geometry/span change. The
    # composed candidate retains the original object's actual lock state.
    if any(type(v.get('locked')) is not bool for v in expanded.get('vias', [])):
        raise ValueError('Missing original fixed-via lock witness')
    if any(v.get('locked') is not True for v in projected.get('vias', [])):
        raise ValueError('Native PNS fixed via is not locked')
    source_rest['vias'] = [dict(v, locked=True) for v in expanded.get('vias', [])]
    if geometry_identity(source_rest) != geometry_identity(projected_rest):
        raise ValueError('Native PNS problem differs from original non-zone geometry/rules')
    for anchor in projection.get('anchors', []):
        found=[p for p in projected['primitives'] if p['uuid']==anchor['uuid']]
        if len(found)!=1 or found[0]['kind']!='circle' or found[0]['size']!=[.000002,.000002] or any(found[0][k]!=anchor[k] for k in ('net','center','clearance_mm','track_width_mm')):
            raise ValueError('Custom pad connection anchor changed')
        from shapely.geometry import Point
        shape=copper(zones[anchor['uuid']]['polygons']);probe=Point(anchor['center'])
        if not shape.contains(probe) or shape.boundary.distance(probe)<=.000002:
            raise ValueError('Synthetic anchor would add copper outside the original custom pad')
    actual = {p['uuid']:p for p in projected['primitives'] if p['uuid'] in tiles}
    if set(actual) != set(tiles):
        raise ValueError('Native PNS problem lost fixed-fill tiles')
    report = []
    for uid, zone in zones.items():
        if uid in omitted:continue
        parts = [];by_outline={}
        local_row=local_by_id.get(uid)
        for tile in (t for t in tiles.values() if t['source_uuid'] == uid):
            p = actual[tile['uuid']]
            if any(p[k] != zone[k] for k in ('net','clearance_mm','track_width_mm')) or p['kind'] != 'polygon':
                raise ValueError('Native fixed-fill tile net/rules/kind changed')
            if 'authored_polygons' not in p:
                raise ValueError('Missing native authored custom-pad witness')
            exact = copper(p['authored_polygons'])
            if not copper(p['polygons']).covers(exact):
                raise ValueError('Native conservative custom-pad envelope lost copper')
            parts.extend(p['authored_polygons'])
            if zone.get('source_kind')=='fixed_graphic' and not local_row:
                index=tile.get('source_outline_index')
                if type(index) is not int or not 0<=index<len(zone['polygons']):
                    raise ValueError('Fixed graphic source outline identity changed')
                by_outline.setdefault(index,[]).extend(p['authored_polygons'])
        old, new = copper(zone['polygons']), copper(parts)
        if local_row:
            from shapely.geometry import box
            required=old.intersection(box(*relevance['guarded_region_mm']))
            expected=copper(local_row['polygons'])
            if not new.equals(expected) or not new.covers(required):
                raise ValueError('Native local obstacle cover lost copper or changed shape: '+uid)
            report.append(dict(source_uuid=uid,source_kind=zone['source_kind'],exact_equal=False,
                proof='conservative_guard_coverage',required_copper_covered=True,
                required_area_mm2=required.area,projected_area_mm2=new.area,
                extra_obstacle_area_mm2=new.difference(required).area))
            continue
        exact_equal=(set(by_outline)==set(range(len(zone['polygons']))) and all(
            native_polygon(poly).equals(copper(by_outline[i])) for i,poly in enumerate(zone['polygons']))) if zone.get('source_kind')=='fixed_graphic' else old.equals(new)
        if not exact_equal:
            raise ValueError('Native fixed-fill geometry changed copper topology or boundary: '+uid)
        report.append(dict(source_uuid=uid, source_kind='fixed_arc_envelope' if zone.get('native_arc') else 'fixed_custom_pad_envelope' if zone.get('custom_pad_projection') else 'fixed_graphic_envelope' if zone.get('source_kind')=='fixed_graphic' else 'zone_fill', area_mm2=old.area, exact_equal=True, proof='per_native_outline' if zone.get('source_kind')=='fixed_graphic' else 'whole_union',
            components=len(old.geoms) if hasattr(old,'geoms') else (0 if old.is_empty else 1),
            holes=sum(len(p.interiors) for p in (old.geoms if hasattr(old,'geoms') else [old]) if hasattr(p,'interiors'))))
    return {'status':'verified', 'zones':report, 'tile_count':len(tiles), 'nonzone_geometry_equal':True, 'local_relevance':relevance, 'local_copper':local, 'omitted_fixed_copper':[r for r in relevance['objects'] if r['omitted']]}


def complexity_plan(model):
    costs=[projection_cost(m, fixed_copper_relevance(m)) for m in [model,*model.get('auxiliary_layers',{}).values()]]
    vertices=sum(c['vertices'] for c in costs)
    tiles=sum(c['triangle_upper_bound'] for c in costs)
    return dict(source_vertices=vertices, triangle_upper_bound=tiles,
                force_local_cover=vertices>MAX_FILL_VERTICES or tiles>MAX_TILES)


def prepare(model):
    from pcb_local.zone_refill import search_model
    from pcb_local.remote_vias import analyze, filtered
    remote = analyze(model)
    model = filtered(model, remote)
    model, refill = search_model(model)
    plan=complexity_plan(model)
    result=_prepare_layer(model,plan['force_local_cover'])
    result['complexity_plan']=plan
    if remote is not None:result['remote_vias']=remote
    if refill is not None:result['zone_refill']=refill
    from pcb_local.geometry_diagnostics import direct_path_obstacles
    result['direct_path_obstacles']=direct_path_obstacles(model)
    result['auxiliary_layers']={layer:_prepare_layer(m,plan['force_local_cover']) for layer,m in model.get('auxiliary_layers',{}).items()}
    all_projections=[result,*result['auxiliary_layers'].values()]
    if sum(p['projected_source_vertices'] for p in all_projections)>MAX_FILL_VERTICES or sum(len(p['tiles']) for p in all_projections)>MAX_TILES:
        raise ValueError('PNS whole-stack fixed copper complexity limit exceeded')
    return result


def verify(source,projected,projection):
    from pcb_local.zone_refill import search_model
    from pcb_local.partial_routing import validated_plans
    from pcb_local.remote_vias import analyze, filtered
    remote=analyze(source)
    if projection.get('remote_vias')!=remote:
        raise ValueError('Remote-via proof differs from independent original geometry analysis')
    source=filtered(source,remote)
    subdivisions=validated_plans(source)
    source, refill = search_model(source)
    if projection.get('zone_refill') != refill:raise ValueError('Zone refill search permission proof changed')
    plan=complexity_plan(source)
    if projection.get('complexity_plan')!=plan:
        raise ValueError('Whole-stack copper complexity plan changed')
    expected=set(source.get('auxiliary_layers',{}))
    if set(projected.get('auxiliary_layers',{}))!=expected or set(projection.get('auxiliary_layers',{}))!=expected:
        raise ValueError('Native PNS copper-layer set changed or a layer was omitted')
    result=_verify_layer(source,projected,projection,plan['force_local_cover'],subdivisions)
    result['auxiliary_layers']={layer:_verify_layer(source['auxiliary_layers'][layer],projected['auxiliary_layers'][layer],projection['auxiliary_layers'][layer],plan['force_local_cover']) for layer in sorted(expected)}
    result['copper_layers']=source.get('copper_layers')
    if remote is not None:result['remote_vias']=remote
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--projection',type=Path,required=True);p.add_argument('--reread',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    try:
        source=json.loads(a.source.read_text())['model']
        result=verify(source,json.loads(a.reread.read_text())['model'],json.loads(a.projection.read_text())) if a.reread else prepare(source)
        a.output.write_text(json.dumps(result,indent=2)+'\n');return 0
    except Exception as exc:
        a.output.write_text(json.dumps({'status':'unsupported','reason':str(exc)},indent=2)+'\n');return 2

if __name__=='__main__':raise SystemExit(main())
