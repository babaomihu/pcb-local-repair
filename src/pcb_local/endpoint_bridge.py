"""Witnessed endpoint access to an overlay-clipped search component.

Physical endpoints are never projected or moved. No epsilon or geometry repair
is used: both source sets must independently cover every complete access segment.
"""
import math

MAX_ANCHOR_CANDIDATES = 8


def anchored_path(local, full_free, window, start, end, *, check, max_vertices):
    from shapely.geometry import Point, LineString
    from pcb_local.mesh_corridor import corridor_path
    original=(tuple(start),tuple(end))
    check()
    if not all(full_free.covers(Point(p)) and window.covers(Point(p)) for p in original):
        return None
    parts=list(local.geoms) if local.geom_type=='MultiPolygon' else [local]
    candidates=[]
    for part in parts:
        check()
        if part.geom_type!='Polygon':continue
        seeds=[];bridges=[];attempts=[]
        vertices=None
        for endpoint in original:
            if part.covers(Point(endpoint)):
                seeds.append(endpoint);bridges.append(0.);attempts.append(0);continue
            if vertices is None:
                vertices={tuple(q) for ring in [part.exterior,*part.interiors] for q in ring.coords[:-1]}
            ordered=sorted(vertices,key=lambda q:(math.dist(endpoint,q),q))[:MAX_ANCHOR_CANDIDATES]
            chosen=None
            for index,seed in enumerate(ordered,1):
                check();segment=LineString([endpoint,seed])
                if part.covers(Point(seed)) and full_free.covers(segment) and window.covers(segment):
                    chosen=seed;attempts.append(index);break
            if chosen is None:break
            seeds.append(chosen);bridges.append(math.dist(endpoint,chosen))
        if len(seeds)==2 and any(bridges):
            candidates.append((sum(bridges),tuple(seeds),part,bridges,attempts))
    if not candidates:return None
    _,seeds,part,bridges,attempts=min(candidates,key=lambda x:(x[0],x[1]))
    result=corridor_path(part,*seeds,max_vertices=max_vertices,check=check)
    raw=[original[0],*map(tuple,result['path_mm']),original[1]]
    path=[p for i,p in enumerate(raw) if i==0 or p!=raw[i-1]]
    check()
    if not (full_free.covers(LineString(path)) and window.covers(LineString(path))):
        return None
    result.update(path_mm=path,length_mm=sum(math.dist(a,b) for a,b in zip(path,path[1:])),
        endpoint_access=dict(policy='strict-source-set-covered-segments',physical_endpoints=original,
            search_seeds=seeds,bridge_lengths_mm=bridges,candidates_tested=attempts,
            max_candidates_per_endpoint=MAX_ANCHOR_CANDIDATES,physical_endpoints_moved=False,
            tolerance_added_mm=0,entire_path_covered_by_full_free_and_window=True))
    return result
