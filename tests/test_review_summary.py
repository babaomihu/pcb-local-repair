"""Report actual copper deltas without turning geometric metrics into proof."""
import copy
import unittest
from pcb_local.review_summary import quality, quality_text, capabilities
from pcb_local.auto_strategy import plans
from test_scope_candidate import TASK


def segment(a,b,layer='F.Cu',net='N'):
    return dict(kind='segment',start_mm=a,end_mm=b,layer=layer,net=net)


class ReviewSummaryTests(unittest.TestCase):
    def test_metrics_distinguish_layers_and_exclude_arcs(self):
        d=dict(added_details=[segment([0,0],[1,0]),segment([1,0],[1,1]),
            segment([0,0],[.05,0],'B.Cu'),dict(kind='via'),dict(kind='arc')],
            changed_routing=['old'],removed_routing=[])
        r=quality(d)
        self.assertEqual(r['added_straight_segments'],3)
        self.assertEqual(r['added_vias'],1)
        self.assertEqual(r['added_arc_count'],1)
        self.assertAlmostEqual(r['straight_length_mm'],2.05)
        self.assertEqual(r['short_straight_segments'],1)
        self.assertEqual(r['noncollinear_two_segment_joints'],1)
        self.assertEqual(r['modified_original_objects'],1)
        self.assertTrue(r['display_only'])
        self.assertIn('不含弧长',r['limitation'])
        self.assertIn('2.050',quality_text(r))

    def test_cross_net_endpoints_are_not_a_bend(self):
        r=quality(dict(added_details=[segment([0,0],[1,0]),segment([1,0],[1,1],net='OTHER')]))
        self.assertEqual(r['noncollinear_two_segment_joints'],0)

    def test_zero_new_vias_does_not_remove_fixed_bridge_strategy(self):
        t=copy.deepcopy(TASK)
        t['permissions']=dict(schema_version=1,route_layers=['F.Cu','B.Cu'],max_new_vias=0,via_type='through')
        before=copy.deepcopy(t)
        rows=plans(t,False)
        self.assertIn('geometry-multilayer',[r[0] for r in rows])
        self.assertTrue(all(r[2]['permissions']['max_new_vias']==0 for r in rows))
        self.assertEqual(t,before)
        r=capabilities(t,{})
        self.assertEqual(r['strategies'][-1]['route_layers'],['F.Cu','B.Cu'])
        self.assertEqual(r['strategies'][-1]['state'],'requires_native_check')

    def test_refill_is_not_advertised_as_multilayer_support(self):
        t=copy.deepcopy(TASK)
        t['zone_refill']={'zones':['plane']}
        r=capabilities(t,{'pns':{'configured':True}})
        self.assertTrue(all(x['state']=='unsupported_combination' for x in r['strategies'] if x['strategy']!='pns'))
        self.assertEqual(next(x for x in r['strategies'] if x['strategy']=='pns')['state'],'requires_native_check')

    def test_invalid_metric_is_not_silently_zero(self):
        with self.assertRaises(ValueError):quality(dict(added_details=[segment([0,0],[float('nan'),0])]))
