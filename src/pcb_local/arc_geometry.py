"""Exact three-point circle and conservative whole-circle copper scope proof."""
from decimal import Decimal
from fractions import Fraction
import math

def nm(value):
    x=Decimal(str(value))*1000000
    if not x.is_finite() or x!=x.to_integral_value():raise ValueError('Arc requires finite integer-nanometre coordinates')
    return int(x)

def circle(start,mid,end):
    a,b,c=[tuple(nm(v) for v in p) for p in (start,mid,end)]
    if any(len(p)!=2 for p in (a,b,c)) or len({a,b,c})!=3:raise ValueError('Arc needs three distinct 2D points')
    ax,ay=a;bx,by=b;cx,cy=c
    det=2*(ax*(by-cy)+bx*(cy-ay)+cx*(ay-by))
    if not det:raise ValueError('Collinear arc points')
    aa=ax*ax+ay*ay;bb=bx*bx+by*by;cc=cx*cx+cy*cy
    x=Fraction(aa*(by-cy)+bb*(cy-ay)+cc*(ay-by),det)
    y=Fraction(aa*(cx-bx)+bb*(ax-cx)+cc*(bx-ax),det)
    r2=(x-ax)**2+(y-ay)**2
    radius=math.sqrt(float(r2))/1e6;center=[float(x)/1e6,float(y)/1e6]
    angles=[math.atan2(float(py-y),float(px-x)) for px,py in (a,b,c)]
    sense=1 if det>0 else -1;sweep=(sense*(angles[2]-angles[0]))%(2*math.pi)
    if not 0<sweep<2*math.pi or not math.isfinite(radius):raise ValueError('Invalid arc sweep/radius')
    return dict(center_nm=(x,y),radius_squared_nm=r2,center=center,radius=radius,start_angle=angles[0],sense=sense,sweep=sweep,length_mm=radius*sweep)

def contained(start,mid,end,width,region):
    """Entire circle plus trace half-width inside ROI, using rational comparisons.

This deliberately rejects some valid partial arcs near a boundary. Checking
only the three stored points or the chord would not protect the swept copper.
    """
    g=circle(start,mid,end);w=nm(width)
    if w<=0:raise ValueError('Invalid arc width')
    bounds=[Fraction(Decimal(str(v)))*1000000 for v in region]
    x,y=g['center_nm'];half=Fraction(w,2)
    distances=[x-bounds[0]-half,y-bounds[1]-half,bounds[2]-x-half,bounds[3]-y-half]
    return all(d>=0 and d*d>=g['radius_squared_nm'] for d in distances),g

def sample(start,mid,end,error_mm=.000002,max_points=2048):
    """Chord envelope deviates by at most error_mm from the actual saved arc."""
    g=circle(start,mid,end)
    step=2*math.acos(max(-1.,1-error_mm/g['radius']))
    if not step:raise ValueError('Arc sampling would exceed precision budget')
    count=max(2,math.ceil(g['sweep']/step))
    if count+1>max_points:raise ValueError('Arc exceeds bounded envelope point count')
    points=[]
    for i in range(count+1):
        angle=g['start_angle']+g['sense']*g['sweep']*i/count
        points.append([g['center'][0]+g['radius']*math.cos(angle),g['center'][1]+g['radius']*math.sin(angle)])
    points[0]=list(map(float,start));points[-1]=list(map(float,end))
    return points,g
