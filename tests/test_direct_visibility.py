import copy
import unittest
from unittest.mock import patch
from shapely.geometry import LineString
from pcb_local.geometry_search import search,configuration_space,SearchLimit,MAX_VERTICES
from test_geometry_search import model


def complex_model(blocked=False):
    m=model();m['outline_edges']=[[[0,0],[30,0]],[[30,0],[30,30]],[[30,30],[0,30]],[[0,30],[0,0]]]
    m['task']['region_mm']=[4,12,26,18]
    m['endpoints'][0]['center']=[6,15];m['endpoints'][1]['center']=[24,15]
    m['primitives']=[dict(kind='circle',net='foreign_'+str(x)+str(y),clearance_mm=.2,center=[x,y],size=[.4,.4])
                     for x in range(6,25,2) for y in (12.8,17.2)]
    if blocked:m['primitives'].append(dict(kind='circle',net='middle',clearance_mm=.2,center=[15,15],size=[.4,.4]))
    return m


class DirectVisibility(unittest.TestCase):
    def test_full_boundary_exceeds_cap_but_straight_route_is_proved(self):
        m=complex_model();original=copy.deepcopy(m);free,*_=configuration_space(m,lambda:None)
        count=sum(len(r.coords)-1 for r in [free.exterior,*free.interiors])
        self.assertGreater(count,MAX_VERTICES)
        r=search(m);self.assertEqual(r['method'],'conservative-configuration-space-direct')
        self.assertEqual(r['vertices'],2);self.assertEqual(r['expanded'],0)
        self.assertEqual(r['length_mm'],18);self.assertTrue(free.covers(LineString(r['path_mm'])))
        self.assertEqual(m,original)
    def test_blocked_straight_line_uses_bounded_local_graph(self):
        m=complex_model(True);r=search(m)
        self.assertEqual(r['status'],'found')
        self.assertLessEqual(r['vertices'],MAX_VERTICES)
        self.assertEqual(r['graph_scope']['kind'],'restricted_corridor')
        free,*_=configuration_space(m,lambda:None)
        self.assertTrue(free.covers(LineString(r['path_mm'])))
    def test_other_net_local_clearance_is_preserved(self):
        m=complex_model();m['primitives'][0]['clearance_mm']=3
        r=search(m);self.assertNotEqual(r['status'],'found')
    def test_same_net_keepout_still_blocks(self):
        m=complex_model();m['primitives'].append(dict(kind='keepout',net=m['task']['target_net'],blocks_same_net=True,
            clearance_mm=0,polygons=[dict(shell=[[14,11],[16,11],[16,19],[14,19]],holes=[])]))
        self.assertEqual(search(m)['status'],'no_path')
    def test_primitive_limit_not_bypassed(self):
        with patch('pcb_local.geometry_search.MAX_PRIMITIVES',1):
            with self.assertRaises(SearchLimit):search(complex_model())
    def test_deadline_not_bypassed(self):
        with self.assertRaises(SearchLimit):search(complex_model(),timeout=0)
    def test_nonstraight_search_falls_back_without_weaker_checks(self):
        m=model();m['primitives']=[dict(kind='circle',net='middle',clearance_mm=.2,center=[5,5],size=[1,1])]
        r=search(m);self.assertEqual(r['status'],'found');self.assertEqual(r['method'],'conservative-configuration-space-visibility-A*')
        free,*_=configuration_space(m,lambda:None);self.assertTrue(free.covers(LineString(r['path_mm'])))


if __name__=='__main__':unittest.main()
