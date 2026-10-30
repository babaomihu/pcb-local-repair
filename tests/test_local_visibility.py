import copy,math,unittest
from unittest.mock import patch
from shapely.geometry import LineString
from pcb_local.geometry_search import search,configuration_space,SearchLimit
from test_direct_visibility import complex_model

class LocalVisibility(unittest.TestCase):
    def test_translation_rotation_and_net_renaming(self):
        m=complex_model(True)
        for angle in [0,23,90]:
            n=copy.deepcopy(m);a=math.radians(angle)
            def point(p):return [p[0]*math.cos(a)-p[1]*math.sin(a)+50,p[0]*math.sin(a)+p[1]*math.cos(a)-30]
            for p in n['primitives']:p['center']=point(p['center'])
            for p in n['endpoints']:p['center']=point(p['center'])
            n['outline_edges']=[[point(p) for p in e] for e in n['outline_edges']]
            corners=[point(p) for p in [[4,12],[26,12],[26,18],[4,18]]]
            n['task']['region_mm']=[min(p[0] for p in corners),min(p[1] for p in corners),max(p[0] for p in corners),max(p[1] for p in corners)]
            n['task']['target_net']='renamed'
            r=search(n);self.assertEqual(r['status'],'found');self.assertLessEqual(r['vertices'],1200)
            free,*_=configuration_space(n,lambda:None);self.assertTrue(free.covers(LineString(r['path_mm'])))
    def test_boundary_attached_obstacle_uses_exterior_detour(self):
        m=complex_model();m['primitives'].append(dict(kind='keepout',net='',blocks_same_net=True,clearance_mm=0,polygons=[dict(shell=[[14,11],[16,11],[16,16.5],[14,16.5]],holes=[])]))
        r=search(m)
        self.assertEqual(r['graph_scope']['corridor_policy'],'exterior_boundary_detour')
        free,*_=configuration_space(m)
        self.assertTrue(free.covers(LineString(r['path_mm'])))
    def test_closed_roi_stays_closed(self):
        m=complex_model(True);m['primitives'].append(dict(kind='keepout',net='',clearance_mm=0,
            polygons=[dict(shell=[[14,11],[16,11],[16,19],[14,19]],holes=[])]))
        self.assertEqual(search(m)['status'],'no_path')
    def test_cap_unchanged_even_for_local_window(self):
        with patch('pcb_local.geometry_search.MAX_VERTICES',1):
            with self.assertRaisesRegex(SearchLimit,'vertex count'):search(complex_model(True))
    def test_rules_and_geometry_are_not_mutated(self):
        m=complex_model(True);saved=copy.deepcopy(m);r=search(m)
        self.assertEqual(m,saved);self.assertEqual(r['width_mm'],.25);self.assertEqual(r['clearance_mm'],.2)
        self.assertEqual(r['additional_safety_mm'],.00001)
        self.assertFalse(r['graph_scope']['global_shortest_claimed'])

if __name__=='__main__':unittest.main()
