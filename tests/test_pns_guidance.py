"""Cursor proposals retain fixed-copper constraints and never grant permissions."""
import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
try:
    from shapely.geometry import LineString
    GEOMETRY=True
except ImportError:
    GEOMETRY=False
from pcb_local.pns_guidance import guidance
from pcb_local.geometry_search import configuration_space, SearchLimit
from pcb_local.pns_worker import cursor_positions


def model():
    return dict(endpoints=[dict(center=[2,5],size=[1,1],track_width_mm=.25,clearance_mm=.2),
                           dict(center=[8,5],size=[1,1],track_width_mm=.25,clearance_mm=.2)],
        minimum_track_width_mm=.25,minimum_clearance_mm=.2,edge_clearance_mm=.2,
        task=dict(target_net='signal',region_mm=[0,0,10,10],allowed_layer='F.Cu'),
        outline_edges=[[[0,0],[10,0]],[[10,0],[10,10]],[[10,10],[0,10]],[[0,10],[0,0]]],primitives=[])


def wall(m):
    m['primitives']=[dict(uuid='wall',kind='segment',net='other',clearance_mm=.2,width_mm=.3,
                          start=[5,0],end=[5,10],locked=False)]


def authorize(m):
    m['task']['modification']=dict(schema_version=1,mode='shove',routing_uuids=['wall'],nets=['other'],max_modified_tracks=1)


@unittest.skipUnless(GEOMETRY,'Pinned router Python required')
class GuidanceChecks(unittest.TestCase):
    def test_cursor_quota_and_exact_endpoint_are_preserved(self):
        start=[2,5];end=[8,5];waypoints=[[3,4.5],[7,4.5]]
        path=LineString([start,*waypoints,end])
        for quota in [1,7,28,64]:
            points=list(cursor_positions(start,end,waypoints,quota))
            self.assertEqual(len(points),quota);self.assertEqual(points[-1],end)
            from shapely.geometry import Point
            self.assertTrue(all(path.distance(Point(p))<1e-12 for p in points))

    def test_zero_length_cursor_legs_do_not_consume_extra_moves(self):
        points=list(cursor_positions([1,1],[1,1],[[1,1]],3))
        self.assertEqual(points,[[1,1]]*3)

    def test_lanes_are_witnessed_and_do_not_mutate_input(self):
        m=model();before=copy.deepcopy(m);r=guidance(m);free,*_=configuration_space(m)
        self.assertEqual(m,before);self.assertEqual(r['status'],'ready');self.assertEqual(len(r['plans']),2)
        for row in r['plans']:
            path=[m['endpoints'][0]['center'],*row['waypoints'],m['endpoints'][1]['center']]
            self.assertTrue(free.covers(LineString(path)));self.assertEqual(len(row['waypoints']),2)
            for endpoint,waypoint in zip(m['endpoints'],row['waypoints']):
                self.assertGreater(LineString([endpoint['center'],waypoint]).length,.5)

    def test_unlocked_does_not_imply_authorized(self):
        m=model();wall(m);r=guidance(m)
        self.assertEqual(r['status'],'no_guidance');self.assertEqual(r['omitted_movable_segments'],[])
        self.assertIn('not a physical impossibility proof',r['reason'])

    def test_authorized_motion_is_guidance_only(self):
        m=model();wall(m);authorize(m);before=copy.deepcopy(m);r=guidance(m)
        self.assertEqual(r['status'],'ready');self.assertEqual(r['omitted_movable_segments'],['wall']);self.assertEqual(m,before)

    def test_locked_authorized_segment_remains_blocker(self):
        m=model();wall(m);authorize(m);m['primitives'][0]['locked']=True
        self.assertEqual(guidance(m)['status'],'no_guidance')

    def test_via_is_never_omitted_by_segment_permission(self):
        m=model();authorize(m);m['primitives']=[dict(uuid='wall',kind='circle',net='other',center=[5,5],size=[20,20],clearance_mm=.2,locked=False)]
        r=guidance(m);self.assertEqual(r['omitted_movable_segments'],[]);self.assertEqual(r['status'],'no_guidance')

    def test_unknown_geometry_fails_closed(self):
        m=model();wall(m);m['primitives'][0]['kind']='unmodelled'
        with self.assertRaises(ValueError):guidance(m)

    def test_shared_budget_is_bounded(self):
        m=model();wall(m)
        with self.assertRaises(SearchLimit):guidance(m,timeout=-1)

    def test_native_shape_centers_used(self):
        m=model();m['endpoints'][0]['center']=[-10,5]
        m['endpoints'][0]['native_pad']={'shape_center':[2,5]}
        self.assertEqual(guidance(m)['status'],'ready')

    def test_endpoint_outside_roi_not_extended(self):
        m=model();m['task']['region_mm']=[3,0,10,10]
        self.assertEqual(guidance(m)['status'],'no_guidance')

if __name__=='__main__':unittest.main()
