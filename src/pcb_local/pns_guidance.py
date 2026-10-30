"""Bounded, obstacle-witnessed cursor lanes for the existing native PNS engine.

This is optimistic guidance, never a routing or acceptance oracle. Only exact
authorized movable segments are absent here; the engine still receives them.
"""
import argparse
import json
import math
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcb_local.geometry_search import configuration_space, SearchLimit, MAX_VERTICES
from pcb_local.modification import contract
from pcb_local.partial_routing import expand, validated_plans
from pcb_local.zone_refill import search_model


def guidance(source, timeout=5):
    from shapely import affinity
    from shapely.geometry import Point, LineString
    deadline = time.monotonic() + timeout
    def check():
        if time.monotonic() >= deadline:
            raise SearchLimit('Cursor guidance exhausted its shared-budget allocation')
    plans = validated_plans(source)
    model = expand(source, plans)
    allowed = set(contract(source['task'])['routing_uuids'])
    for plan in plans:
        allowed.discard(plan['parent_uuid'])
        allowed.update(p['uuid'] for p in plan['children'] if not p['locked'])
    omitted = [p['uuid'] for p in model['primitives']
               if p['uuid'] in allowed and p['kind'] == 'segment' and not p.get('locked', True)]
    model['primitives'] = [p for p in model['primitives'] if p['uuid'] not in omitted]
    model, refill = search_model(model)
    free, width, clearance, _ = configuration_space(model, check)
    endpoints = [(p.get('native_pad') or {}).get('shape_center', p['center']) for p in model['endpoints']]
    start, end = endpoints
    result = dict(schema_version=1, status='no_guidance', plans=[],
                  omitted_movable_segments=sorted(omitted), zone_refill=refill,
                  considered_fixed_obstacle_uuids=sorted(p['uuid'] for p in model['primitives']
                      if p['net'] != model['task']['target_net'] or p.get('blocks_same_net')),
                  width_mm=width, clearance_mm=clearance,
                  policy='Fixed-obstacle conservative lanes; optimistic only for authorized movable copper; native PNS and complete acceptance remain mandatory')
    if math.dist(start, end) <= 1e-6:
        return result
    angle = math.degrees(math.atan2(end[1]-start[1], end[0]-start[0]))
    local = affinity.rotate(free, -angle, origin=start)
    sx, sy = start; ex = sx + math.dist(start, end)
    parts = list(local.geoms) if local.geom_type == 'MultiPolygon' else [local]
    parts = [p for p in parts if p.geom_type == 'Polygon' and p.covers(Point(sx, sy)) and p.covers(Point(ex, sy))]
    if not parts:
        result['reason'] = 'No common conservative fixed-obstacle component; this is not a physical impossibility proof'
        return result
    local = parts[0]
    vertices = [xy for ring in [local.exterior, *local.interiors] for xy in ring.coords[:-1]]
    if len(vertices) > MAX_VERTICES:
        raise SearchLimit('Cursor guidance exceeds bounded vertex count')
    levels = sorted({sy, *(y for _, y in vertices)})
    # A waypoint inside the starting pad has no committable track. Place the
    # lane anchors past the native endpoint envelope, without extra attempts.
    exits = [math.hypot(*p.get('size', [0, 0]))/2 + width/2 + clearance
             for p in model['endpoints']]
    if sum(exits) >= ex-sx:
        result['reason'] = 'No distinct pad-exit lane anchors within endpoint span'
        return result
    candidates = []
    for lo, hi in zip(levels, levels[1:]):
        check()
        if hi-lo <= 2e-5:
            continue
        y = (lo+hi)/2
        line = LineString([(sx, sy), (sx+exits[0], y), (ex-exits[1], y), (ex, sy)])
        if local.covers(line):
            candidates.append((y, line))
    for name, choices in [('guide-low', [c for c in candidates if c[0] < sy]),
                          ('guide-high', [c for c in candidates if c[0] > sy])]:
        if not choices:
            continue
        y, line = min(choices, key=lambda c:c[0]) if name.endswith('low') else max(choices, key=lambda c:c[0])
        world = affinity.rotate(line, angle, origin=start)
        path = [[round(x, 6), round(y, 6)] for x,y in world.coords]
        # Integer-coordinate rounding must retain the fixed-space witness.
        if not free.covers(LineString(path)):
            continue
        result['plans'].append(dict(policy=name, waypoints=path[1:-1],
                                    fixed_space_witness=True, length_mm=world.length))
    result['status'] = 'ready' if result['plans'] else 'no_guidance'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=5)
    args=parser.parse_args()
    try:
        value=json.loads(args.source.read_text())
        result=guidance(value.get('model', value), args.timeout)
    except (ValueError, SearchLimit) as exc:
        result=dict(status='unavailable', reason=str(exc), plans=[])
    args.output.write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    main()
