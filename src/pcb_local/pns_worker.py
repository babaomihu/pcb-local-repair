#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated PCBWorld/PNS proposal worker. Parent owns timeout and acceptance.

No native KiCad module is imported into the CLI/plugin process. This worker
never changes rules and never accepts its own routing result.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import subprocess
import traceback

try:
    from .native_layers import copper_layer_id
    from .layer_stack import native_copper_id
    from .pns_progress import observe, summarize
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pcb_local.native_layers import copper_layer_id
    from pcb_local.layer_stack import native_copper_id
    from pcb_local.pns_progress import observe, summarize


def identity_seed(board):
    """Deterministic KIID stream per frozen problem, not a global seed reset.

    Reusing seed zero on a board containing a prior repair reuses that repair's
    UUID stream. Unique object identity is still independently checked on every
    saved proposal; this seed is not a substitute for collision detection.
    """
    return int.from_bytes(hashlib.sha256(Path(board).read_bytes()).digest()[:4], 'big') & 0x7fffffff


def cursor_positions(start, end, waypoints, count):
    """Spend exactly the existing move quota along one piecewise linear guide."""
    path=[start,*waypoints,end]
    lengths=[math.dist(a,b) for a,b in zip(path,path[1:])]
    total=sum(lengths)
    for step in range(1,count+1):
        if step==count:
            yield list(end)
            continue
        distance=total*step/count
        for segment,(a,b) in enumerate(zip(path,path[1:])):
            if distance <= lengths[segment] or segment==len(lengths)-1:
                fraction=min(1,distance/lengths[segment]) if lengths[segment] else 1
                yield [a[j]+(b[j]-a[j])*fraction for j in (0,1)]
                break
            distance-=lengths[segment]


def native_pad_witness(board_path, layer_name):
    """Read actual engine-saved pad geometry in an isolated native process."""
    import wx
    import pcbnew
    app = wx.App(False)
    board = pcbnew.LoadBoard(str(board_path))
    layer = copper_layer_id(board, layer_name)
    point = lambda p: [p.x / 1e6, p.y / 1e6]
    return [dict(uuid=p.m_Uuid.AsString(), reference=p.GetParentFootprint().GetReference(),
                 net=p.GetNetname(), anchor=point(p.GetPosition()),
                 shape_center=point(p.ShapePos(layer)), layer=layer_name)
            for p in board.GetPads() if p.IsOnLayer(layer)]


def endpoint_net_codes(router, request, layer, pad_witness=None):
    """Bind original pads, line ends and full-stack vias in the loaded engine.

    No synthetic copper or relaxed arrival tolerance. The native source binding
    is checked again after projection/loading; endpoints remain fixed objects.
    """
    pads = router.get_pads()
    bindings = request.get('endpoint_bindings')
    anchors = request.get('endpoint_anchors', request['endpoints'])
    if len(anchors) != 2 or len(request['endpoints']) != 2 or (bindings is not None and len(bindings) != 2):
        raise ValueError('PNS requires exactly two bound endpoints')
    def same(a, b):
        return len(a) == len(b) == 2 and all(math.isfinite(v) for v in [*a, *b]) and all(round(x*1e6) == round(y*1e6) for x,y in zip(a,b))
    codes=[]
    identities=[]
    for index, anchor in enumerate(anchors):
        binding = bindings[index] if bindings else None
        kind = binding['kind'] if binding else 'pad'
        uid = binding['uuid'] if binding else request.get('endpoint_refs', [None,None])[index]
        if binding and (binding['net'] != request['target_net'] or binding['layer'] != request['allowed_layer'] or not same(binding['position_mm'], anchor)):
            raise ValueError('PNS endpoint binding disagrees with requested coordinates/net/layer')
        if kind == 'pad':
            if binding and binding['selector'] != 'center':
                raise ValueError('Unsupported PNS pad endpoint selector')
            matches=[p for p in pads if p.net_name == request['target_net']
                     and (p.footprint_ref == uid if uid else same([p.x_mm,p.y_mm], anchor))
                     and p.layer in (layer,-2)]
            if len(matches) != 1 or not same([matches[0].x_mm,matches[0].y_mm], anchor):
                raise ValueError('PNS pad endpoint identity/position/layer is ambiguous')
            # PadInfo exposes GetPosition(), not ShapePos()/offset. Read the
            # latter from the engine's saved board, never from request fields.
            actual = [p for p in (pad_witness or [])
                      if p['reference'] == matches[0].footprint_ref
                      and (not uid or p['uuid'] == uid)
                      and p['net'] == request['target_net']
                      and p['layer'] == request['allowed_layer']
                      and same(p['anchor'], anchor)]
            if len(actual) != 1 or not same(actual[0]['shape_center'], request['endpoints'][index]):
                raise ValueError('PNS pad route coordinate differs from native loaded geometry')
            identities.append(('pad', actual[0]['uuid']))
        elif kind == 'segment' and binding['selector'] in ('start','end'):
            matches=[t for t in router.get_tracks() if t.uuid == uid and t.net_name == request['target_net'] and t.layer == layer]
            if len(matches) != 1:
                raise ValueError('PNS segment endpoint identity/net/layer is ambiguous')
            t=matches[0]
            position=[t.x1_mm,t.y1_mm] if binding['selector']=='start' else [t.x2_mm,t.y2_mm]
            if not same(position,anchor) or not same(position,request['endpoints'][index]):
                raise ValueError('PNS segment endpoint moved or changed orientation')
            identities.append(('segment', t.uuid))
        elif kind == 'via' and binding['selector'] == 'center':
            matches=[v for v in router.get_vias() if v.uuid == uid and v.net_name == request['target_net']]
            if len(matches) != 1:
                raise ValueError('PNS via endpoint identity/net is ambiguous')
            v=matches[0]
            if (v.top_layer != native_copper_id('F.Cu') or v.bottom_layer != native_copper_id('B.Cu')
                    or not same([v.x_mm,v.y_mm],anchor) or not same(anchor,request['endpoints'][index])):
                raise ValueError('PNS via endpoint span or position changed')
            identities.append(('via', v.uuid))
        else:
            raise ValueError('Unsupported PNS endpoint kind/selector')
        codes.append(matches[0].net_code)
    if len(set(codes)) != 1:
        raise ValueError('PNS endpoints disagree on net identity')
    if len(set(identities)) != 2:
        raise ValueError('PNS requires two distinct endpoint objects')
    return codes


