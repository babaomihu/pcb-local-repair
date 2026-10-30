"""Bounded configuration-space visibility search, independent of a fixed grid.

Uses Shapely from the pinned router environment. Expanded curved obstacles
are circumscribed, not inscribed. The resulting straight-track candidate is
only a proposal: the unmodified full native acceptance gate remains required.
"""
import argparse
import heapq
import json
import math
from pathlib import Path
import sys
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcb_local.workspace import copy_project, source_files
from pcb_local.validation import parse_sexpr, name, semantic_string, Quoted

ARC_QUADRANTS = 32
SAFETY_MM = 0.00001  # 10 nm; additional conservatism, not rule relaxation.
MAX_VERTICES = 1200
MAX_PRIMITIVES = 10000


class SearchLimit(RuntimeError):
    """Typed bounded-search termination; size limits are not elapsed time."""
    def __init__(self, message, kind="resource_limit", **details):
        super().__init__(message)
        self.kind, self.details = kind, details

    def result(self):
        return {"status": "budget_exhausted" if self.kind == "deadline" else "bounded_out",
                "reason": str(self), "limit_kind": self.kind, "limit_details": self.details}



def native_polygon(value):
    """Recover only exactly doubled KiCad fracture bridges, without repair.

All remaining edges must form valid closed rings. Area must match the raw
algebraic polygon; arbitrary crossings/overlaps are not silently simplified.
"""
    from collections import Counter
    import shapely
    from shapely.geometry import Polygon, MultiLineString, LineString
    raw = Polygon(value["shell"], value["holes"])
    if raw.is_valid and not raw.is_empty:
        return raw
    edges = Counter()
    for ring in [value["shell"], *value["holes"]]:
        points = list(map(tuple, ring))
        for a, b in zip(points, points[1:] + points[:1]):
            if a != b:
                edges[a, b] += 1
    clean = [edge for edge, count in edges.items()
             for _ in range(max(0, count - edges[edge[1], edge[0]]))]
    if not clean or len(clean) == sum(edges.values()):
        raise ValueError("Invalid native polygon without exact fracture bridges")
    _, cuts, dangles, invalid = shapely.polygonize_full([LineString(e) for e in clean])
    if not all(x.is_empty for x in (cuts, dangles, invalid)):
        raise ValueError("Unresolved native polygon edges after removing exact bridges")
    recovered = shapely.build_area(MultiLineString(clean))
    if (not recovered.is_valid or recovered.is_empty or
            not math.isclose(raw.area, recovered.area, rel_tol=1e-13, abs_tol=1e-12)):
        raise ValueError("Exact native fracture recovery did not preserve copper area")
    return recovered


