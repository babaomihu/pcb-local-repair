"""Outline diagnostics preserve holes/islands and never authorize a candidate."""
import copy
import unittest
from pcb_local.outline_diagnostics import analyze_outline, unavailable


def rect(x0, y0, x1, y1):
    return {'shell': [[x0,y0],[x1,y0],[x1,y1],[x0,y1]], 'holes': []}


def raw(outline=None, region=None):
    return {'schema_version':1, 'status':'raw', 'native_version':'10.0.4',
            'source_sha256':'abc', 'source_sha256_after':'abc', 'source_unchanged':True,
            'complete_outline':True, 'inferred_outline':False,
            'region_mm':region or [2,2,8,8], 'outline':outline or [rect(0,0,10,10)],
            'edges':[{'uuid':'bottom', 'shape':0, 'polygons':[rect(-.05,-.05,10.05,.05)]}]}


class OutlineDiagnosticsTests(unittest.TestCase):
    def test_inside_is_not_routing_acceptance(self):
        value=raw();saved=copy.deepcopy(value);report=analyze_outline(value)
        self.assertEqual(report['status'],'analyzed')
        self.assertTrue(report['roi_wholly_in_native_outline'])
        self.assertFalse(report['acceptance_authorized'])
        self.assertFalse(report['search_model_changed'])
        self.assertFalse(report['physical_unreachability_proven'])
        self.assertEqual(value,saved)

    def test_hole_enclosed_despite_four_inside_corners(self):
        board=rect(0,0,10,10);board['holes']=[rect(4,4,6,6)['shell']]
        report=analyze_outline(raw([board]))
        self.assertEqual(report['outline_holes'],1)
        self.assertEqual(report['roi_board_holes'],1)
        self.assertFalse(report['roi_wholly_in_native_outline'])
        self.assertAlmostEqual(report['roi_outside_outline_area_mm2'],4)

    def test_islands_do_not_become_one_bbox(self):
        report=analyze_outline(raw([rect(0,0,4,10),rect(6,0,10,10)]))
        self.assertEqual(report['outline_components'],2)
        self.assertEqual(report['roi_board_components'],2)
        self.assertFalse(report['physical_unreachability_proven'])
        self.assertIn('existing copper',report['interpretation'])

    def test_notch_with_one_outer_ring_has_two_roi_components(self):
        board={'shell':[[0,0],[4,0],[4,6],[6,6],[6,0],[10,0],[10,10],[0,10]],'holes':[]}
        report=analyze_outline(raw([board],[2,1,8,5]))
        self.assertEqual(report['outline_components'],1)
        self.assertEqual(report['roi_board_components'],2)

    def test_touching_edge_and_long_distant_center_are_reported(self):
        value=raw(region=[2,0,8,8]);value['edges'][0]['polygons']=[rect(-100,-.05,100,.05)]
        report=analyze_outline(value)
        self.assertEqual([e['uuid'] for e in report['intersecting_edges']],['bottom'])
        self.assertEqual(report['intersecting_edges'][0]['bounds_mm'],[-100,-.05,100,.05])

    def test_incomplete_or_inferred_or_mutated_source_fails_unavailable(self):
        for key,value in [('complete_outline',False),('inferred_outline',True),('source_sha256_after','changed'),('native_version','9.0.0')]:
            with self.subTest(key=key):
                data=raw();data[key]=value
                self.assertEqual(analyze_outline(data)['status'],'unavailable')

    def test_missing_edges_duplicate_id_or_invalid_roi_do_not_claim_complete(self):
        for change in ({'edges':[]},{'region_mm':[0,0,float('nan'),10]}):
            data=raw();data.update(change);self.assertEqual(analyze_outline(data)['status'],'unavailable')
        data=raw();data['edges']*=2;self.assertEqual(analyze_outline(data)['status'],'unavailable')

    def test_native_failure_retains_reason(self):
        report=analyze_outline(unavailable('Native complete outline failed'))
        self.assertEqual(report['status'],'unavailable')
        self.assertIn('Native complete outline failed',report['reason'])
        self.assertTrue(report['diagnostic_only'])


if __name__=='__main__':unittest.main()
