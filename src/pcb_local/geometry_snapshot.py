"""Read a conservative primitive model with native KiCad; never save a board.

This optional search model has a narrower scope than the main KRT path. It is
not a DRC checker. Filled zones require a hash-bound native preflight board.
Unknown copper, unsupported edge shapes and custom rules are never silently omitted.
"""
import argparse
import json
import hashlib
import os
import sys
from pathlib import Path

try:
    from .layer_stack import supported_stack
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pcb_local.layer_stack import supported_stack


from pcb_local.native_layers import copper_layer_identity, copper_layer_id


def snapshot(board_path, task, refilled_board=None, refilled_sha256=None, pns_model=False, require_endpoints=True, via_model=False):
    import wx
    app = wx.GetApp() or wx.App(False)
    import pcbnew

    path = Path(board_path)
    if not path.with_suffix(".kicad_pro").is_file():
        raise ValueError("Native geometry requires the authored project rules")
    geometry_path = Path(refilled_board) if refilled_board else path
    if refilled_board and (not refilled_sha256 or
            hashlib.sha256(geometry_path.read_bytes()).hexdigest() != refilled_sha256):
        raise ValueError("Refilled geometry does not match the preflight hash")
    board = pcbnew.LoadBoard(str(geometry_path))
    if board is None or pcbnew.Version() != "10.0.4":
        raise ValueError("Geometric model requires native KiCad 10.0.4")
    layer = copper_layer_id(board, task["allowed_layer"])
    from pcb_local.custom_clearance import resolve
    stack=[board.GetStandardLayerName(l) for l in board.GetEnabledLayers().CuStack()]
    custom=resolve(path,task['allowed_layer'],stack,conservative_stack=pns_model)
    design = board.GetDesignSettings()
    settings = design.m_NetSettings

    def point(p):
        return [p.x / 1e6, p.y / 1e6]

    def unsupported(message,item):
        error=ValueError(message)
        error.items=[dict(uuid=str(item.m_Uuid.AsString()),net=str(item.GetNetname()) if hasattr(item,'GetNetname') else '',
            kind=type(item).__name__,layer=board.GetStandardLayerName(item.GetLayer()),position_mm=point(item.GetPosition()),reason=message)]
        if message=='Geometric search requires straight board-edge segments':
            from pcb_local.outline_diagnostics import native_outline
            error.outline_diagnostics_raw=native_outline(geometry_path,task['region_mm'])
        raise error

    def base(item):
        nc = settings.GetEffectiveNetClass(item.GetNetname())
        return {"uuid": str(item.m_Uuid.AsString()), "net": str(item.GetNetname()),
                "clearance_mm": max(max(0, item.GetOwnClearance(layer), nc.GetClearance()) / 1e6,
                                    custom['search_minimum_mm'],design.m_MinClearance / 1e6),
                "track_width_mm": nc.GetTrackWidth() / 1e6}

    def polygons(poly):
        # Preserve exact integer vertices, including doubled bridge edges in
        # filled rings. The search worker removes only exact reverse pairs.
        def chain(ring):
            if ring.ArcCount():
                raise ValueError("Native polygon contains unflattened arcs")
            return [point(ring.CPoint(j)) for j in range(ring.PointCount())]
        return [{"shell": chain(poly.Outline(i)),
                 "holes": [chain(poly.Hole(i, j)) for j in range(poly.HoleCount(i))]}
                for i in range(poly.OutlineCount())]

    def copper_polygon(item):
        shape = pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(shape, layer, 0, 100, pcbnew.ERROR_OUTSIDE)
        return {**base(item), "kind": "polygon", "polygons": polygons(shape),
                "native_error_mm": .0001}

    def fixed_graphic(drawing, owner=None):
        if type(drawing).__name__ != 'PCB_SHAPE':
            unsupported('Geometric search supports fixed PCB_SHAPE copper graphics only',drawing)
        row=copper_polygon(drawing)
        row.update(source_kind='fixed_graphic',native_graphic_kind=int(drawing.GetShape()),
                   source_owner_uuid=str(owner.m_Uuid.AsString()) if owner is not None else None,
                   policy='Fixed native ERROR_OUTSIDE copper envelope; original graphic is never rewritten')
        if not row['polygons']:
            unsupported('Copper graphic has no witnessed native polygon',drawing)
        return row

    def native_pad(pad):
        shapes = {pcbnew.PAD_SHAPE_CIRCLE: 'circle', pcbnew.PAD_SHAPE_RECT: 'rect',
                  pcbnew.PAD_SHAPE_OVAL: 'oval', pcbnew.PAD_SHAPE_TRAPEZOID: 'trapezoid',
                  pcbnew.PAD_SHAPE_ROUNDRECT: 'roundrect', pcbnew.PAD_SHAPE_CHAMFERED_RECT: 'roundrect'}
        attrs = {pcbnew.PAD_ATTRIB_SMD: 'smd', pcbnew.PAD_ATTRIB_PTH: 'thru_hole',
                 pcbnew.PAD_ATTRIB_NPTH: 'np_thru_hole', pcbnew.PAD_ATTRIB_CONN: 'connect'}
        extra = [pad.GetSecondaryDrillSize(), pad.GetTertiaryDrillSize()]
        if (pad.GetShape() not in shapes or pad.GetAttribute() not in attrs or
                pad.Padstack().Mode() != pcbnew.PADSTACK.MODE_NORMAL or
                pad.GetRemoveUnconnected() or pad.IsFreePad() or
                any(v.x or v.y for v in extra)):
            return None
        drill = point(pad.GetDrillSize())
        if any(drill) and pad.GetDrillShape() not in (pcbnew.PAD_DRILL_SHAPE_CIRCLE, pcbnew.PAD_DRILL_SHAPE_OBLONG):
            return None
        layers = [board.GetStandardLayerName(l) for l in board.GetEnabledLayers().CuStack() if pad.IsOnLayer(l)]
        return dict(shape=shapes[pad.GetShape()], attribute=attrs[pad.GetAttribute()],
            offset=point(pad.GetOffset()), drill=drill,
            drill_shape='oval' if pad.GetDrillShape() == pcbnew.PAD_DRILL_SHAPE_OBLONG else 'circle',
            copper_layers=layers, delta=point(pad.GetDelta()),
            roundrect_ratio=pad.GetRoundRectRadiusRatio(), chamfer_ratio=pad.GetChamferRectRatio(),
            chamfer_corners=pad.GetChamferPositions(), shape_center=point(pad.ShapePos(layer)))

    primitives, pads, edges, vias = [], {}, [], []
    zones = list(board.Zones())
    for fp in board.GetFootprints():
        zones.extend(fp.Zones())
        for drawing in fp.GraphicalItems():
            if drawing.GetLayer() == layer:
                primitives.append(fixed_graphic(drawing,fp))
        for pad in fp.Pads():
            drill = pad.GetDrillSize()
            if drill.x or drill.y:
                shape = pcbnew.SHAPE_POLY_SET()
                pad.TransformHoleToPolygon(shape, 0, 100, pcbnew.ERROR_OUTSIDE)
                primitives.append({**base(pad), "kind": "hole", "polygons": polygons(shape),
                    "blocks_same_net": pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH,
                    "hole_clearance_mm": design.m_HoleClearance / 1e6,
                    "native_error_mm": .0001})
            if not pad.IsOnLayer(layer):
                continue
            shape = pad.GetShape()
            analytic = (pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD and
                        shape in (pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_RECT) and
                        not (pad.GetOffset().x or pad.GetOffset().y))
            # Standard plated pads are valid centre anchors just like the
            # already supported fixed through vias. Their own plated hole is
            # not a foreign-net obstacle; full native acceptance still proves
            # actual copper contact. Do not extend this to NPTH, offset pads,
            # custom padstacks, backdrills or removable copper layers.
            standard_pth = (pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH and
                            native_pad(pad) is not None)
            row = {**(base(pad) if analytic else copper_polygon(pad)),
                   "center": point(pad.GetPosition()), "size": point(pad.GetSize()),
                   "angle_degrees": -pad.GetOrientationDegrees(),
                   "endpoint_supported": (pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD or standard_pth) and
                                          not (pad.GetOffset().x or pad.GetOffset().y)}
            if pns_model:
                row['native_pad'] = native_pad(pad)
                row['custom_pad_projection'] = (shape == pcbnew.PAD_SHAPE_CUSTOM and
                    pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD and pad.Padstack().Mode() == pcbnew.PADSTACK.MODE_NORMAL and
                    not (drill.x or drill.y or pad.GetOffset().x or pad.GetOffset().y) and
                    not pad.IsFreePad() and not pad.GetRemoveUnconnected() and
                    not any(v.x or v.y for v in (pad.GetSecondaryDrillSize(),pad.GetTertiaryDrillSize())) and
                    [board.GetStandardLayerName(l) for l in board.GetEnabledLayers().CuStack() if pad.IsOnLayer(l)] == [task['allowed_layer']])
                row['endpoint_supported'] = (row['native_pad'] is not None and pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH) or row['custom_pad_projection']
            if analytic:
                row["kind"] = "circle" if shape == pcbnew.PAD_SHAPE_CIRCLE else "rect"
            if shape == pcbnew.PAD_SHAPE_CUSTOM:
                # Separate exact authored polygon witness from the conservative
                # ERROR_OUTSIDE search envelope. Only the fixed-fill adapter
                # consumes this; other geometric search keeps its old envelope.
                exact = pcbnew.SHAPE_POLY_SET()
                pad.TransformShapeToPolygon(exact, layer, 0, 100, pcbnew.ERROR_INSIDE)
                row['authored_polygons'] = polygons(exact)
            if row["kind"] == "circle" and row["size"][0] != row["size"][1]:
                raise ValueError("Noncircular round pad")
            primitives.append(row)
            if row["uuid"] in pads:
                raise ValueError("Duplicate pad identity in geometric model")
            pads[row["uuid"]] = row
    for track in board.GetTracks():
        kind = type(track).__name__
        if kind not in ("PCB_TRACK", "PCB_ARC", "PCB_VIA"):
            raise ValueError("Unmodelled routing item type")
        if kind == "PCB_VIA" and track.IsOnLayer(layer):
            vias.append({**base(track), "center": point(track.GetPosition()),
                "diameter_mm": track.GetWidth(layer) / 1e6,
                "drill_mm": track.GetDrillValue() / 1e6,
                "layers": [board.GetStandardLayerName(track.TopLayer()), board.GetStandardLayerName(track.BottomLayer())],
                "copper_layers": [board.GetStandardLayerName(l) for l in board.GetEnabledLayers().CuStack() if track.IsOnLayer(l)],
                "locked": bool(track.IsLocked()),
                "standard_through": track.GetViaType() == pcbnew.VIATYPE_THROUGH and
                    track.Padstack().Mode() == pcbnew.PADSTACK.MODE_NORMAL and
                    not track.GetRemoveUnconnected() and not track.GetIsFree() and
                    not any(v is not None and (v.x or v.y) for v in (track.GetSecondaryDrillSize(),track.GetTertiaryDrillSize()))})
            primitives.append({**base(track), "kind": "hole", "center": point(track.GetPosition()),
                "diameter_mm": track.GetDrillValue() / 1e6,
                "hole_clearance_mm": design.m_HoleClearance / 1e6})
        if kind == "PCB_TRACK" and track.GetLayer() == layer:
            primitives.append({**base(track), "kind": "segment",
                "start": point(track.GetStart()), "end": point(track.GetEnd()),
                "width_mm": track.GetWidth() / 1e6, "locked": bool(track.IsLocked())})
        elif kind != "PCB_TRACK" and track.IsOnLayer(layer):
            row = copper_polygon(track)
            if kind == "PCB_ARC" and pns_model:
                row['native_arc'] = dict(start=point(track.GetStart()), mid=point(track.GetMid()),
                    end=point(track.GetEnd()), width_mm=track.GetWidth()/1e6)
            primitives.append(row)
    for zone in zones:
        if not zone.IsOnLayer(layer):
            continue
        if zone.GetIsRuleArea():
            if (zone.GetDoNotAllowVias() if via_model else zone.GetDoNotAllowTracks()):
                primitives.append({"uuid": str(zone.m_Uuid.AsString()), "net": "", "kind": "keepout",
                    "blocks_same_net": True, "clearance_mm": 0,
                    "polygons": polygons(zone.Outline())})
        else:
            if not refilled_board:
                raise ValueError("Zones require hash-bound geometry from completed native refill/preflight")
            primitives.append({**base(zone), "kind": "polygon", "source_kind": "zone_fill",
                "locked": bool(zone.IsLocked()), "min_thickness_mm": zone.GetMinThickness() / 1e6,
                "source_owner_uuid": next((str(fp.m_Uuid.AsString()) for fp in board.GetFootprints() if any(str(z.m_Uuid.AsString()) == str(zone.m_Uuid.AsString()) for z in fp.Zones())), None),
                "polygons": polygons(zone.GetFilledPolysList(layer))})
    for drawing in board.GetDrawings():
        if drawing.GetLayer() == layer:
            primitives.append(fixed_graphic(drawing))
    # Read the full native board, including footprint-local Edge.Cuts after
    # KiCad's world-coordinate transform. Never infer a missing outer contour.
    outline = pcbnew.SHAPE_POLY_SET()
    if not board.GetBoardPolygonOutlines(outline, False, None, False, False) or not outline.OutlineCount():
        raise ValueError('Native complete board outline unavailable; inference disabled')
    outline_polygons = polygons(outline)
    outline_shapes, edge_envelopes = [], []
    drawings = [(d,None) for d in board.GetDrawings()]
    drawings += [(d,str(fp.m_Uuid.AsString())) for fp in board.GetFootprints() for d in fp.GraphicalItems()]
    for drawing,owner in drawings:
        if drawing.GetLayer() != pcbnew.Edge_Cuts:continue
        if type(drawing).__name__ != 'PCB_SHAPE':
            unsupported('Unsupported non-shape board edge',drawing)
        kind=drawing.GetShape();source=str(drawing.m_Uuid.AsString())
        shared=dict(source_uuid=source,owner_uuid=owner,width_mm=drawing.GetWidth()/1e6)
        if kind == pcbnew.SHAPE_T_SEGMENT:
            outline_shapes.append(dict(shared,kind='segment',start=point(drawing.GetStart()),end=point(drawing.GetEnd())))
        elif kind == pcbnew.SHAPE_T_RECT:
            corners=[point(v) for v in drawing.GetRectCorners()]
            if len(corners)!=4:raise ValueError('Native rectangle does not have four corners')
            outline_shapes.extend(dict(shared,kind='segment',start=a,end=b) for a,b in zip(corners,corners[1:]+corners[:1]))
        elif kind == pcbnew.SHAPE_T_POLY:
            # KiCad materializes a non-axis-aligned footprint rectangle as a
            # world-coordinate polygon. Keep every ring edge, without a bbox.
            for polygon in polygons(drawing.GetPolyShape()):
                for ring in [polygon['shell'],*polygon['holes']]:
                    if len(ring)<3:raise ValueError('Incomplete native polygon edge')
                    outline_shapes.extend(dict(shared,kind='segment',start=a,end=b) for a,b in zip(ring,ring[1:]+ring[:1]) if a!=b)
        elif kind == pcbnew.SHAPE_T_ARC:
            outline_shapes.append(dict(shared,kind='arc',start=point(drawing.GetStart()),mid=point(drawing.GetArcMid()),end=point(drawing.GetEnd())))
        elif kind == pcbnew.SHAPE_T_CIRCLE:
            outline_shapes.append(dict(shared,kind='circle',center=point(drawing.GetCenter()),end=point(drawing.GetEnd())))
        else:
            unsupported('Native outline supports segment, rectangle, polygon, arc and circle edges only',drawing)
        envelope=pcbnew.SHAPE_POLY_SET()
        drawing.TransformShapeToPolygon(envelope,pcbnew.Edge_Cuts,0,100,pcbnew.ERROR_OUTSIDE)
        if not envelope.OutlineCount():raise ValueError('Missing native board-edge envelope')
        edge_envelopes.append(dict(source_uuid=source,owner_uuid=owner,polygons=polygons(envelope)))
    if not outline_shapes:raise ValueError('Native board has no witnessed edge objects')
    for polygon in outline_polygons:
        for ring in [polygon['shell'],*polygon['holes']]:
            edges.extend([[a,b] for a,b in zip(ring,ring[1:]+ring[:1]) if a!=b])
    outline_precision=dict(max_error_mm=design.m_MaxError/1e6,
        chaining_epsilon_mm=board.GetOutlinesChainingEpsilon()/1e6,edge_envelope_error_mm=.0001)
    selected = task.get("target_pad_uuids", [])
    if task.get('target_endpoints') and require_endpoints:
        endpoints=[]
        for end in task['target_endpoints']:
            uid=end['uuid'];kind=end['kind'];point_mm=end['position_mm']
            if kind=='pad':
                row=pads.get(uid)
                if row is None or not row['endpoint_supported'] or row['center']!=point_mm:
                    raise ValueError('Unsupported or changed pad endpoint')
                endpoints.append(row)
                continue
            matches=[t for t in board.GetTracks() if str(t.m_Uuid.AsString())==uid]
            if len(matches)!=1:raise ValueError('Endpoint object identity is ambiguous')
            item=matches[0]
            if not item.IsOnLayer(layer) or str(item.GetNetname())!=task['target_net']:
                raise ValueError('Endpoint layer/net mismatch')
            if kind=='segment' and type(item).__name__=='PCB_TRACK' and end['selector'] in ('start','end'):
                actual=point(item.GetStart() if end['selector']=='start' else item.GetEnd())
            elif kind=='via' and type(item).__name__=='PCB_VIA' and end['selector']=='center':
                via=next((v for v in vias if v['uuid']==uid),None)
                if not via or not via['standard_through']:raise ValueError('Unsupported via endpoint')
                actual=point(item.GetPosition())
            else:raise ValueError('Unsupported endpoint kind/selector')
            if actual!=point_mm:raise ValueError('Endpoint coordinate changed')
            endpoints.append({**base(item),'center':actual,'endpoint_supported':True})
        if len(endpoints)!=2:raise ValueError('Exactly two endpoints required')
    else:
        if require_endpoints and (len(selected) != 2 or len(set(selected)) != 2 or any(k not in pads for k in selected)):
            raise ValueError("Geometric search requires two distinct supported pad endpoints")
        endpoints = [pads[k] for k in selected if k in pads]
        if require_endpoints and any(not p["endpoint_supported"] for p in endpoints):
            raise ValueError("Unsupported native pad endpoint" if pns_model else "Geometric endpoint search requires unoffset SMD or standard plated through-hole pads")
    if any(p["net"] != task["target_net"] for p in endpoints):
        raise ValueError("Geometric endpoint net mismatch")
    return {"native_version": pcbnew.Version(), "task": task,
            "endpoints": endpoints, "primitives": primitives, "outline_edges": edges,
            "outline_shapes": outline_shapes, "outline_polygons": outline_polygons,
            "outline_precision": outline_precision, "outline_edge_envelopes": edge_envelopes,
            "vias": vias, "copper_layer_count": board.GetCopperLayerCount(),
            "copper_layers": [board.GetStandardLayerName(l) for l in board.GetEnabledLayers().CuStack()],
            "native_layer_identity": copper_layer_identity(board),
            "minimum_clearance_mm": max(design.m_MinClearance / 1e6,custom['search_minimum_mm']),
            "custom_clearance_projection": custom,
            "minimum_track_width_mm": design.m_TrackMinWidth / 1e6,
            "hole_clearance_mm": design.m_HoleClearance / 1e6,
            "hole_to_hole_clearance_mm": design.m_HoleToHoleMin / 1e6,
            "via_rules": dict(diameter_mm=max(settings.GetEffectiveNetClass(task["target_net"]).GetViaDiameter(), design.m_ViasMinSize, max(settings.GetEffectiveNetClass(task["target_net"]).GetViaDrill(),design.m_MinThroughDrill)+2*design.m_ViasMinAnnularWidth)/1e6,
                              drill_mm=max(settings.GetEffectiveNetClass(task["target_net"]).GetViaDrill(),design.m_MinThroughDrill)/1e6),
            "edge_clearance_mm": design.m_CopperEdgeClearance / 1e6,
            "refilled_geometry_sha256": refilled_sha256 if refilled_board else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pns-model", action="store_true")
    parser.add_argument("--multilayer-model", action="store_true")
    parser.add_argument("--refilled-board", type=Path)
    parser.add_argument("--refilled-sha256")
    args = parser.parse_args()
    try:
        task = json.loads(args.task.read_text())
        model = snapshot(args.board, task, args.refilled_board, args.refilled_sha256, args.pns_model)
        if args.pns_model:
            layers = model['copper_layers']
            if not supported_stack(layers):
                raise ValueError('PNS witnessed stack supports standard 2/4/6/8 copper layers')
            model['auxiliary_layers'] = {layer:snapshot(args.board,dict(task,allowed_layer=layer),
                args.refilled_board,args.refilled_sha256,True,False) for layer in layers if layer != task['allowed_layer']}
        if args.multilayer_model:
            from pcb_local.task_policy import permissions
            policy=permissions(task,model['copper_layers'])
            if task.get('modification'):
                raise ValueError('Multilayer geometry requires additive routing')
            if task.get('zone_refill'):
                from pcb_local.zone_refill import layer_contract
                layer_contract(task)
            if not supported_stack(model['copper_layers']):
                raise ValueError('Multilayer geometry requires a witnessed 2/4/6/8-layer stack')
            model['routing_models']={l: (model.copy() if l==task['allowed_layer'] else
                snapshot(args.board,dict(task,allowed_layer=l),args.refilled_board,args.refilled_sha256,False,False)) for l in policy['route_layers']}
            model['via_models']={l:snapshot(args.board,dict(task,allowed_layer=l),args.refilled_board,args.refilled_sha256,False,False,True) for l in model['copper_layers']}
        result = {"status": "ready", "model": model}
    except Exception as exc:
        result = {"status": "unsupported", "reason": type(exc).__name__ + ": " + str(exc), "unsupported_items": getattr(exc,"items",[])}
        if hasattr(exc,"outline_diagnostics_raw"):result["outline_diagnostics_raw"]=exc.outline_diagnostics_raw
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "model"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # Native SWIG/wx teardown can stall; this worker never writes boards.
