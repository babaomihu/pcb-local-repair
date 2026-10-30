#!/usr/bin/env python3
"""Read a refilled board with KiCad's bundled Python; never save the board.

The output partitions PAD/track/via/arc UUIDs using KiCad connectivity. Zone
UUIDs are deliberately not union keys: one zone can contain isolated islands.
"""
import argparse
import json
import os
import re
import shutil
import sys
import traceback
import uuid
from pathlib import Path


def compile_rules_witness(path, output, pcbnew):
    """Compile all authored rules then stop at an intentional final sentinel.

    10.0.4 compileRules iterates in file order and immediately throws on an
    invalid expression (drc_engine.cpp:717-728). WriteDRCReport exposes that
    failure, unlike LoadBoard. A separate project copy keeps the measurement
    rules unchanged and prevents entering WriteDRCReport's GUI-dependent DRC.
    """
    path, output = Path(path), Path(output)
    if str(pcbnew.Version()) != "10.0.4":
        raise RuntimeError("Rule compilation witness is only verified for KiCad 10.0.4")
    folder = output.parent / "rule-compile-probe"
    folder.mkdir(exist_ok=False)
    probe = folder / "board.kicad_pcb"
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copy2(path.with_suffix(suffix), probe.with_suffix(suffix))
    token = "__PCB_LOCAL_COMPILE_END_" + uuid.uuid4().hex
    original_rules = probe.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    if token in original_rules:
        raise RuntimeError("Compilation sentinel collision")
    probe.with_suffix(".kicad_dru").write_text(original_rules + '\n(rule "' + token +
        '" (condition "A.__pcb_local_invalid_field") (constraint clearance (min 0mm)))\n', encoding="utf-8")
    saved_stderr = os.dup(2)
    returned = None
    try:
        with output.open("wb") as log:
            sys.stderr.flush()
            os.dup2(log.fileno(), 2)
            probe_board = pcbnew.LoadBoard(str(probe))
            if probe_board is None:
                raise RuntimeError("Rule compilation probe could not load the board")
            returned = bool(pcbnew.WriteDRCReport(probe_board, str(folder / "unexpected.rpt"), pcbnew.EDA_UNITS_MM, True))
    finally:
        sys.stderr.flush()
        os.dup2(saved_stderr, 2)
        os.close(saved_stderr)
    diagnostic = output.read_text(encoding="utf-8", errors="replace")
    expected = "Init DRC engine: err <Parse error in '" + token + "', line 0, offset 0.>"
    errors = [line.strip() for line in diagnostic.splitlines()
              if re.search(r"\b(?:error|failed|assertion|exception)\b|错误|錯誤|失败|失敗", line, re.I)]
    # Mere false, token occurrence, or parser failure is insufficient. This
    # exact line is emitted for the sentinel's compileRules semantic error.
    okay = returned is False and errors == [expected]
    evidence = {"checked": True, "successful": okay,
                "method": "KiCad 10.0.4 sequential compilation reached an intentional final sentinel in an isolated copy",
                "sentinel": token, "expected_diagnostic": expected,
                "error_diagnostics": errors, "diagnostic": str(output), "probe": str(folder)}
    output.with_suffix(".json").write_text(json.dumps(evidence, indent=2) + "\n")
    if not okay:
        raise RuntimeError("Native authored-rule compilation did not reach its terminal sentinel; see " + str(output))
    return evidence