def configuration_space(model, check=lambda: None):
    """Build conservative free space; shared by routing and quality proposals."""
    import shapely
    from shapely import affinity
    from shapely.geometry import Point, MultiLineString, box

    # Numbers from project files are untrusted even after native preflight.
    def number(value, positive=False):
        value = float(value)
        if not math.isfinite(value) or value < 0 or (positive and value <= 0):
            raise ValueError("Invalid geometric size/constraint")
        return value

    endpoints = model["endpoints"]
    width = max(number(p["track_width_mm"], True) for p in endpoints)
    width = max(width, number(model["minimum_track_width_mm"]))
    width = math.ceil(width * 1e6) / 1e6  # Never quantize a width down.
    clearance = max(number(model["minimum_clearance_mm"]),
                    *(number(p["clearance_mm"]) for p in endpoints))
    half = width / 2
    inflation = 1 / math.cos(math.pi / (4 * ARC_QUADRANTS))

    def conservative_buffer(shape, distance):
        return shape.buffer((distance + SAFETY_MM) * inflation, quad_segs=ARC_QUADRANTS)

    outline_error = 0.
    if 'outline_polygons' in model:
        if not model.get('outline_shapes') or not model.get('outline_edge_envelopes'):
            raise ValueError('Missing exact native board-edge witnesses')
        precision=model['outline_precision']
        outline_error=sum(number(precision[k]) for k in ('max_error_mm','chaining_epsilon_mm','edge_envelope_error_mm')) + .000002
        board_area=shapely.union_all([native_polygon(p) for p in model['outline_polygons']])
    else:
        edge_lines = MultiLineString(model["outline_edges"])
        _, cuts, dangles, invalid = shapely.polygonize_full(list(edge_lines.geoms))
        if not cuts.is_empty or not dangles.is_empty or not invalid.is_empty:
            raise ValueError("Board outline is not closed valid straight rings")
        board_area = shapely.build_area(edge_lines)
    if board_area.is_empty or not board_area.is_valid:
        raise ValueError("No valid board area")
    roi = box(*model["task"]["region_mm"])
    edge_distance=number(model['edge_clearance_mm'])+half
    # Native arc chords and epsilon chaining are approximations. Shrinking the
    # complete material region plus blocking actual ERROR_OUTSIDE edge envelopes
    # forbids routes in their uncertainty strip, including around inner holes.
    allowed = board_area.buffer(-(edge_distance+outline_error+SAFETY_MM)*inflation,
                                quad_segs=ARC_QUADRANTS).intersection(roi.buffer(-half-SAFETY_MM))
    if 'outline_polygons' in model:
        envelopes=shapely.union_all([native_polygon(p) for e in model['outline_edge_envelopes'] for p in e['polygons']])
        allowed=allowed.difference(conservative_buffer(envelopes,edge_distance+outline_error))
    obstacles = []
    if len(model["primitives"]) > MAX_PRIMITIVES:
        raise SearchLimit("Geometric primitive count exceeds bounded search scope", "primitive_limit", observed=len(model["primitives"]), maximum=MAX_PRIMITIVES)
    for item in model["primitives"]:
        check()
        if item["net"] == model["task"]["target_net"] and not item.get("blocks_same_net"):
            continue
        distance = half + max(clearance, number(item["clearance_mm"]))
        if item["kind"] in ("polygon", "keepout", "hole"):
            if "polygons" in item:
                shapes = [native_polygon(p) for p in item["polygons"]]
                if any(not p.is_valid or p.is_empty for p in shapes):
                    raise ValueError("Invalid native polygon; no silent geometry repair")
                raw = shapely.union_all(shapes)
            else:
                raw = conservative_buffer(Point(item["center"]), number(item["diameter_mm"], True)/2)
            if item["kind"] == "keepout":
                distance = half
            elif item["kind"] == "hole":
                distance = half + number(item["hole_clearance_mm"])
            shape = conservative_buffer(raw, distance)
        elif item["kind"] == "segment":
            # The hull of two circumscribed endpoint disks contains the exact
            # swept disk. Use the same world-oriented disk polygon for tracks
            # and pads: rotating a capsule's tessellation independently creates
            # hundreds of artificial union vertices at coincident round pads.
            radius = distance + number(item["width_mm"], True) / 2
            shape = shapely.union_all([conservative_buffer(Point(item[key]), radius)
                                       for key in ("start", "end")]).convex_hull
        elif item["kind"] == "circle":
            shape = conservative_buffer(Point(item["center"]), distance + number(item["size"][0], True) / 2)
        elif item["kind"] == "rect":
            x, y = item["center"]
            sx, sy = [number(v, True) / 2 for v in item["size"]]
            rectangle = affinity.rotate(box(x-sx, y-sy, x+sx, y+sy), item["angle_degrees"], origin=(x, y))
            shape = conservative_buffer(rectangle, distance)
        else:
            raise ValueError("Unmodelled copper primitive")
        if shape.intersects(allowed):
            obstacles.append(shape)
    blocked = shapely.union_all(obstacles) if obstacles else shapely.GeometryCollection()
    free = allowed.difference(blocked)
    return free, width, clearance, outline_error


