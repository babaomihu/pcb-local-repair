"""Bounded search on a contracted triangle dual inside unchanged free space.

Original implementation, informed by navigation-mesh routing. Not Polyanya:
centroid/portal paths are feasible proposals, with no shortest-path guarantee.
"""
import heapq
import math
from collections import defaultdict


class MeshLimit(RuntimeError):
    def __init__(self, message, kind, **details):
        super().__init__(message)
        self.kind, self.details = kind, details


def corridor_path(free, start, end, *, max_vertices, check, max_cells=10000):
    import shapely
    from shapely.geometry import Point, LineString

    check()
    if not hasattr(shapely, 'constrained_delaunay_triangles'):
        raise MeshLimit('Constrained triangulation unavailable', 'mesh_unavailable')
    # Euler count for a polygon without Steiner points. Bound work BEFORE
    # entering the non-interruptible GEOS triangulation, then also check output.
    expected_cells=sum(len(r.coords)-1 for r in [free.exterior,*free.interiors])+2*len(free.interiors)-2
    if expected_cells>max_cells:
        raise MeshLimit('Triangle preprocessing exceeds bounded scope', 'mesh_cell_limit', observed=expected_cells, maximum=max_cells)
    cells=list(shapely.constrained_delaunay_triangles(free).geoms)
    check()
    if not cells or len(cells)>max_cells:
        raise MeshLimit('Triangle preprocessing exceeds bounded scope', 'mesh_cell_limit', observed=len(cells), maximum=max_cells)
    # Exact set comparison; no snapping, buffer repair, tolerance or obstacle removal.
    if not shapely.union_all(cells).equals(free):
        raise ValueError('Triangulation does not exactly cover the original local free space')
    check()
    edges=defaultdict(list); centers=[]; owners=[[],[]]
    for i,cell in enumerate(cells):
        check()
        ring=list(cell.exterior.coords)[:-1]
        if len(ring)!=3 or cell.area<=0 or not free.covers(cell):
            raise ValueError('Invalid or out-of-scope mesh cell')
        centers.append(tuple(cell.centroid.coords[0]))
        for a,b in zip(ring,ring[1:]+ring[:1]):edges[tuple(sorted((a,b)))].append(i)
        for j,p in enumerate((start,end)):
            if cell.covers(Point(p)):owners[j].append(i)
    if not all(owners):
        raise ValueError('Mesh has no cell containing an endpoint')
    source,target=[min(ids) for ids in owners]
    adjacent=[{} for _ in cells]
    for (a,b),ids in edges.items():
        check()
        if len(ids)>2:raise ValueError('Nonmanifold triangle adjacency')
        if len(ids)==2:
            x,y=ids;portal=((a[0]+b[0])/2,(a[1]+b[1])/2)
            adjacent[x][y]=portal;adjacent[y][x]=portal
    # Degree-two cells are geometry on an edge, not additional search states.
    anchors={i for i,ns in enumerate(adjacent) if len(ns)!=2}|{source,target}
    if len(anchors)>max_vertices:
        raise MeshLimit('Contracted mesh vertex count exceeds search cap', 'vertex_limit', observed=len(anchors), maximum=max_vertices, mesh_cells=len(cells))
    graph={i:[] for i in anchors}; visited=set()
    for a in sorted(anchors):
        for first in sorted(adjacent[a]):
            check()
            if (a,first) in visited:continue
            path=[centers[a]];last,current=a,first
            while True:
                check()
                if (last,current) in visited:raise ValueError('Unexpected repeated mesh edge')
                visited.add((last,current));visited.add((current,last))
                path.extend([adjacent[last][current],centers[current]])
                if current in anchors:break
                choices=[n for n in adjacent[current] if n!=last]
                if len(choices)!=1:raise ValueError('Invalid degree-two mesh chain')
                last,current=current,choices[0]
            cost=sum(math.dist(p,q) for p,q in zip(path,path[1:]))
            graph[a].append((current,cost,path))
            graph[current].append((a,cost,list(reversed(path))))
    check()
    queue=[(0.,source)];distance={source:0.};parents={};closed=set()
    while queue:
        check();cost,node=heapq.heappop(queue)
        if node in closed:continue
        closed.add(node)
        if node==target:break
        for other,length,path in graph[node]:
            tentative=cost+length
            if tentative<distance.get(other,math.inf):
                distance[other]=tentative;parents[other]=(node,path)
                heapq.heappush(queue,(tentative,other))
    if target not in closed:
        raise MeshLimit('Contracted local mesh did not connect endpoints', 'mesh_disconnected')
    chains=[];node=target
    while node!=source:
        check();previous,path=parents[node];chains.append(path);node=previous
    raw=[tuple(start),centers[source]]
    for path in reversed(chains):raw.extend(path[1:])
    raw.append(tuple(end))
    raw=[p for i,p in enumerate(raw) if i==0 or p!=raw[i-1]]
    if not free.covers(LineString(raw)):
        raise ValueError('Portal path is outside unchanged local free space')
    # Greedy shortcuts use exact coverage queries against the same polygon.
    path=[raw[0]];at=0
    while at<len(raw)-1:
        check();nxt=len(raw)-1
        while nxt>at+1:
            check()
            if free.covers(LineString([raw[at],raw[nxt]])):break
            nxt-=1
        path.append(raw[nxt]);at=nxt
    if not free.covers(LineString(path)):
        raise ValueError('Shortcut path is outside unchanged local free space')
    return dict(path_mm=path,vertices=len(anchors),expanded=len(closed),
                mesh_cells=len(cells),mesh_cell_limit=max_cells,
                mesh_vertices_cap=max_vertices,raw_waypoints=len(raw),
                method='conservative-configuration-space-contracted-mesh',
                length_mm=sum(math.dist(p,q) for p,q in zip(path,path[1:])),
                global_shortest_claimed=False)
