import copy
import math
import unittest
from unittest.mock import patch
from shapely.geometry import LineString
from pcb_local.geometry_search import search, configuration_space, SearchLimit, boundary_corridor
from test_direct_visibility import complex_model


def attached_model():
    m=complex_model()
    m['primitives'].append(dict(kind='keepout',net='',blocks_same_net=True,clearance_mm=0,
        polygons=[dict(shell=[[14,11],[16,11],[16,16.5],[14,16.5]],holes=[])]))
    return m

def transform(m, angle, mirror=False):
    m=copy.deepcopy(m);a=math.radians(angle)
    def point(p):
        x,y=p[0]-15,p[1]-15
        if mirror:y=-y
        return [x*math.cos(a)-y*math.sin(a)+60,x*math.sin(a)+y*math.cos(a)+70]
    for p in m['primitives']:
        if 'center' in p:p['center']=point(p['center'])
        for poly in p.get('polygons',[]):
            poly['shell']=[point(q) for q in poly['shell']]
    for p in m['endpoints']:p['center']=point(p['center'])
    m['outline_edges']=[[point(p) for p in edge] for edge in m['outline_edges']]
    corners=[point(p) for p in [[4,12],[26,12],[26,18],[4,18]]]
    m['task']['region_mm']=[min(p[0] for p in corners),min(p[1] for p in corners),max(p[0] for p in corners),max(p[1] for p in corners)]
    m['task']['target_net']='independent-name'
    return m

class BoundaryCorridor(unittest.TestCase):
    def test_rotations_reflections_and_reverse_endpoints(self):
        for angle in (0,90,180,270):
            for mirror in (False,True):
                for reverse in (False,True):
                    with self.subTest(angle=angle,mirror=mirror,reverse=reverse):
                        m=transform(attached_model(),angle,mirror)
                        if reverse:m['endpoints'].reverse()
                        saved=copy.deepcopy(m);result=search(m)
                        self.assertEqual(result['status'],'found')
                        self.assertEqual(result['graph_scope']['corridor_policy'],'exterior_boundary_detour')
                        self.assertLessEqual(result['vertices'],1200)
                        self.assertLessEqual(len(result['graph_scope']['boundary_attempts']),2)
                        free,*_=configuration_space(m)
                        self.assertTrue(free.covers(LineString(result['path_mm'])))
                        self.assertEqual(m,saved)

    def test_two_boundary_obstacles(self):
        m=attached_model()
        m['primitives'].append(dict(kind='keepout',net='',blocks_same_net=True,clearance_mm=0,
            polygons=[dict(shell=[[19,11],[20,11],[20,16],[19,16]],holes=[])]))
        r=search(m);self.assertEqual(r['status'],'found')
        free,*_=configuration_space(m);self.assertTrue(free.covers(LineString(r['path_mm'])))

    def test_guide_crossing_hole_does_not_remove_hole(self):
        m=attached_model()
        m['primitives'].append(dict(kind='circle',net='foreign',clearance_mm=.2,center=[12,15],size=[.4,.4]))
        r=search(m);free,*_=configuration_space(m)
        self.assertTrue(free.covers(LineString(r['path_mm'])))

    def test_both_candidates_over_cap_return_bounded_out(self):
        with patch('pcb_local.geometry_search.MAX_VERTICES',10):
            with self.assertRaises(SearchLimit) as error:search(attached_model())
        self.assertEqual(error.exception.kind,'local_corridor_disconnected')
        self.assertEqual(len(error.exception.details['boundary_attempts']),2)
        self.assertTrue(all(x['vertices']>10 for x in error.exception.details['boundary_attempts']))

    def test_deadline_is_not_renewed(self):
        m=attached_model();f,*_=configuration_space(m)
        def stop():raise SearchLimit('stopped','deadline')
        with self.assertRaises(SearchLimit):boundary_corridor(f,(6,15),(24,15),.45,stop)

    def test_fully_closed_region_is_not_repaired(self):
        m=attached_model();m['primitives'][-1]['polygons'][0]['shell']=[[14,11],[16,11],[16,19],[14,19]]
        self.assertEqual(search(m)['status'],'no_path')

if __name__=='__main__':unittest.main()