def boundary_corridor(full_free, start, end, radius, check):
    """Try at most two exterior-boundary arcs as search hints, not permissions.

    An obstacle attached to the allowed-region edge is part of the free
    component's exterior, not an interior hole. Walk the two arcs between the
    first and last chord contacts. Clipping each hint to the unchanged complete
    free space retains every obstacle, including holes crossed by a hint.
    """
    from shapely.geometry import Point, LineString
    from shapely.ops import substring

    chord = LineString([start, end])
    ring = LineString(full_free.exterior.coords)
    check()
    contacts = chord.intersection(ring)
    def contact_points(shape):
        if shape.is_empty:
            return []
        if shape.geom_type == 'Point':
            return [shape]
        if shape.geom_type == 'LineString':
            return [Point(shape.coords[0]), Point(shape.coords[-1])]
        return [p for child in getattr(shape, 'geoms', []) for p in contact_points(child)]
    points = sorted(contact_points(contacts), key=chord.project)
    if len(points) < 2 or points[0].equals(points[-1]):
        return None, []
    a, b = [ring.project(p) for p in (points[0], points[-1])]
    low, high = sorted((a, b))
    arcs = [list(substring(ring, low, high).coords),
            list(substring(ring, high, ring.length).coords)
            + list(substring(ring, 0, low).coords)[1:]]
    if a > b:
        arcs[0].reverse()
    else:
        arcs[1].reverse()
    guides = [LineString([start, *arc, end]) for arc in arcs]
    guides.sort(key=lambda g: (g.length, tuple(g.coords)))
    attempts = []
    for guide in guides:
        check()
        local = full_free.intersection(guide.buffer(radius))
        pieces = list(local.geoms) if local.geom_type == 'MultiPolygon' else [local]
        usable = [p for p in pieces if p.geom_type == 'Polygon'
                  and p.covers(Point(start)) and p.covers(Point(end))]
        row = {'guide_length_mm': guide.length, 'connected': len(usable) == 1}
        attempts.append(row)
        if len(usable) != 1:
            continue
        part = usable[0]
        vertices = set(tuple(p) for r in [part.exterior, *part.interiors] for p in r.coords[:-1])
        vertices.discard(start); vertices.discard(end)
        row['vertices'] = len(vertices) + 2
        check()
        if row['vertices'] <= MAX_VERTICES:
            return part, attempts
    return None, attempts


