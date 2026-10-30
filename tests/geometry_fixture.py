"""CC0 mechanism fixtures: varied transforms/constraints; no reference paths."""
import json
import math
from pathlib import Path
import uuid
from pcb_local.validation import CRITICAL_DRC_CHECKS


def create(folder, *, angle=0, offset=(0,0), width=.25, clearance=.2, gap=.653,
           gates=((10,14.713),(18,15.289)), rectangle=False, name='signal', wall=False):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    def uid(label):return str(uuid.uuid5(uuid.NAMESPACE_URL,str(folder)+label))
    a=math.radians(angle)
    def point(p):
        x,y=p;return [round(x*math.cos(a)-y*math.sin(a)+offset[0],6),round(x*math.sin(a)+y*math.cos(a)+offset[1],6)]
    def coords(p):return '%.6f %.6f'%tuple(point(p))
    netnames={1:name};bars=[]
    for i,(x,y) in enumerate(gates):
        if wall:ends=[((x,2.4),(x,27.6))]
        else:ends=[((x,2.4),(x,y-(gap+.3)/2)),((x,y+(gap+.3)/2),(x,27.6))]
        for ends_ in ends:
            n=len(netnames)+1;netnames[n]='foreign_'+str(n);bars.append((n,ends_))
    text=['(kicad_pcb (version 20241229) (generator "geometry_mechanism_tests") (general (thickness 1.6))',
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (35 "F.Paste" user) (37 "F.SilkS" user) (39 "F.Mask" user) (44 "Edge.Cuts" user) (49 "F.Fab" user))',
        '(setup (pad_to_mask_clearance 0)) (net 0 "")']
    text += ['(net %d %s)'%(n,json.dumps(s)) for n,s in netnames.items()]
    corners=[(2,2),(28,2),(28,28),(2,28),(2,2)]
    for i,(p,q) in enumerate(zip(corners,corners[1:])):
        text.append('(gr_line (start %s) (end %s) (stroke (width .05) (type default)) (layer "Edge.Cuts") (uuid "%s"))'%(coords(p),coords(q),uid('edge'+str(i))))
    def pad(label,p,n,size=(.3,.3),rect=False,rotation=0,own=0):
        return '(footprint "Mechanism:Pad" (layer "F.Cu") (uuid "%s") (at %s) (attr smd) (property "Reference" "%s" (at 0 0) (layer "F.SilkS") (hide yes) (effects (font (size .6 .6) (thickness .1)))) (property "Value" "Pad" (at 0 0) (layer "F.Fab") (hide yes) (effects (font (size .6 .6) (thickness .1)))) (pad "1" smd %s (at 0 0 %.6f) (size %g %g) (layers "F.Cu" "F.Paste" "F.Mask") (net %d %s) (clearance %g) (uuid "%s")))'%(uid(label+'fp'),coords(p),label,'rect' if rect else 'circle',rotation-angle,*size,n,json.dumps(netnames[n]),own,uid(label+'pad'))
    text += [pad('A',(6,15),1,(1,1.4) if rectangle else (1,1),rectangle,31),pad('Z',(24,15),1,(1,1.4) if rectangle else (1,1),rectangle,31)]
    for n,ends in bars:
        text += [pad('B%d_%d'%(n,j),p,n) for j,p in enumerate(ends)]
        text.append('(segment (start %s) (end %s) (width .3) (layer "F.Cu") (net %d) (uuid "%s") (locked yes))'%(coords(ends[0]),coords(ends[1]),n,uid('track'+str(n))))
    if rectangle:
        # Independent rotated foreign rectangle and local clearance override.
        text += [pad('RECT',(8,14.7),2,(.6,1.3),True,37,.28)]
    board=folder/'board.kicad_pcb';board.write_text('\n'.join(text)+'\n)\n')
    project={'meta':{'version':1},'board':{'design_settings':{'rules':{'min_clearance':clearance,'min_track_width':width,'min_copper_edge_clearance':.2},'rule_severities':{k:'error' for k in sorted(CRITICAL_DRC_CHECKS)}}},'net_settings':{'classes':[{'name':'Default','clearance':clearance,'track_width':width,'via_diameter':.6,'via_drill':.3,'diff_pair_width':width,'diff_pair_gap':clearance}]}}
    board.with_suffix('.kicad_pro').write_text(json.dumps(project,indent=2)+'\n')
    roi=[point(p) for p in [(4,12),(26,12),(26,18),(4,18)]]
    task=dict(id=folder.name,target_net=name,target_pad_uuids=[uid('Apad'),uid('Zpad')],allowed_layer='F.Cu',region_mm=[min(p[0] for p in roi),min(p[1] for p in roi),max(p[0] for p in roi),max(p[1] for p in roi)])
    return board,task
