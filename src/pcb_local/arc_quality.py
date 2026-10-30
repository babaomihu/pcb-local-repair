"""Bounded tangent/arc proposals around real round obstacles; never acceptance."""
import math
from pcb_local.arc_geometry import contained,sample

def propose(points,width,model,check=lambda:None):
    from shapely.geometry import LineString
    from pcb_local.layered_quality import _changed_edges_witness
    from pcb_local.geometry_search import SAFETY_MM
    if not 5<=len(points)<=512:return None
    old_length=sum(math.dist(a,b) for a,b in zip(points,points[1:]));best=None;checked=0
    witness=_changed_edges_witness(model,width,check)
    clearance=max(model['minimum_clearance_mm'],*(e['clearance_mm'] for e in model['endpoints']))
    obstacles=[]
    for p in model['primitives']:
        check()
        if p['net']==model['task']['target_net'] and not p.get('blocks_same_net'):continue
        if p['kind']=='circle' and len(set(p['size']))==1:circles=[(p['center'],p['size'][0]/2)]
        elif p['kind']=='segment':circles=[(p['start'],p['width_mm']/2),(p['end'],p['width_mm']/2)]
        else:continue
        for center,radius in circles:
            if len(obstacles)>=64:break
            # Extra 10 nm covers quantization and the 2 nm chord envelope;
            # original 10 nm clearance safety remains in the final witness.
            obstacles.append((center,radius+width/2+max(clearance,p['clearance_mm'])+2*SAFETY_MM,p['uuid']))
    def tangent(p,c,r):
        dx,dy=p[0]-c[0],p[1]-c[1];dist=math.hypot(dx,dy)
        if dist<=r:return []
        alpha=math.atan2(dy,dx);delta=math.acos(r/dist)
        return [[c[0]+r*math.cos(alpha+s*delta),c[1]+r*math.sin(alpha+s*delta)] for s in (-1,1)]
    a,b=points[0],points[-1]
    for center,radius,uid in obstacles:
        for p in tangent(a,center,radius):
            for q in tangent(b,center,radius):
                for sense in (-1,1):
                    check();checked+=1
                    pa=math.atan2(p[1]-center[1],p[0]-center[0]);qa=math.atan2(q[1]-center[1],q[0]-center[0]);sweep=(sense*(qa-pa))%(2*math.pi)
                    if not math.pi/12<=sweep<=11*math.pi/6:continue
                    mid=[center[0]+radius*math.cos(pa+sense*sweep/2),center[1]+radius*math.sin(pa+sense*sweep/2)]
                    p0,mid0,q0=[[round(v,6) for v in xy] for xy in (p,mid,q)]
                    if p0==a or q0==b:continue
                    try:
                        inside,g=contained(p0,mid0,q0,width,model['task']['region_mm'])
                        if not inside:continue
                        length=math.dist(a,p0)+g['length_mm']+math.dist(q0,b)
                        if length>=old_length-1e-9 or best and length>=best['length_mm']:continue
                        arc,_=sample(p0,mid0,q0)
                    except ValueError:continue
                    path=LineString([a,*arc,b])
                    if not path.is_simple:continue
                    # Buffer with an outward-corrected envelope: every point
                    # of the exact saved arc is within 2 nm of these chords.
                    envelope=path.buffer(.000002/math.cos(math.pi/32),quad_segs=8)
                    how=witness(envelope)
                    if not how:continue
                    best=dict(start=list(a),tangent_start=p0,mid=mid0,tangent_end=q0,end=list(b),
                        length_mm=length,before_length_mm=old_length,obstacle_uuid=uid,witness=how,
                        saved_arc_sweep_radians=g['sweep'],saved_arc_radius_mm=g['radius'],sample_points=len(arc))
    if best:best['candidates_checked']=checked
    return best