def search(model, *, timeout=30):
    import numpy as np
    import shapely
    from shapely import affinity
    from shapely.geometry import Point, LineString, MultiLineString, Polygon, box

    deadline = time.monotonic() + timeout

    def check():
        if time.monotonic() >= deadline:
            raise SearchLimit("Geometric search exhausted the remaining shared search budget", "deadline")

    free, width, clearance, outline_error = configuration_space(model, check)
    endpoints = model["endpoints"]
    start, end = [tuple(p["center"]) for p in endpoints]
    parts = list(free.geoms) if free.geom_type == "MultiPolygon" else [free]
    connected = [p for p in parts if p.geom_type == "Polygon" and p.covers(Point(start)) and p.covers(Point(end))]
    if not connected:
        return {"status": "no_path", "reason": "Endpoint centers have no common conservatively modelled free-space component",
                "width_mm": width, "clearance_mm": clearance}
    free = connected[0]
    # A segment wholly covered by the SAME conservative free component needs
    # no visibility graph. Its Euclidean length is a global lower bound, so
    # there is no reason to enumerate distant boundary vertices first.
    check()
    if start != end and free.covers(LineString([start, end])):
        check()
        return {"status": "found", "path_mm": [start, end], "width_mm": width,
                "clearance_mm": clearance, "vertices": 2, "expanded": 0,
                "length_mm": math.dist(start, end),
                "method": "conservative-configuration-space-direct",
                "additional_safety_mm": SAFETY_MM, "arc_quadrants": ARC_QUADRANTS,
                "outline_uncertainty_margin_mm": outline_error}
    vertices = set(tuple(p) for ring in [free.exterior, *free.interiors] for p in ring.coords[:-1])
    vertices.discard(start)
    vertices.discard(end)
    nodes = [start, end] + sorted(vertices)
    full_free = free
    graph_scope = {"kind": "full_component", "full_vertices": len(nodes)}
    if len(nodes) > MAX_VERTICES:
        # Restrict search space, never obstacle/rule space. A failed local
        # window is not proof that the complete component has no route.
        check()
        radius = max(width + clearance, math.dist(start, end) / 16)
        initial_radius = radius
        window = LineString([start, end]).buffer(radius, cap_style=2)
        local = full_free.intersection(window)
        pieces = list(local.geoms) if local.geom_type == "MultiPolygon" else [local]
        usable = [p for p in pieces if p.geom_type == "Polygon"
                  and p.covers(Point(start)) and p.covers(Point(end))]
        check()
        if len(usable) != 1:
            from pcb_local.endpoint_bridge import anchored_path
            from pcb_local.mesh_corridor import MeshLimit
            try:
                anchored = anchored_path(local, full_free, window, start, end,
                                         check=check, max_vertices=MAX_VERTICES)
            except MeshLimit as exc:
                # Optional access strategy cannot suppress the older bounded
                # window strategies. Shared deadline exceptions are not caught.
                anchored = None
                graph_scope["endpoint_access_limit"] = dict(kind=exc.kind, details=exc.details)
            if anchored is not None:
                return dict(anchored, status="found", width_mm=width, clearance_mm=clearance,
                            graph_scope=dict(kind="restricted_corridor", full_vertices=len(nodes),
                                             radius_mm=radius, corridor_policy="witnessed_endpoint_access",
                                             global_shortest_claimed=False),
                            additional_safety_mm=SAFETY_MM, arc_quadrants=ARC_QUADRANTS,
                            outline_uncertainty_margin_mm=outline_error)
            # Use actual holes of the SAME complete conservative component.
            # A hole intersecting the chord can require a wider detour than
            # endpoint distance alone suggests. Propose one envelope-derived
            # window, not a sequence of unbounded radius/budget increases.
            chord = LineString([start, end])
            length = math.dist(start, end)
            extent, blockers = 0., 0
            if length:
                dx, dy = end[0]-start[0], end[1]-start[1]
                for ring in full_free.interiors:
                    check()
                    if not Polygon(ring).intersects(chord):
                        continue
                    blockers += 1
                    extent = max(extent, max(abs(dx*(y-start[1])-dy*(x-start[0]))/length
                                             for x,y in ring.coords))
            proposed = extent + width + clearance
            if blockers and proposed > radius:
                graph_scope.update(corridor_policy="blocking_hole_envelope",
                                   initial_radius_mm=radius, blocking_holes=blockers)
                radius = proposed
                window = chord.buffer(radius, cap_style=2)
                local = full_free.intersection(window)
                pieces = list(local.geoms) if local.geom_type == "MultiPolygon" else [local]
                usable = [p for p in pieces if p.geom_type == "Polygon"
                          and p.covers(Point(start)) and p.covers(Point(end))]
                check()
            if len(usable) != 1:
                boundary, attempts = boundary_corridor(full_free, start, end, initial_radius, check)
                if boundary is None:
                    raise SearchLimit("Full graph exceeds vertex limit; bounded local corridor does not connect endpoints", "local_corridor_disconnected", full_vertices=len(nodes), maximum=MAX_VERTICES, radius_mm=radius, boundary_attempts=attempts)
                usable = [boundary]
                graph_scope.update(corridor_policy="exterior_boundary_detour",
                                   boundary_attempts=attempts, initial_radius_mm=initial_radius)
                radius = initial_radius
        free = usable[0]
        vertices = set(tuple(p) for ring in [free.exterior, *free.interiors] for p in ring.coords[:-1])
        vertices.discard(start); vertices.discard(end)
        nodes = [start, end] + sorted(vertices)
        graph_scope.update(kind="restricted_corridor", radius_mm=radius, local_vertices=len(nodes),
                           global_shortest_claimed=False)
        if len(nodes) > MAX_VERTICES:
            from pcb_local.mesh_corridor import corridor_path, MeshLimit
            try:
                mesh = corridor_path(free, start, end, max_vertices=MAX_VERTICES, check=check)
            except MeshLimit as exc:
                raise SearchLimit(str(exc), exc.kind, **exc.details) from exc
            check()
            if not full_free.covers(LineString(mesh["path_mm"])):
                raise ValueError("Mesh proposal is not covered by the complete conservative space")
            return dict(mesh, status="found", width_mm=width, clearance_mm=clearance,
                        graph_scope=graph_scope, additional_safety_mm=SAFETY_MM,
                        arc_quadrants=ARC_QUADRANTS, outline_uncertainty_margin_mm=outline_error)
    coords = np.asarray(nodes, dtype=float)
    if not np.isfinite(coords).all():
        raise ValueError("Nonfinite geometry")
    shapely.prepare(free)
    distances = np.full(len(nodes), np.inf)
    distances[0] = 0
    parent, closed = {}, set()
    queue = [(math.dist(start, end), 0., 0)]
    expanded = 0
    while queue:
        check()
        _, cost, current = heapq.heappop(queue)
        if current in closed:
            continue
        if current == 1:
            path, index = [], 1
            while True:
                path.append(nodes[index])
                if index == 0:
                    break
                index = parent[index]
            check()
            if not full_free.covers(LineString(path)):
                raise ValueError("Local proposal is not covered by the complete conservative space")
            return {"status": "found", "path_mm": list(reversed(path)), "width_mm": width,
                    "clearance_mm": clearance, "vertices": len(nodes), "expanded": expanded,
                    "length_mm": cost, "method": "conservative-configuration-space-visibility-A*",
                    "additional_safety_mm": SAFETY_MM, "arc_quadrants": ARC_QUADRANTS,
                    "outline_uncertainty_margin_mm": outline_error, "graph_scope": graph_scope}
        closed.add(current)
        expanded += 1
        indices = np.asarray([j for j in range(len(nodes)) if j not in closed], dtype=int)
        if not len(indices):
            break
        pairs = np.stack([np.repeat(coords[current][None, :], len(indices), axis=0), coords[indices]], axis=1)
        visible = shapely.covers(free, shapely.linestrings(pairs))
        for index in indices[visible]:
            index = int(index)
            value = cost + math.dist(nodes[current], nodes[index])
            if value < distances[index]:
                distances[index], parent[index] = value, current
                heapq.heappush(queue, (value + math.dist(nodes[index], end), value, index))
    return {"status": "no_path", "reason": "No visible path in the bounded conservative model",
            "vertices": len(nodes), "expanded": expanded, "width_mm": width, "clearance_mm": clearance}


