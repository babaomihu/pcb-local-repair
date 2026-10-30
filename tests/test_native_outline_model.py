"""Conservative curved-board search and exact PNS edge-identity boundaries."""
import copy,unittest
from test_geometry_search import model
from pcb_local.geometry_search import search
from pcb_local.pns_adapter import outline_identity,geometry_identity
from shapely.geometry import Point,LineString,Polygon,box


def polygon(shape):
    return {'shell':[list(p) for p in list(shape.exterior.coords)[:-1]],'holes':[[list(p) for p in list(r.coords)[:-1]] for r in shape.interiors]}


def curved_model():
    m=model();m['primitives']=[]
    outer=Point(5,5).buffer(5,quad_segs=32);hole=Point(5,5).buffer(.4,quad_segs=32)
    m['outline_polygons']=[polygon(outer.difference(hole))]
    m['outline_shapes']=[dict(kind='circle',center=[5,5],end=[10,5],width_mm=.05),dict(kind='circle',center=[5,5],end=[5.4,5],width_mm=.05)]
    m['outline_precision']=dict(max_error_mm=.005,chaining_epsilon_mm=.01,edge_envelope_error_mm=.0001)
    m['outline_edge_envelopes']=[{'polygons':[polygon(outer.boundary.buffer(.025))]},{'polygons':[polygon(hole.boundary.buffer(.025))]}]
    return m


class NativeOutlineModel(unittest.TestCase):
    def test_circle_hole_keeps_conservative_distance(self):
        m=curved_model();r=search(m);self.assertEqual(r['status'],'found')
        self.assertGreater(LineString(r['path_mm']).distance(Point(5,5)),.4+.2+.125+.015)
        self.assertAlmostEqual(r['outline_uncertainty_margin_mm'],.015102)

    def test_uncertain_boundary_endpoint_is_not_accepted(self):
        m=curved_model();m['endpoints'][0]['center']=[.33,5]
        self.assertEqual(search(m)['status'],'no_path')

    def test_inner_hole_not_ignored_when_straight_legacy_edges_unchanged(self):
        m=curved_model();m['endpoints'][0]['center']=[5,5]
        self.assertEqual(search(m)['status'],'no_path')

    def test_unknown_or_missing_precision_refused(self):
        for value in [-1,float('nan'),float('inf')]:
            m=curved_model();m['outline_precision']['max_error_mm']=value
            with self.assertRaises(ValueError):search(m)
        m=curved_model();del m['outline_edge_envelopes']
        with self.assertRaises(ValueError):search(m)

    def test_disjoint_material_is_not_a_rectangle(self):
        m=curved_model();m['outline_polygons']=[polygon(box(0,0,4,10)),polygon(box(6,0,10,10))]
        self.assertEqual(search(m)['status'],'no_path')

    def test_shape_identity_is_world_geometry_not_owner(self):
        a=curved_model();b=copy.deepcopy(a)
        for e in b['outline_shapes']:e.update(owner_uuid='changed',source_uuid='changed')
        self.assertEqual(outline_identity(a),outline_identity(b))
        b['outline_shapes'][1]['end'][0]+=.1
        self.assertNotEqual(outline_identity(a),outline_identity(b))

    def test_arc_midpoint_and_stroke_are_witnessed(self):
        a=curved_model();a['outline_shapes']=[dict(kind='arc',start=[0,0],mid=[1,1],end=[2,0],width_mm=.05)]
        for key in ('mid','width_mm'):
            b=copy.deepcopy(a)
            if key=='mid':b['outline_shapes'][0][key]=[1,2]
            else:b['outline_shapes'][0][key]=.1
            self.assertNotEqual(outline_identity(a),outline_identity(b))
        b=copy.deepcopy(a);b['outline_shapes'][0]['start'],b['outline_shapes'][0]['end']=b['outline_shapes'][0]['end'],b['outline_shapes'][0]['start']
        self.assertEqual(outline_identity(a),outline_identity(b))

if __name__=='__main__':unittest.main()
