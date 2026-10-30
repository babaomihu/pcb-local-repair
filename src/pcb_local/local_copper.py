"""Conservative local obstacle covers for large fixed, unrelated copper.

Only solver geometry is reduced. Source copper is retained for full-board
acceptance. The proof is coverage inside the rule-derived guard, never global
copper equality or connectivity equivalence.
"""
import hashlib
import json

GRID = .000001
SIMPLIFY = .0001


def projection_cost(model, relevance):
    """Conservative pre-triangulation count, including holes and islands.

    A polygon with V vertices and H holes needs V+2H-2 triangles. Counting
    collinear vertices can overestimate, which only triggers a safe cover.
    Match the projection's union/per-outline handling before counting.
    """
    import shapely
    from .geometry_search import native_polygon
    omitted = set(relevance['omitted_uuids'])
    vertices = tiles = 0
    for p in model['primitives']:
        if p['uuid'] in omitted or not (p.get('source_kind') in ('zone_fill','fixed_graphic') or p.get('native_arc') or p.get('custom_pad_projection')):
            continue
        polygons = [native_polygon(poly) for poly in p['polygons']]
        vertices += sum(len(r) for poly in p['polygons'] for r in [poly['shell'], *poly['holes']])
        shapes = polygons if p.get('source_kind') == 'fixed_graphic' else [shapely.union_all(polygons)]
        for shape in shapes:
            for part in (shape.geoms if hasattr(shape, 'geoms') else [shape]):
                if part.is_empty:
                    continue
                v = len(part.exterior.coords)-1 + sum(len(h.coords)-1 for h in part.interiors)
                tiles += max(0, v + 2*len(part.interiors) - 2)
    return {'vertices': vertices, 'triangle_upper_bound': tiles}


def local_covers(model, relevance, vertex_limit, force=False):
    import shapely
    from shapely.geometry import box, Polygon
    from .geometry_search import native_polygon
    rows={r['uuid']:r for r in relevance['objects']}
    omitted=set(relevance['omitted_uuids'])
    eligible=[p for p in model['primitives'] if p.get('source_kind') in ('zone_fill','fixed_graphic') and p['uuid'] not in omitted]
    # Preserve the exact whole-object path on ordinary small problems.
    count=sum(len(r) for p in model['primitives'] if p['uuid'] not in omitted and
              (p.get('source_kind') in ('zone_fill','fixed_graphic') or p.get('native_arc') or p.get('custom_pad_projection'))
              for poly in p.get('polygons',[]) for r in [poly['shell'],*poly['holes']])
    if count<=vertex_limit and not force:return {'schema_version':1,'objects':[]}
    guard=box(*relevance['guarded_region_mm']);output=[]
    for p in eligible:
        row=rows[p['uuid']]
        if row['reason']=='target_or_authorized_context':continue
        source=shapely.union_all([native_polygon(poly) for poly in p['polygons']])
        # No projection benefit for an object wholly inside the influence guard.
        if guard.covers(source):continue
        required=source.intersection(guard)
        if required.is_empty:continue  # whole-object omission remains a separate proof
        if required.geom_type not in ('Polygon','MultiPolygon'):raise ValueError('Local copper intersection is not a polygon')
        # Simplification cannot authorize missing copper: independently test full
        # original guard intersection after outward growth and integer snapping.
        reduced=required.simplify(SIMPLIFY,preserve_topology=True)
        cover=shapely.set_precision(reduced.buffer(SIMPLIFY+2*GRID,join_style='mitre'),GRID)
        if not cover.is_valid or cover.is_empty or not cover.covers(required):
            raise ValueError('Integer local obstacle cover lost required source copper')
        if cover.geom_type not in ('Polygon','MultiPolygon'):raise ValueError('Invalid local obstacle cover type')
        pieces=list(cover.geoms) if hasattr(cover,'geoms') else [cover]
        polys=[]
        for part in sorted(pieces,key=lambda g:(g.bounds,g.area)):
            points=lambda ring:[[round(x,6),round(y,6)] for x,y in list(ring.coords)[:-1]]
            polys.append({'shell':points(part.exterior),'holes':[points(h) for h in part.interiors]})
        exact=shapely.union_all([native_polygon(poly) for poly in polys])
        if not exact.equals(cover) or not exact.covers(required):raise ValueError('Local cover serialization changed copper coverage')
        output.append({'uuid':p['uuid'],'net':p['net'],'source_sha256':row['source_sha256'],
                       'guarded_region_mm':relevance['guarded_region_mm'],'polygons':polys,
                       'required_area_mm2':required.area,'cover_area_mm2':exact.area,
                       'extra_obstacle_area_mm2':exact.difference(required).area,
                       'proof':'conservative_guard_coverage','exact_global_copper_equal':False,
                       'policy':'Fixed unrelated solver obstacles only; target/authorized context and full source-board acceptance retained'})
    return {'schema_version':1,'grid_mm':GRID,'simplification_mm':SIMPLIFY,'objects':output}
