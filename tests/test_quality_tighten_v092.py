"""Bounded intersection proposals spend saved length, never physical clearance."""
import copy
import math
import unittest
from pcb_local.layered_quality import _intersection,_changed_edges_witness,tighten_intersections
from test_trace_quality import model,GEOMETRY


@unittest.skipUnless(GEOMETRY,'Pinned geometry runtime required')
class IntersectionQualityTests(unittest.TestCase):
    def setUp(self):
        self.task=dict(target_net='signal',target_pad_uuids=['a','b'],region_mm=[4,4,26,26])
        self.m=model(self.task)
        self.model=dict(endpoints=self.m['endpoints'],routing_models={'B.Cu':self.m})
        self.result=[[10,10],[11,10],[12,11],[12,12]]
        self.raw=[[10,10],[9,9],[12,11],[12,12]]
    def plans(self,credit=True):
        return [dict(layer='B.Cu',points=copy.deepcopy(self.raw if credit else self.result),result=copy.deepcopy(self.result),kept_reason=None)]
    def test_integer_intersection_and_parallel(self):
        self.assertEqual(_intersection([10,10],[11,10],[12,11],[12,12]),[12,10])
        self.assertIsNone(_intersection([0,0],[1,0],[0,1],[1,1]))
        q=_intersection([0,0],[3,1],[0,1],[1,0]);self.assertEqual(q,[.75,.25])
    def test_fractional_native_input_rejected(self):
        with self.assertRaises(ValueError):_intersection([0,.0000001],[1,0],[0,1],[1,1])
    def test_tradeoff_requires_prior_savings(self):
        plans=self.plans();r=tighten_intersections(plans,self.model,.25)
        self.assertEqual(r['removed_segments'],1)
        self.assertGreater(r['length_increase_vs_prior_quality_mm'],0)
        self.assertLessEqual(r['final_length_mm'],r['raw_length_mm'])
        self.assertEqual(plans[0]['result'],[[10,10],[12,10],[12,12]])
        frozen=self.plans(False);r=tighten_intersections(frozen,self.model,.25)
        self.assertEqual(r['removed_segments'],0);self.assertEqual(frozen[0]['result'],self.result)
    def test_incidental_contact_freeze_is_honored(self):
        p=self.plans();p[0]['kept_reason']='Additional same-net contact'
        self.assertEqual(tighten_intersections(p,self.model,.25)['removed_segments'],0)
    def test_other_added_chain_contact_is_not_dropped(self):
        p=self.plans();p.append(dict(layer='B.Cu',points=[[11,10],[11,9]],result=[[11,10],[11,9]]))
        self.assertEqual(tighten_intersections(p,self.model,.25)['removed_segments'],0)
    def test_changed_edge_obstacle_rejects(self):
        self.m['primitives']=[dict(kind='circle',net='obstacle',uuid='o',center=[12,10],size=[.1,.1],clearance_mm=.2)]
        self.assertEqual(tighten_intersections(self.plans(),self.model,.25)['removed_segments'],0)
    def test_roi_not_enlarged(self):
        self.m['task']['region_mm']=[4,10,26,26]
        self.assertEqual(tighten_intersections(self.plans(),self.model,.25)['removed_segments'],0)
    def test_analytic_circle_preserves_full_ten_nm_margin(self):
        from shapely.geometry import LineString
        self.m['primitives']=[dict(kind='circle',net='other',uuid='o',center=[15,15],size=[1,1],clearance_mm=.2)]
        w=_changed_edges_witness(self.m,.25,lambda:None)
        self.assertEqual(w(LineString([[15.825011,14.9],[15.825011,15.1]])),'analytic_round_primitives_same_safety')
        self.assertIsNone(w(LineString([[15.825009,14.9],[15.825009,15.1]])))
        self.assertIsNone(w(LineString([[15.824999,14.9],[15.824999,15.1]])))
    def test_analytic_segment_preserves_rule(self):
        from shapely.geometry import LineString
        self.m['primitives']=[dict(kind='segment',net='other',uuid='o',start=[15,14],end=[15,16],width_mm=1,clearance_mm=.2)]
        w=_changed_edges_witness(self.m,.25,lambda:None)
        self.assertIsNotNone(w(LineString([[15.825011,14.9],[15.825011,15.1]])))
        self.assertIsNone(w(LineString([[15.825009,14.9],[15.825009,15.1]])))
    def test_complex_blocker_remains_conservative(self):
        from shapely.geometry import LineString
        self.m['primitives']=[dict(kind='circle',net='other',uuid='o',center=[15,15],size=[1,1],clearance_mm=.2),
            dict(kind='keepout',net='',uuid='k',clearance_mm=0,polygons=[dict(shell=[[15.8,14],[16,14],[16,16],[15.8,16]],holes=[])])]
        self.assertIsNone(_changed_edges_witness(self.m,.25,lambda:None)(LineString([[15.825011,14.9],[15.825011,15.1]])))
    def test_cancel_propagates_without_extra_budget(self):
        def check():raise TimeoutError('cancelled')
        with self.assertRaises(TimeoutError):tighten_intersections(self.plans(),self.model,.25,check)

if __name__=='__main__':unittest.main()
