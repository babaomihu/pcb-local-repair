"""Independent, exact native fill witness for explicit local refill permission.

Extraction only reads already-refilled, hash-bound boards in KiCad's runtime.
Comparison runs in the pinned Shapely runtime. Neither command changes a board;
zone definitions, electrical connectivity and DRC remain separate mandatory gates.
"""
import argparse
import hashlib
import json
import math
import os
import traceback
from pathlib import Path
import re
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcb_local.zone_refill import layer_contract
from pcb_local.native_layers import copper_layer_identity

NATIVE_VERSION = '10.0.4'
META_KEYS = ('net', 'locked', 'rule_area', 'source_owner_uuid', 'layers')


def extract(board_path, expected_sha256):
    """Read every copper-layer fill, without running refill or saving a board."""
    path = Path(board_path).resolve()
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256 or ''):
        raise ValueError('Missing or invalid refilled board SHA256')
    digest = lambda: hashlib.sha256(path.read_bytes()).hexdigest()
    if digest() != expected_sha256:
        raise ValueError('Refilled board hash differs before native fill extraction')
    import wx
    app = wx.GetApp() or wx.App(False)
    import pcbnew
    if str(pcbnew.Version()) != NATIVE_VERSION:
        raise ValueError('Native fill witness requires KiCad ' + NATIVE_VERSION)
    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise ValueError('Native KiCad could not read the refilled board')
    layer_ids = list(board.GetEnabledLayers().CuStack())
    identities = copper_layer_identity(board)
    layer_names = {row["native_id"]: row["canonical_name"] for row in identities}
    layers = [layer_names[int(i)] for i in layer_ids]

    def polygons(poly):
        def ring(chain):
            if chain.ArcCount():
                raise ValueError('Native zone fill contains unflattened arcs')
            return [[chain.CPoint(i).x / 1e6, chain.CPoint(i).y / 1e6]
                    for i in range(chain.PointCount())]
        return [dict(shell=ring(poly.Outline(i)),
                     holes=[ring(poly.Hole(i, j)) for j in range(poly.HoleCount(i))])
                for i in range(poly.OutlineCount())]

    native_zones = [(zone, None) for zone in board.Zones()]
    native_zones += [(zone, str(fp.m_Uuid.AsString()))
                    for fp in board.GetFootprints() for zone in fp.Zones()]
    zones = []
    for zone, owner in native_zones:
        on_layers = [i for i in layer_ids if zone.IsOnLayer(i)]
        rule_area = bool(zone.GetIsRuleArea())
        fills = {layer_names[int(i)]: [] if rule_area else
                 polygons(zone.GetFilledPolysList(i)) for i in on_layers}
        zones.append(dict(uuid=str(zone.m_Uuid.AsString()), net=str(zone.GetNetname()),
                          locked=bool(zone.IsLocked()), rule_area=rule_area,
                          source_owner_uuid=owner, layers=list(fills), fills=fills))
    if len({z['uuid'] for z in zones}) != len(zones):
        raise ValueError('Native zone identities are not unique')
    if digest() != expected_sha256:
        raise ValueError('Refilled board changed during native fill extraction')
    return dict(status='ready', schema_version=1, native_version=NATIVE_VERSION,
                board_path=str(path), board_sha256=expected_sha256,
                copper_layers=layers, native_layer_identity=identities, zones=sorted(zones, key=lambda z: z['uuid']))


def _snapshot(value):
    """Validate extraction coverage before comparing any geometry."""
    if (value.get('status') != 'ready' or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or value.get('native_version') != NATIVE_VERSION
            or not re.fullmatch(r'[0-9a-f]{64}', value.get('board_sha256', ''))):
        raise ValueError('Invalid native fill extraction or board hash')
    layers = value.get('copper_layers')
    if (not isinstance(layers, list) or not layers or len(layers) != len(set(layers))
            or any(not isinstance(l, str) or not re.fullmatch(r'(F|B|In[1-9][0-9]*)\.Cu', l) for l in layers)):
        raise ValueError('Invalid native copper-layer coverage')
    if not isinstance(value.get('zones'), list):
        raise ValueError('Missing native zone coverage')
    result = {}
    for row in value['zones']:
        uid = row.get('uuid')
        if (not isinstance(uid, str) or not uid or uid in result
                or not isinstance(row.get('net'), str)
                or type(row.get('locked')) is not bool or type(row.get('rule_area')) is not bool
                or not (row.get('source_owner_uuid') is None or isinstance(row['source_owner_uuid'], str))
                or 'source_owner_uuid' not in row
                or not isinstance(row.get('layers'), list)
                or len(row['layers']) != len(set(row['layers']))
                or not set(row['layers']) <= set(layers)
                or not isinstance(row.get('fills'), dict) or set(row['fills']) != set(row['layers'])):
            raise ValueError('Incomplete or duplicate native zone identity/coverage')
        if row['rule_area'] and any(row['fills'].values()):
            raise ValueError('Native rule area unexpectedly contains a copper fill')
        result[uid] = row
    return result


