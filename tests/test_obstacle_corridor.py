import copy, math, unittest
from unittest.mock import patch
from shapely.geometry import LineString
from pcb_local.geometry_search import search,configuration_space,SearchLimit,MAX_VERTICES
from test_direct_visibility import complex_model

def wide():
    m=complex_model(True);m['primitives'][-1]['size']=[2.5,2.5]
    return m

class ObstacleCorridor(unittest.TestCase):
    def assert_route(self,m):
        before=copy.deepcopy(m);r=search(m);self.assertEqual(r['status'],'found')
        self.assertEqual(r['graph_scope']['corridor_policy'],'blocking_hole_envelope')
        self.assertGreater(r['graph_scope']['radius_mm'],r['graph_scope']['initial_radius_mm'])
        self.assertLessEqual(r['vertices'],MAX_VERTICES)
        free,*_=configuration_space(m);self.assertTrue(free.covers(LineString(r['path_mm'])))
        self.assertEqual(m,before);self.assertFalse(r['graph_scope']['global_shortest_claimed'])
        return r
    def test_retained_large_obstacle_now_uses_its_boundary(self):
        r=self.assert_route(wide());self.assertEqual(r['graph_scope']['blocking_holes'],1)
    def test_translation_rotation_and_renaming(self):
        for angle in [23,90]:
            m=wide();a=math.radians(angle)
            def pt(p):return [p[0]*math.cos(a)-p[1]*math.sin(a)+81.27,p[0]*math.sin(a)+p[1]*math.cos(a)-23.13]
            for p in m['primitives']:p['center']=pt(p['center'])
            for p in m['endpoints']:p['center']=pt(p['center'])
            m['outline_edges']=[[pt(p) for p in edge] for edge in m['outline_edges']]
            corners=[pt(p) for p in [[4,12],[26,12],[26,18],[4,18]]]
            m['task']['region_mm']=[min(p[0] for p in corners),min(p[1] for p in corners),max(p[0] for p in corners),max(p[1] for p in corners)]
            m['task']['target_net']='a_different_net';self.assert_route(m)
    def test_multiple_blockers_and_original_rules(self):
        m=complex_model();m['primitives'] += [dict(kind='circle',net='unrelated',clearance_mm=.21,center=[x,15],size=[2,2]) for x in [12,18]]
        m['minimum_track_width_mm']=.28;m['minimum_clearance_mm']=.21
        r=self.assert_route(m);self.assertEqual(r['graph_scope']['blocking_holes'],2)
        self.assertEqual(r['width_mm'],.28);self.assertEqual(r['clearance_mm'],.21)
    def test_new_window_must_still_obey_same_graph_cap(self):
        with patch('pcb_local.geometry_search.MAX_VERTICES',1):
            with self.assertRaises(SearchLimit) as c:search(wide())
        self.assertEqual(c.exception.kind,'vertex_limit');self.assertEqual(c.exception.details['maximum'],1)
    def test_full_barrier_not_reinterpreted_as_hole_to_remove(self):
        m=wide();m['primitives'].append(dict(kind='keepout',net='',blocks_same_net=True,clearance_mm=0,polygons=[dict(shell=[[14,11],[16,11],[16,19],[14,19]],holes=[])]))
        self.assertEqual(search(m)['status'],'no_path')
    def test_deadline_not_reset_after_full_space(self):
        # Trigger cutoff after constructing the complete space, not at entry.
        import pcb_local.geometry_search as g
        original=g.configuration_space;clock=[0]
        def space(*args):
            value=original(*args);clock[0]=10;return value
        with patch.object(g,'configuration_space',side_effect=space),patch.object(g.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(SearchLimit) as c:search(wide(),timeout=5)
        self.assertEqual(c.exception.kind,'deadline')
