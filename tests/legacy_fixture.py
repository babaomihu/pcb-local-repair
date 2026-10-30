"""CC0 small KiCad-5 mechanisms; these are not original engineering successes."""
import json
from pathlib import Path
from pcb_local.validation import CRITICAL_DRC_CHECKS


def create(folder, offset=0, backside=False, conflicting_project=False):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    layer = 'B.Cu' if backside else 'F.Cu'
    text = ['(kicad_pcb (version 20171130) (host pcbnew "5.1.10")',
        '(general (thickness 1.6))',
        '(layers (0 F.Cu signal) (31 B.Cu signal) (35 F.Paste user) (34 B.Paste user) (37 F.SilkS user) (36 B.SilkS user) (39 F.Mask user) (38 B.Mask user) (44 Edge.Cuts user) (49 F.Fab user))',
        '(setup (trace_clearance 0.2) (trace_min 0.25) (via_size 0.6) (via_drill 0.3) (pad_to_mask_clearance 0))',
        '(net 0 "") (net 1 "sensor_data")',
        '(net_class Default "Default" (clearance .2) (trace_width .25) (via_dia .6) (via_drill .3) (uvia_dia .3) (uvia_drill .1) (add_net "sensor_data"))']
    for i,x in enumerate((10,20)):
        text.append('(module "Test:Pad" (layer %s) (tstamp %08X) (at %g 10) (attr smd) (fp_text reference "J%d" (at 0 -2) (layer F.SilkS) (effects (font (size 1 1) (thickness .15)))) (fp_text value "Pad" (at 0 2) (layer F.Fab) (effects (font (size 1 1) (thickness .15)))) (pad 1 smd circle (at 0 0) (size 1 1) (layers %s %s %s) (net 1 "sensor_data")))' % (layer, 0x12345670+i, x+offset, i, layer, 'B.Paste' if backside else 'F.Paste', 'B.Mask' if backside else 'F.Mask'))
    for x,y in ((10,14),(16,20)):
        text.append('(segment (start %g 10) (end %g 10) (width .25) (layer %s) (net 1))' % (x+offset,y+offset,layer))
    corners=[(5,5),(25,5),(25,15),(5,15),(5,5)]
    for a,b in zip(corners,corners[1:]):
        text.append('(gr_line (start %g %g) (end %g %g) (layer Edge.Cuts) (width .05))' % (a[0]+offset,a[1],b[0]+offset,b[1]))
    board=folder/'board.kicad_pcb';board.write_text('\n'.join(text)+'\n)\n')
    # Native KiCad 5 inline defaults must agree with the supplied modern
    # project rules; the conflict case intentionally does not and must fail.
    project={'meta':{'version':1},'board':{'design_settings':{'rules':{'min_track_width':.25}}},'net_settings':{'classes':[{'name':'Default','clearance':.2,'track_width':.25,'via_diameter':.6,'via_drill':.3,'microvia_diameter':.3,'microvia_drill':.1,'diff_pair_width':.2,'diff_pair_gap':.25,'diff_pair_via_gap':.25}]}}
    if conflicting_project:
        project['board']['design_settings']['rules'].update(min_clearance=.2,min_copper_edge_clearance=.2)
        project['board']['design_settings']['rule_severities']={k:'error' for k in sorted(CRITICAL_DRC_CHECKS)}
    board.with_suffix('.kicad_pro').write_text(json.dumps(project,indent=2)+'\n')
    return board