def _copper(polygons):
    import shapely
    from decimal import Decimal
    from pcb_local.geometry_search import native_polygon
    if not isinstance(polygons, list):
        raise ValueError('Invalid native fill polygon list')
    shapes = []
    for polygon in polygons:
        if not isinstance(polygon, dict) or set(polygon) != {'shell', 'holes'} or not isinstance(polygon['holes'], list):
            raise ValueError('Invalid native fill polygon structure')
        for ring in [polygon['shell'], *polygon['holes']]:
            if not isinstance(ring, list) or len(ring) < 3:
                raise ValueError('Invalid native fill ring')
            for point in ring:
                if (not isinstance(point, list) or len(point) != 2
                        or any(type(c) not in (int, float) or not math.isfinite(c) for c in point)):
                    raise ValueError('Invalid native fill coordinate')
                if any(Decimal(str(c)) * 1000000 != (Decimal(str(c)) * 1000000).to_integral_value() for c in point):
                    raise ValueError('Native fill coordinate exceeds integer nanometre precision')
        shapes.append(native_polygon(polygon))
    shape = shapely.union_all(shapes)
    if not shape.is_valid:
        raise ValueError('Invalid native copper union topology')
    return shape


def compare(before, after, task):
    """Check exact equality outside the explicit zone/layer/ROI permission.

    Area values are diagnostic only. Exact geometry predicates decide acceptance,
    so even a one-nanometre sliver outside the ROI is a protection violation.
    """
    from shapely.geometry import box
    selected = layer_contract(task)
    old, new = _snapshot(before), _snapshot(after)
    if task['allowed_layer'] not in before['copper_layers']:
        raise ValueError('Authorized refill layer is absent from the native board')
    for uid in selected:
        if uid not in old or not set(selected[uid]) <= set(old[uid]['layers']):
            raise ValueError('Selected zone is absent from an authorized layer')
        zone = old[uid]
        if (zone['locked'] or zone['rule_area'] or zone['source_owner_uuid'] is not None
                or not zone['net'] or zone['net'] == task['target_net']):
            raise ValueError('Only unlocked board zones on a foreign net may participate in refill')
    region = box(*task['region_mm'])
    identities_equal = set(old) == set(new)
    stack_equal = before['copper_layers'] == after['copper_layers']
    metadata_equal = identities_equal and all(
        all(old[uid][k] == new[uid][k] for k in META_KEYS) for uid in old)
    rows = []
    for uid in sorted(set(old) | set(new)):
        old_zone, new_zone = old.get(uid), new.get(uid)
        old_fills = old_zone['fills'] if old_zone else {}
        new_fills = new_zone['fills'] if new_zone else {}
        for layer in sorted(set(old_fills) | set(new_fills)):
            a, b = _copper(old_fills.get(layer, [])), _copper(new_fills.get(layer, []))
            changed = a.symmetric_difference(b)
            allowed = layer in selected.get(uid, [])
            outside = changed.difference(region) if allowed else changed
            preserved = region.covers(changed) if allowed and not changed.is_empty else changed.is_empty
            rows.append(dict(uuid=uid, layer=layer,
                             authorized_local_refill=allowed,
                             exact_equal=a.equals(b), scope_preserved=bool(preserved),
                             added_area_mm2=b.difference(a).area,
                             removed_area_mm2=a.difference(b).area,
                             unauthorized_change_area_mm2=outside.area,
                             changed_bounds_mm=list(changed.bounds) if not changed.is_empty else None,
                             unauthorized_change_bounds_mm=list(outside.bounds) if not outside.is_empty else None))
    passed = identities_equal and stack_equal and metadata_equal and all(r['scope_preserved'] for r in rows)
    return dict(status='verified', schema_version=1,
                checks={'zone_refill_scope_preserved': bool(passed)},
                source_sha256={'before': before['board_sha256'], 'after': after['board_sha256']},
                zone_uuids=sorted(selected), zone_layers=selected,
                allowed_layer=task['allowed_layer'], region_mm=task['region_mm'],
                zone_identities_equal=identities_equal, copper_layers_equal=stack_equal,
                zone_metadata_equal=metadata_equal, zones=rows,
                policy='Exact fill equality on all unselected zones/layers; selected-zone symmetric difference contained in the unchanged task ROI. Area is diagnostic, never an acceptance tolerance. Zone definitions, electrical relations and full DRC require independent checks.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    extract_parser = subs.add_parser('extract')
    extract_parser.add_argument('--board', type=Path, required=True)
    extract_parser.add_argument('--sha256', required=True)
    extract_parser.add_argument('--output', type=Path, required=True)
    compare_parser = subs.add_parser('compare')
    compare_parser.add_argument('--before', type=Path, required=True)
    compare_parser.add_argument('--after', type=Path, required=True)
    compare_parser.add_argument('--task', type=Path, required=True)
    compare_parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'extract':
            result = extract(args.board, args.sha256)
            code = 0
        else:
            result = compare(json.loads(args.before.read_text()), json.loads(args.after.read_text()),
                             json.loads(args.task.read_text()))
            code = 0 if all(result['checks'].values()) else 1
    except Exception as exc:
        result = dict(status='unsupported', reason=str(exc), checks={})
        code = 2
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    return code


def entrypoint(work=main):
    """Exit only after read-only work/output closes; preserve failure status."""
    try:
        code = work()
    except SystemExit as exc:
        code = exc.code if type(exc.code) is int else 2
    except BaseException:
        traceback.print_exc()
        code = 2
    if type(code) is not int:
        code = 2
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # No board writes; native teardown can otherwise stall.


if __name__ == '__main__':
    entrypoint()