def candidate(before, task, result, folder):
    """Add only searched target segments to a byte-preserved project copy."""
    board, _ = copy_project(before, folder)
    text = before.read_text(encoding="utf-8")
    roots = parse_sexpr(text)
    codes = [n[1] for n in roots[0][1:] if name(n) == "net" and len(n) >= 3
             and semantic_string(n[2]) == task["target_net"]]
    if len(codes) == 1 and str(codes[0]).isdigit():
        net_token = str(codes[0])
    elif not codes and not any(name(n) == "net" for n in roots[0][1:]):
        # KiCad 10 saves net names directly on conductive objects.
        pads = [p for fp in roots[0][1:] if name(fp) == "footprint"
                for p in fp[1:] if name(p) == "pad"]
        if not any(name(n) == "net" and len(n) == 2 and isinstance(n[1], Quoted)
                   and semantic_string(n[1]) == task["target_net"] for p in pads for n in p[1:]):
            raise ValueError("No supported target net representation")
        net_token = json.dumps(task["target_net"], ensure_ascii=False)
    else:
        raise ValueError("Ambiguous numeric input net declaration")
    # Quantize all shared endpoints identically. Remove only coincident
    # rounded points, never skip a short edge leaving disconnected neighbours.
    points = []
    for point in result["path_mm"]:
        point = tuple(round(x * 1e6) / 1e6 for x in point)
        if not points or point != points[-1]:
            points.append(point)
    if len(points) < 2:
        raise ValueError("No nonzero route after native coordinate quantization")
    tracks = []
    for a, b in zip(points, points[1:]):
        tracks.append('(segment (start %.9f %.9f) (end %.9f %.9f) (width %.9f) '
                      '(layer %s) (net %s) (uuid "%s"))' %
                      (*a, *b, result["width_mm"], json.dumps(task["allowed_layer"]), net_token, uuid.uuid4()))
    end = text.rfind(")")
    if end < 0 or text[end+1:].strip():
        raise ValueError("Unexpected trailing board data")
    board.write_text(text[:end] + "\n" + "\n".join(tracks) + "\n" + text[end:], encoding="utf-8")
    return board


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=False)
    hashes = source_files(args.board)
    started = time.monotonic()
    try:
        document = json.loads(args.model.read_text())
        if document["status"] != "ready":
            result = {"status": "unsupported", "reason": document.get("reason")}
        else:
            result = search(document["model"], timeout=args.timeout)
            if result["status"] == "found":
                board = candidate(args.board, document["model"]["task"], result, args.out_dir / "candidate")
                result.update(candidate_board=str(board), candidate_hashes=source_files(board), requires_native_verification=True)
    except SearchLimit as exc:
        result = exc.result()
    except Exception as exc:
        result = {"status": "unsupported", "reason": type(exc).__name__ + ": " + str(exc)}
    result.update(seconds=time.monotonic()-started, input_unchanged=source_files(args.board) == hashes)
    (args.out_dir / "search.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["input_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
