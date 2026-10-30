"""Geometry invariants, independent of stored challenge/reference routes."""
import copy
import math
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
try:
    import shapely
    from shapely.geometry import LineString
    GEOMETRY = True
except ImportError:
    GEOMETRY = False
from pcb_local.geometry_search import search, SearchLimit, native_polygon


def model(gap=.653, dx=0, dy=0):
    def pt(x,y): return [x+dx,y+dy]
    endpoints=[dict(center=pt(2,5),track_width_mm=.25,clearance_mm=.2),
               dict(center=pt(8,5),track_width_mm=.25,clearance_mm=.2)]
    return dict(endpoints=endpoints, minimum_track_width_mm=.25, minimum_clearance_mm=.2,
        edge_clearance_mm=.2, task=dict(target_net='arbitrary',region_mm=[dx,dy,dx+10,dy+10]),
        outline_edges=[[pt(0,0),pt(10,0)],[pt(10,0),pt(10,10)],[pt(10,10),pt(0,10)],[pt(0,10),pt(0,0)]],
        primitives=[dict(kind='segment',net='other',clearance_mm=.2,width_mm=.3,start=pt(5,0),end=pt(5,5-(gap+.3)/2)),
                    dict(kind='segment',net='other',clearance_mm=.2,width_mm=.3,start=pt(5,5+(gap+.3)/2),end=pt(5,10))])


@unittest.skipUnless(GEOMETRY, 'Run with pinned router Python for geometry tests')
class GeometryChecks(unittest.TestCase):
    def test_translation_and_net_renaming_do_not_remove_narrow_passage(self):
        for dx,dy in [(0,0),(13.137,7.333),(-31.817,-17.913)]:
            m=model(dx=dx,dy=dy);m['task']['target_net']='signal_renamed'
            result=search(m)
            self.assertEqual(result['status'],'found')
            self.assertEqual(result['width_mm'],.25)
            path=LineString(result['path_mm'])
            for item in m['primitives']:
                self.assertGreaterEqual(path.distance(LineString([item['start'],item['end']])),.475-1e-9)

    def test_geometrically_closed_gap_stays_closed(self):
        self.assertEqual(search(model(.649))['status'],'no_path')

    def test_larger_rule_cannot_be_ignored(self):
        for key in ['minimum_clearance_mm','minimum_track_width_mm']:
            m=model();m[key]=.4
            self.assertEqual(search(m)['status'],'no_path')
        m=model();m['primitives'][0]['clearance_mm']=.3
        self.assertEqual(search(m)['status'],'no_path')

    def test_endpoint_outside_roi_is_not_extended(self):
        m=model();m['task']['region_mm']=[3,0,10,10]
        self.assertEqual(search(m)['status'],'no_path')

    def test_board_cutout_is_an_obstacle(self):
        m=model(gap=2);m['primitives']=[]
        m['outline_edges'] += [[[4,4],[6,4]],[[6,4],[6,6]],[[6,6],[4,6]],[[4,6],[4,4]]]
        result=search(m);self.assertEqual(result['status'],'found')
        from shapely.geometry import box
        self.assertGreaterEqual(LineString(result['path_mm']).distance(box(4,4,6,6)),.325-1e-9)

    def test_unclosed_outline_rejected(self):
        m=model();m['outline_edges'].pop()
        with self.assertRaises(ValueError):search(m)

    def test_unknown_copper_is_not_ignored(self):
        m=model();m['primitives'][0]['kind']='custom'
        with self.assertRaises(ValueError):search(m)

    def test_budget_and_size_limits(self):
        from unittest.mock import patch
        with self.assertRaises(SearchLimit):search(model(),timeout=-1)
        # A blocked direct line needs a visibility graph and still obeys its cap.
        bent=model();bent['primitives']=[dict(kind='circle',net='other',clearance_mm=.2,center=[5,5],size=[1,1])]
        with patch('pcb_local.geometry_search.MAX_VERTICES',1):
            with self.assertRaises(SearchLimit):search(bent)

    def test_no_mutation_of_model(self):
        m=model();before=copy.deepcopy(m);search(m);self.assertEqual(m,before)

    def test_native_fractured_hole_preserved_without_rounding(self):
        shell=[[0,0],[10,0],[10,10],[0,10],[0,0],[3,3],[3,7],[7,7],[7,3],[3,3],[0,0]]
        from shapely.geometry import Polygon
        shape=native_polygon(dict(shell=shell,holes=[]))
        self.assertEqual(shape.area,84)
        self.assertEqual(len(shape.interiors),1)
        self.assertTrue(shape.equals(Polygon(shell[:4], [[[3,3],[3,7],[7,7],[7,3]]])))

    def test_unknown_invalid_polygon_not_repaired(self):
        with self.assertRaises(ValueError):
            native_polygon(dict(shell=[[0,0],[2,2],[0,2],[2,0]],holes=[]))

    def test_keepout_cannot_be_ignored_as_same_net(self):
        m=model();m['primitives']=[dict(kind='keepout',net='arbitrary',blocks_same_net=True,
            clearance_mm=0,polygons=[dict(shell=[[4,0],[6,0],[6,10],[4,10]],holes=[])])]
        self.assertEqual(search(m)['status'],'no_path')

    def test_physical_hole_clearance_changes_path(self):
        m=model();m['primitives']=[dict(kind='hole',net='',blocks_same_net=True,
            clearance_mm=0,center=[5,5],diameter_mm=1,hole_clearance_mm=.4)]
        result=search(m);self.assertEqual(result['status'],'found')
        from shapely.geometry import Point
        self.assertGreaterEqual(LineString(result['path_mm']).distance(Point(5,5)),.5+.4+.125-1e-9)

if __name__=='__main__':unittest.main()
