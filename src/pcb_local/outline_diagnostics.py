"""Read-only board-outline diagnostics; never authorizes or changes routing.

Native KiCad extraction and host geometry analysis are deliberately separate:
KiCad's Python needs no Shapely, and the router runtime needs no pcbnew.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys

SCHEMA_VERSION = 1
MAX_OUTLINE_VERTICES = 250000
NOTICE = ('Diagnostic only: board material within the ROI does not prove full-task '
          'reachability or unreachability. Original unsupported result, rules, ROI, '
          'routing model and acceptance requirements remain unchanged.')


def unavailable(reason):
    return {'schema_version': SCHEMA_VERSION, 'status': 'unavailable',
            'reason': str(reason), 'diagnostic_only': True, 'notice': NOTICE}


def _region(region):
    values = list(map(float, region))
    if (len(values) != 4 or not all(math.isfinite(v) for v in values)
            or values[0] >= values[2] or values[1] >= values[3]):
        raise ValueError('Invalid diagnostic ROI')
    return values


def native_outline(board_path, region_mm):
    """Read full native outline and every Edge.Cuts envelope, or fail closed.

    This function runs only under the pinned KiCad Python. It does not save,
    refill, alter design settings, or infer a missing board outline.
    """
    try:
        region = _region(region_mm)
        path = Path(board_path)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        import wx
        app = wx.GetApp() or wx.App(False)
        import pcbnew
        if pcbnew.Version() != '10.0.4':
            return unavailable('Outline diagnostic requires KiCad 10.0.4')
        board = pcbnew.LoadBoard(str(path))
        if board is None:
            return unavailable('Native board load failed')
        count = 0

        def polygons(poly):
            def chain(ring):
                nonlocal count
                if ring.ArcCount():
                    raise ValueError('Unflattened native outline arcs')
                count += ring.PointCount()
                if count > MAX_OUTLINE_VERTICES:
                    raise ValueError('Diagnostic outline vertex bound exceeded; no partial analysis')
                return [[ring.CPoint(i).x / 1e6, ring.CPoint(i).y / 1e6]
                        for i in range(ring.PointCount())]
            return [{'shell': chain(poly.Outline(i)),
                     'holes': [chain(poly.Hole(i, j)) for j in range(poly.HoleCount(i))]}
                    for i in range(poly.OutlineCount())]

        outline = pcbnew.SHAPE_POLY_SET()
        valid = bool(board.GetBoardPolygonOutlines(outline, False, None, False, False))
        if not valid or outline.OutlineCount() == 0:
            return unavailable('Native complete board outline unavailable; inference disabled')
        geometry = polygons(outline)
        edges = []
        drawings = [(item, None) for item in board.GetDrawings()]
        drawings += [(item, str(fp.m_Uuid.AsString()))
                     for fp in board.GetFootprints() for item in fp.GraphicalItems()]
        for item, owner in drawings:
            if item.GetLayer() != pcbnew.Edge_Cuts:
                continue
            if type(item).__name__ != 'PCB_SHAPE':
                raise ValueError('Unsupported non-shape Edge.Cuts item; no partial analysis')
            envelope = pcbnew.SHAPE_POLY_SET()
            item.TransformShapeToPolygon(envelope, pcbnew.Edge_Cuts, 0, 100, pcbnew.ERROR_OUTSIDE)
            if envelope.OutlineCount() == 0:
                raise ValueError('Missing native Edge.Cuts envelope; no partial analysis')
            edges.append({'uuid': str(item.m_Uuid.AsString()), 'owner_uuid': owner,
                          'shape': int(item.GetShape()), 'polygons': polygons(envelope)})
        if not edges:
            raise ValueError('No native edge witnesses')
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        if after != before:
            return unavailable('Source board changed during diagnostic extraction')
        design = board.GetDesignSettings()
        return {'schema_version': SCHEMA_VERSION, 'status': 'raw', 'native_version': pcbnew.Version(),
                'diagnostic_only': True, 'notice': NOTICE, 'source_board': str(path.resolve()),
                'source_sha256': before, 'source_sha256_after': after, 'source_unchanged': True,
                'region_mm': region, 'complete_outline': True, 'inferred_outline': False,
                'outline': geometry, 'edges': edges, 'native_vertices': count,
                'precision': {'outline_max_error_mm': design.m_MaxError / 1e6,
                              'outline_chaining_epsilon_mm': board.GetOutlinesChainingEpsilon() / 1e6,
                              'edge_envelope_error_mm': .0001,
                              'edge_envelope_policy': 'Native ERROR_OUTSIDE; authored edge stroke retained'},
                'edge_clearance_mm': design.m_CopperEdgeClearance / 1e6}
    except Exception as exc:
        return unavailable(exc)


def analyze_outline(raw):
    """Analyze complete raw geometry without changing any model/acceptance flag."""
    try:
        if raw.get('status') == 'unavailable':
            return unavailable(raw.get('reason', 'Native extraction unavailable'))
        if (raw.get('schema_version') != SCHEMA_VERSION or raw.get('status') != 'raw'
                or raw.get('native_version') != '10.0.4' or not raw.get('complete_outline')
                or raw.get('inferred_outline') is not False or not raw.get('source_unchanged')
                or not raw.get('source_sha256')
                or raw.get('source_sha256') != raw.get('source_sha256_after')):
            raise ValueError('Missing complete identity-bound native outline evidence')
        import shapely
        from shapely.geometry import box
        if __package__:
            from .geometry_search import native_polygon
        else:
            from pcb_local.geometry_search import native_polygon
        region = _region(raw['region_mm'])
        roi = box(*region)

        def geometry(polys):
            if not polys:
                raise ValueError('Empty native outline/envelope witness')
            result = shapely.union_all([native_polygon(p) for p in polys])
            if result.is_empty or not result.is_valid:
                raise ValueError('Invalid native outline/envelope geometry')
            return result

        board = geometry(raw['outline'])
        clipped = board.intersection(roi)

        def surfaces(g):
            if g.geom_type == 'Polygon':
                return [g] if not g.is_empty and g.area > 0 else []
            return [p for part in getattr(g, 'geoms', ()) for p in surfaces(part)]

        parts = surfaces(clipped)
        hits = []
        ids = set()
        if not raw.get('edges'):
            raise ValueError('Missing complete Edge.Cuts witnesses')
        for edge in raw['edges']:
            uid = edge['uuid']
            if not uid or uid in ids:
                raise ValueError('Missing/duplicate Edge.Cuts identity')
            ids.add(uid)
            shape = geometry(edge['polygons'])
            if shape.intersects(roi):
                hits.append({'uuid': uid, 'owner_uuid': edge.get('owner_uuid'),
                             'shape': edge['shape'], 'bounds_mm': list(shape.bounds),
                             'relation': 'native_outer_edge_envelope_intersects_roi'})
        whole_inside = bool(board.covers(roi))
        interpretation = ('ROI contains multiple separate board-material regions; '
                          'existing copper elsewhere may still affect task connectivity.' if len(parts) > 1
                          else 'ROI intersects the board boundary or outside material.' if not whole_inside
                          else 'ROI is covered by the native approximated board outline; this is not a routing acceptance proof.')
        return {'schema_version': SCHEMA_VERSION, 'status': 'analyzed', 'diagnostic_only': True,
                'notice': NOTICE, 'native_version': raw['native_version'],
                'source_board': raw.get('source_board'), 'source_sha256': raw['source_sha256'],
                'source_unchanged': True, 'complete_outline': True, 'inferred_outline': False,
                'region_mm': region, 'precision': raw.get('precision'),
                'outline_components': len(surfaces(board)),
                'outline_holes': sum(len(p.interiors) for p in surfaces(board)),
                'roi_board_components': len(parts), 'roi_board_holes': sum(len(p.interiors) for p in parts),
                'roi_wholly_in_native_outline': whole_inside,
                'roi_outside_outline_area_mm2': roi.difference(board).area,
                'intersecting_edges': hits, 'edge_witness_count': len(ids),
                'interpretation': interpretation, 'physical_unreachability_proven': False,
                'search_model_changed': False, 'acceptance_authorized': False}
    except Exception as exc:
        return unavailable(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--native', action='store_true')
    mode.add_argument('--analyze', type=Path)
    parser.add_argument('--board', type=Path)
    parser.add_argument('--region', nargs=4, type=float)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.native and (args.board is None or args.region is None):
        parser.error('--native requires --board and --region')
    try:
        result = native_outline(args.board, args.region) if args.native else analyze_outline(json.loads(args.analyze.read_text()))
    except Exception as exc:
        result = unavailable(exc)
    with args.output.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'status': result['status'], 'output': str(args.output)}, ensure_ascii=False), flush=True)
    return 0 if result['status'] in ('raw', 'analyzed') else 2


if __name__ == '__main__':
    if __package__ in (None, ''):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    if '--native' in sys.argv:
        os._exit(code)  # wx/KiCad teardown can hang after successful read-only extraction.
    raise SystemExit(code)