def main():
    if '--native-pad-witness' in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument('--native-pad-witness', type=Path, required=True)
        parser.add_argument('--layer', required=True)
        parser.add_argument('--output', type=Path, required=True)
        args = parser.parse_args()
        args.output.write_text(json.dumps(native_pad_witness(args.native_pad_witness, args.layer))+'\n')
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--module-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--kicad-python", type=Path, required=True)
    args = parser.parse_args(); args.out_dir.mkdir(parents=True, exist_ok=False)
    request = json.loads(args.request.read_text())
    report = {"status": "error", "attempts": [], "requires_native_validation": True}
    output = args.out_dir / "proposal.json"
    def save():
        output.write_text(json.dumps(report, indent=2) + "\n")
    try:
        sys.path.insert(0, str(args.module_dir))
        import kicad_rl_router as krl
        report['engine_seed'] = identity_seed(request['board'])
        report['engine_seed_policy'] = 'sha256-frozen-problem-first31bits; deterministic KIID stream, may affect UUID-based PNS obstacle ordering'
        router = krl.RLRouter(request["board"], request["project"], report['engine_seed'], 250, 1000000)
        if not router.was_project_loaded_from_file():
            raise ValueError("PNS did not load the supplied project rules")
        report["project_loaded"] = True
        rules = router.get_design_rules()
        report["global_minima"] = {k:getattr(rules,k) for k in request.get("global_minima",{})}
        if any(abs(report["global_minima"][k]-v)>1e-8 for k,v in request.get("global_minima",{}).items()):
            raise ValueError("PNS/native board minima mismatch")
        router.set_routing_mode(request.get("routing_mode", 1))
        router.set_corner_mode(0)
        def tracks():
            return sorted((t.uuid, t.net_name, t.layer, t.x1_mm, t.y1_mm, t.x2_mm, t.y2_mm, t.width_mm)
                          for t in router.get_tracks())
        def vias():
            return sorted((v.uuid, v.net_name, v.x_mm, v.y_mm, v.diameter_mm,
                           v.drill_mm, v.top_layer, v.bottom_layer) for v in router.get_vias())
        initial = tracks()
        initial_vias = vias()
        report['initial_vias'] = initial_vias
        report["initial_tracks"] = initial
        baseline = args.out_dir / "loaded.kicad_pcb"
        router.save(str(baseline))
        report["loaded_board"] = str(baseline)
        pads = router.get_pads()
        ends = request["endpoints"]
        # Native PCB_LAYER_ID in the pinned engine's include/layer_ids.h,
        # not the serialized .kicad_pcb B.Cu token (31). A spanning pad is -2.
        layer = native_copper_id(request.get('allowed_layer', 'F.Cu'))
        witness_path = args.out_dir / 'loaded-pad-witness.json'
        # Inherits the outer job's process group; cancellation/shared budget
        # kills this child too. The additional native launch has a 10 s cap
        # and never extends the parent's existing generation deadline.
        subprocess.run([str(args.kicad_python), str(Path(__file__).resolve()),
            '--native-pad-witness', str(baseline), '--layer', request['allowed_layer'],
            '--output', str(witness_path)], check=True, timeout=10)
        endpoint_codes = endpoint_net_codes(router, request, layer, json.loads(witness_path.read_text()))
        report['native_pad_witness'] = str(witness_path)
        report['endpoint_net_codes'] = endpoint_codes
        report["effective_netclasses"] = {}
        for net, expected in request["netclasses"].items():
            codes = {p.net_code for p in pads if p.net_name == net}
            codes.update(t.net_code for t in router.get_tracks() if t.net_name == net)
            codes.update(v.net_code for v in router.get_vias() if v.net_name == net)
            if len(codes) != 1:
                raise ValueError("PNS net identity is missing/ambiguous: " + net)
            nc = router.get_netclass_for_net(next(iter(codes)))
            values = {key: getattr(nc, key + "_mm") for key in ("clearance", "track_width", "via_diameter", "via_drill")}
            report["effective_netclasses"][net] = values
            if any(abs(values[key] - expected[key]) > 1e-8 for key in values):
                raise ValueError("PNS/native effective netclass mismatch: " + net)
        checkpoint = router.checkpoint()
        if not router.has_checkpoint(checkpoint):
            raise ValueError("PNS checkpoint creation failed")
        save()
        guides={p['policy']:p['waypoints'] for p in request.get('cursor_guidance',{}).get('plans',[])
                if p.get('fixed_space_witness') and p.get('policy') in ('guide-low','guide-high')}
        modes=['direct','guided-incremental' if guides else 'incremental','edge-low','edge-high']
        plans = [(mode, direction, pair) for mode in modes
                 for direction,pair in enumerate((ends,list(reversed(ends))))]
        for i, (mode, direction, (start, end)) in enumerate(plans):
            router.restore(checkpoint)
            if tracks() != initial or vias() != initial_vias:
                raise ValueError("PNS restore did not recover exact initial track geometry/UUIDs")
            attempt = {"direction": direction, 'cursor_policy':mode, "restore_verified": True, "start": start, "end": end, 'progress':[]}
            report["attempts"].append(attempt)
            attempt["started"] = bool(router.start_route(*start, layer))
            if attempt["started"]:
                sizes = list(router.get_route_sizes()); attempt["sizes"] = sizes
                expected = request["netclasses"][request["target_net"]]
                required_width = max(expected["track_width"], request['global_minima']['min_track_width_mm'])
                attempt['required_width_mm'] = required_width
                if abs(sizes[0] - required_width) > 1e-8:
                    raise ValueError("PNS start-route width differs from native class/board minimum")
                if mode.startswith('edge-'):
                    # Task-derived perimeter lanes add a bounded topological
                    # alternative when a straight cursor sweep cannot shove.
                    # No reference routes, board IDs or fixture coordinates.
                    axis = 1 if abs(end[0]-start[0]) >= abs(end[1]-start[1]) else 0
                    region = request['region_mm']; margin=sizes[0]/2+.000001
                    lane = region[axis]+margin if mode=='edge-low' else region[axis+2]-margin
                    waypoints=[list(start),list(end)]
                    for point in waypoints:point[axis]=lane
                    attempt['waypoints'] = waypoints
                    attempt['waypoint_results'] = []
                    attempt['waypoint_result_semantics'] = 'Native FixRoute returns realEnd; false with force_finish=False can mean normal continued routing. Arrival observations, final commit and native acceptance are separate.'
                    for waypoint_index, point in enumerate(waypoints):
                        router.move(*point)
                        attempt['progress'].append(observe(router,point,layer,endpoint_codes[0],'waypoint-%d'%waypoint_index))
                        attempt['waypoint_results'].append(bool(router.fix_route(*point,False,True,layer,0.0,False,False)))
                count = min(64,max(1,math.ceil(math.dist(start,end)/max(.5,sizes[0]+2*expected['clearance'])))) if mode in ('incremental','guided-incremental') else 1
                attempt['cursor_steps'] = count
                waypoints=[]
                if mode=='guided-incremental':
                    # Reallocate the original incremental slot and its exact
                    # move count; no intermediate fixes or extra plans.
                    chosen=sorted(guides)[direction % len(guides)]
                    waypoints=guides[chosen] if direction==0 else list(reversed(guides[chosen]))
                    attempt['guidance_policy']=chosen
                    attempt['waypoints']=waypoints
                for step,xy in enumerate(cursor_positions(start,end,waypoints,count),1):
                    attempt["moved"] = bool(router.move(*xy))
                    attempt['progress'].append(observe(router,xy,layer,endpoint_codes[0],'endpoint' if step==count else 'cursor-%d'%step))
                attempt["committed"] = bool(router.fix_route(*end, True, True, layer, 0.0, False, False))
                if attempt["committed"]:
                    path = args.out_dir / ("candidate-%d.kicad_pcb" % i)
                    router.save(str(path))
                    attempt["candidate_board"] = str(path)
                    attempt["tracks"] = tracks()
            attempt["failure_reason"] = router.get_failure_reason()
            attempt['progress_summary'] = summarize(attempt)
            save()
        router.restore(checkpoint)
        report["final_restore_verified"] = tracks() == initial and vias() == initial_vias
        if not report["final_restore_verified"]:
            raise ValueError("PNS final restore failed")
        restored = args.out_dir / "restored.kicad_pcb"
        router.save(str(restored)); report["restored_board"] = str(restored)
        router.release_checkpoint(checkpoint)
        report["status"] = "proposed" if any(x.get("committed") for x in report["attempts"]) else "no_progress"
        save(); return 0
    except Exception as exc:
        report.update(status="error", error=type(exc).__name__ + ": " + str(exc))
        save(); traceback.print_exc(); return 2


if __name__ == "__main__":
    code = main()
    sys.stdout.flush(); sys.stderr.flush(); os._exit(code)