def snapshot(path, rule_check_output=None):
    import wx
    app = wx.App(False)  # KiCad's Python library expects application traits.
    import pcbnew
    rule_compilation = {"checked": False, "successful": None, "method": None}
    if rule_check_output is not None:
        rule_compilation = compile_rules_witness(path, rule_check_output, pcbnew)
    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise RuntimeError("KiCad rejected the board")

    def mm(position):
        return [position.x / 1e6, position.y / 1e6]

    enabled = set(board.GetEnabledLayers().CuStack())

    def layers(obj):
        return [str(board.GetStandardLayerName(layer)) for layer in obj.GetLayerSet().CuStack()
                if layer in enabled]

    def parameters(obj):
        # BOARD_CONNECTED_ITEM.GetEffectiveNetClass exposes a bare NETCLASS*
        # in KiCad 10.0.4 SWIG. NET_SETTINGS returns a usable shared_ptr proxy.
        nc = board.GetDesignSettings().m_NetSettings.GetEffectiveNetClass(obj.GetNetname())
        members = sorted(str(key) for key in board.GetAllNetClasses()
                         if nc.ContainsNetclassWithName(key))
        return {"name": str(nc.GetName()), "track_width_mm": nc.GetTrackWidth() / 1e6,
                "clearance_mm": nc.GetClearance() / 1e6,
                "members": members or [str(nc.GetName())]}

    def metadata(obj, parent=None):
        return {"position_mm": mm(obj.GetPosition()), "copper_layers": layers(obj),
                "locked": bool(obj.IsLocked() or (parent is not None and parent.IsLocked())),
                "netclass": parameters(obj),
                "own_clearance_mm": max([0]+[obj.GetOwnClearance(l) for l in obj.GetLayerSet().CuStack() if l in enabled])/1e6}

    items = {}
    reference_value_fields = []
    objects = {}
    native_keys = {}
    for footprint in board.GetFootprints():
        for field_name, native_field in (("Reference", footprint.Reference()), ("Value", footprint.Value())):
            reference_value_fields.append({"footprint_uuid": str(footprint.m_Uuid.AsString()),
                "field_name": field_name, "uuid": str(native_field.m_Uuid.AsString()),
                "position_mm": mm(native_field.GetPosition()), "text": str(native_field.GetText())})
        for pad in footprint.Pads():
            uuid = str(pad.m_Uuid.AsString())
            position = pad.GetPosition()
            uid = "pad:{}:{}:{}:{}:{}".format(footprint.m_Uuid.AsString(), uuid,
                  pad.GetNumber(), position.x, position.y)
            if uid in objects:
                raise ValueError("Indistinguishable composite pad identity: " + uid)
            objects[uid] = pad
            native_keys[int(pad.this)] = uid
            items[uid] = {"kind": "pad", "uuid": uuid, "net": str(pad.GetNetname()),
                          "reference": str(footprint.GetReference()),
                          "pin_function": str(pad.GetPinFunction()),
                          "pin_type": str(pad.GetPinType()),
                          "number": str(pad.GetNumber()), "footprint_uuid": str(footprint.m_Uuid.AsString()),
                          **metadata(pad, footprint)}
    kinds = {"PCB_TRACK": "segment", "PCB_VIA": "via", "PCB_ARC": "arc"}
    for track in board.GetTracks():
        uid = str(track.m_Uuid.AsString())
        if uid in objects:
            raise ValueError("Duplicate item UUID: " + uid)
        kind = kinds.get(str(track.GetClass()))
        if kind is None:
            raise ValueError("Unsupported conductive object: " + track.GetClass())
        objects[uid] = track
        native_keys[int(track.this)] = uid
        items[uid] = {"kind": kind, "uuid": uid, "net": str(track.GetNetname()), **metadata(track)}
        if kind in {"segment", "arc"}:
            items[uid].update(start_mm=mm(track.GetStart()), end_mm=mm(track.GetEnd()),
                              width_mm=track.GetWidth() / 1e6)
        if kind == 'arc':
            items[uid]['mid_mm'] = mm(track.GetMid())
        if kind == 'via':
            items[uid]['standard_through'] = (track.GetViaType()==pcbnew.VIATYPE_THROUGH
                and track.Padstack().Mode()==pcbnew.PADSTACK.MODE_NORMAL
                and not track.GetRemoveUnconnected() and not track.GetIsFree()
                and not any(v is not None and (v.x or v.y) for v in (track.GetSecondaryDrillSize(),track.GetTertiaryDrillSize())))
    if not items:
        raise ValueError("Board contains no pads or routing items")
    if not board.BuildConnectivity():
        raise RuntimeError("KiCad BuildConnectivity failed")
    changed_nets = [uid for uid, obj in objects.items()
                    if str(obj.GetNetname()) != items[uid]["net"]]
    if changed_nets:
        raise RuntimeError("BuildConnectivity changed stored net assignment: "
                           + ", ".join(changed_nets[:10]))
    connection = board.GetConnectivity()
    components, assigned = [], {}
    for uid, obj in objects.items():
        if uid in assigned:
            continue
        members = {uid}
        for other in connection.GetConnectedItems(obj):
            other_id = native_keys.get(int(other.this))
            if other_id in items:
                if str(other.GetNetname()) != items[uid]["net"]:
                    raise ValueError("KiCad component includes different net assignments")
                members.add(other_id)
        overlaps = members.intersection(assigned)
        if overlaps:
            raise ValueError("Asymmetric/overlapping native connectivity components")
        index = len(components)
        for member in members:
            assigned[member] = index
        components.append({"net": items[uid]["net"], "items": sorted(members),
                           "pads": sorted(x for x in members if items[x]["kind"] == "pad")})
    settings = board.GetDesignSettings()
    minima = {key: getattr(settings, field) / 1e6 for key, field in (
        ('clearance', 'm_MinClearance'), ('track_width', 'm_TrackMinWidth'),
        ('board_edge_clearance', 'm_CopperEdgeClearance'),
        ('hole_clearance', 'm_HoleClearance'), ('hole_to_hole_clearance', 'm_HoleToHoleMin'),
        ('via_diameter', 'm_ViasMinSize'), ('via_drill', 'm_MinThroughDrill'),
        ('via_annular_width', 'm_ViasMinAnnularWidth'))}
    net_rules = {}
    for net_name in sorted(set(str(n) for n in board.GetNetsByName()) | {''}):
        nc = settings.m_NetSettings.GetEffectiveNetClass(net_name)
        net_rules[net_name] = {key: getattr(nc, method)() / 1e6 for key, method in (
            ('clearance', 'GetClearance'), ('track_width', 'GetTrackWidth'),
            ('via_diameter', 'GetViaDiameter'), ('via_drill', 'GetViaDrill'))}
    zone_choices = []
    for z in board.Zones():
        if z.GetIsRuleArea() or z.IsLocked() or not str(z.GetNetname()):continue
        bounds=z.GetBoundingBox()
        zone_choices.append(dict(uuid=str(z.m_Uuid.AsString()), net=str(z.GetNetname()),
            local_clearance_mm=max(0,z.GetLocalClearance() or 0)/1e6, min_thickness_mm=z.GetMinThickness()/1e6,
            layers=[str(board.GetStandardLayerName(l)) for l in z.GetLayerSet().CuStack() if board.IsLayerEnabled(l)],
            bounds_mm=[bounds.GetLeft()/1e6,bounds.GetTop()/1e6,bounds.GetRight()/1e6,bounds.GetBottom()/1e6]))
    return {"version": str(pcbnew.Version()), "items": items,
            "zone_refill_choices": zone_choices,
            "routing_rules": {"board_minima_mm": minima, "netclasses_mm": net_rules},
            "copper_layers": [str(board.GetStandardLayerName(layer)) for layer in board.GetEnabledLayers().CuStack()],
            "rule_compilation": rule_compilation,
            "reference_value_fields": reference_value_fields,
            "net_tie_footprints": [str(fp.m_Uuid.AsString()) for fp in board.GetFootprints() if fp.IsNetTie()],
            "components": components, "component_of": assigned,
            "limitations": ["Pad-less zone-fill islands are not electrical terminals in this snapshot; zone definitions are protected separately."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--rule-check-output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(snapshot(args.board, args.rule_check_output), ensure_ascii=False, indent=2) + "\n")


def entrypoint():
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    except BaseException:
        traceback.print_exc()
        code = 2
    # Native teardown can stall after both successful work and Python errors.
    # This worker owns no board writes; close JSON/log streams before exit.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    entrypoint()
