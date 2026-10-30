"""Explain possible direct-path obstacles; this is not a route feasibility test."""

def direct_path_obstacles(model):
    import shapely
    from shapely.geometry import Point,LineString,box
    from shapely import affinity
    from .geometry_search import native_polygon
    ends=model.get('endpoints',[])
    if len(ends)!=2:return {'items':[],'available':False}
    points=[(p.get('native_pad') or {}).get('shape_center',p['center']) for p in ends]
    line=LineString(points)
    half=max(model['minimum_track_width_mm'],*(p['track_width_mm'] for p in ends))/2
    clearance=max(model['minimum_clearance_mm'],*(p['clearance_mm'] for p in ends))
    selected=set(model['task'].get('modification',{}).get('routing_uuids',[]));items=[]
    for p in model['primitives']:
        if p['net']==model['task']['target_net'] and not p.get('blocks_same_net'):continue
        if 'polygons' in p:shape=shapely.union_all([native_polygon(x) for x in p['polygons']])
        elif p['kind']=='segment':shape=LineString([p['start'],p['end']]).buffer(p['width_mm']/2)
        elif p['kind']=='circle':shape=Point(p['center']).buffer(p['size'][0]/2)
        elif p['kind']=='hole':shape=Point(p['center']).buffer(p['diameter_mm']/2)
        elif p['kind']=='rect':
            x,y=p['center'];sx,sy=[v/2 for v in p['size']]
            shape=affinity.rotate(box(x-sx,y-sy,x+sx,y+sy),p['angle_degrees'],origin=(x,y))
        else:continue
        distance=half+(0 if p['kind']=='keepout' else p.get('hole_clearance_mm',max(clearance,p['clearance_mm'])))
        if not shape.is_empty and shape.distance(line)<=distance:
            items.append(dict(uuid=p['uuid'],net=p['net'],kind=p['kind'],layer=model['task']['allowed_layer'],
                bounds_mm=list(shape.bounds),distance_from_direct_centerline_mm=shape.distance(line),required_distance_mm=distance,
                selected_for_modification=p['uuid'] in selected))
    items.sort(key=lambda x:(x['bounds_mm'],x['uuid'],x['kind']))
    return dict(available=True,items=items[:32],total=len(items),truncated=len(items)>32,
        interpretation='Native-model objects intersect the width/clearance corridor of the direct endpoint chord; not proof that every route is blocked')
