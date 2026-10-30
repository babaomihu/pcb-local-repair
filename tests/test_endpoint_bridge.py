import copy,math,unittest
from shapely.geometry import box,Point,LineString
from pcb_local.endpoint_bridge import anchored_path
from pcb_local.geometry_search import search,configuration_space
from pcb_local.mesh_corridor import MeshLimit
from test_mesh_corridor import dense
from test_boundary_corridor import transform

class EndpointBridge(unittest.TestCase):
    def test_oblique_and_reversed_endpoints(self):
        for angle in (13,37,53,71,123):
            for reverse in (False,True):
                with self.subTest(angle=angle,reverse=reverse):
                    m=transform(dense(),angle)
                    if reverse:m['endpoints'].reverse()
                    saved=copy.deepcopy(m);r=search(m,timeout=4)
                    self.assertEqual(r['status'],'found')
                    ends=[tuple(p['center']) for p in m['endpoints']]
                    self.assertEqual(tuple(r['path_mm'][0]),ends[0]);self.assertEqual(tuple(r['path_mm'][-1]),ends[1])
                    full,*_=configuration_space(m);self.assertTrue(full.covers(LineString(r['path_mm'])))
                    self.assertLessEqual(r['vertices'],1200);self.assertEqual(saved,m)
                    if angle==71:
                        access=r['endpoint_access'];self.assertFalse(access['physical_endpoints_moved'])
                        self.assertEqual(access['tolerance_added_mm'],0)
                        self.assertTrue(all(n<=8 for n in access['candidates_tested']))
    def test_access_proved_without_snapping_physical_endpoint(self):
        full=window=box(0,0,10,10);local=box(.001,0,10,10)
        r=anchored_path(local,full,window,(0,5),(9,5),check=lambda:None,max_vertices=1200)
        self.assertIsNotNone(r);self.assertEqual(tuple(r['path_mm'][0]),(0,5))
        self.assertNotEqual(tuple(r['endpoint_access']['search_seeds'][0]),(0,5))
        self.assertTrue(full.covers(LineString(r['path_mm'])))
    def test_original_endpoint_outside_either_source_is_rejected(self):
        local=box(.001,0,10,10)
        for full,window in [(box(1e-12,0,10,10),box(0,0,10,10)),(box(0,0,10,10),box(1e-12,0,10,10))]:
            self.assertIsNone(anchored_path(local,full,window,(0,5),(9,5),check=lambda:None,max_vertices=1200))
    def test_access_cannot_cross_copper_or_window_hole(self):
        blocked=box(0,0,10,10).difference(box(.0001,-1,.0002,11));local=box(.001,0,10,10)
        for full,window in [(blocked,box(0,0,10,10)),(box(0,0,10,10),blocked)]:
            self.assertIsNone(anchored_path(local,full,window,(0,5),(9,5),check=lambda:None,max_vertices=1200))
    def test_inside_points_do_not_invoke_new_strategy(self):
        part=box(0,0,10,10)
        self.assertIsNone(anchored_path(part,part,part,(1,5),(9,5),check=lambda:None,max_vertices=1200))
    def test_shared_limits_preserved(self):
        m=transform(dense(),71);full,*_=configuration_space(m);a,b=[tuple(p['center']) for p in m['endpoints']]
        w=LineString([a,b]).buffer(math.dist(a,b)/16,cap_style=2);local=full.intersection(w)
        with self.assertRaises(MeshLimit):anchored_path(local,full,w,a,b,check=lambda:None,max_vertices=1)
        def stop():raise TimeoutError('unchanged deadline')
        with self.assertRaises(TimeoutError):anchored_path(local,full,w,a,b,check=stop,max_vertices=1200)

    def test_optional_resource_limit_keeps_existing_boundary_fallback(self):
        from unittest.mock import patch
        from test_boundary_corridor import attached_model
        with patch('pcb_local.endpoint_bridge.anchored_path',side_effect=MeshLimit('bounded','mesh_cell_limit',maximum=10000)):
            r=search(attached_model())
        self.assertEqual(r['status'],'found')
        self.assertEqual(r['graph_scope']['corridor_policy'],'exterior_boundary_detour')
        self.assertEqual(r['graph_scope']['endpoint_access_limit']['kind'],'mesh_cell_limit')
    def test_shared_deadline_not_swallowed_by_optional_fallback(self):
        from unittest.mock import patch
        from test_boundary_corridor import attached_model
        from pcb_local.geometry_search import SearchLimit
        with patch('pcb_local.endpoint_bridge.anchored_path',side_effect=SearchLimit('expired','deadline')):
            with self.assertRaises(SearchLimit) as caught:search(attached_model())
        self.assertEqual(caught.exception.kind,'deadline')

if __name__=='__main__':unittest.main()
