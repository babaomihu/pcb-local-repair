"""Drill-resource mechanism checks, not a replacement for native DRC."""
import copy
import itertools
import math
import unittest
from unittest.mock import patch

from test_multilayer_geometry import layered
from test_geometry_search import GEOMETRY
from pcb_local.multilayer_geometry import search
from pcb_local.geometry_search import SearchLimit


@unittest.skipUnless(GEOMETRY, 'Requires pinned geometry dependencies')
class MultilayerViaSafetyTests(unittest.TestCase):
    def test_same_net_existing_hole_still_blocks_new_drills(self):
        model = layered(); first = search(model)
        self.assertEqual(first['status'], 'found')
        center = first['via_positions_mm'][0]
        hole = dict(kind='hole', net=model['task']['target_net'], center=center,
                    diameter_mm=.3, clearance_mm=.2, hole_clearance_mm=.25, blocks_same_net=False)
        for layer in model['copper_layers']:
            model['via_models'][layer]['primitives'].append(copy.deepcopy(hole))
            model['routing_models'][layer]['primitives'].append(copy.deepcopy(hole))
        second = search(model)
        self.assertEqual(second['status'], 'found')
        for point in second['via_positions_mm']:
            self.assertGreaterEqual(math.dist(point, center), .3 + .25)

    def test_large_existing_hole_clearance_cannot_be_ignored(self):
        model = layered()
        hole = dict(kind='hole', net=model['task']['target_net'], center=[5,5],
                    diameter_mm=.3, clearance_mm=.2, hole_clearance_mm=.1, blocks_same_net=False)
        for layer in model['copper_layers']:
            model['via_models'][layer]['primitives'].append(copy.deepcopy(hole))
        model['hole_to_hole_clearance_mm'] = 20
        result = search(model)
        self.assertEqual(result['status'], 'no_path')

    def test_pairwise_new_holes_respect_larger_drill_clearance(self):
        model = layered(); model['hole_to_hole_clearance_mm'] = 4
        result = search(model)
        self.assertEqual(result['status'], 'found')
        self.assertEqual(len(result['via_positions_mm']), 2)
        for left, right in itertools.combinations(result['via_positions_mm'], 2):
            self.assertGreaterEqual(math.dist(left, right), result['via_drill_mm'] + 4)

    def test_impossible_new_hole_spacing_does_not_relax_rule(self):
        model = layered(); model['hole_to_hole_clearance_mm'] = 20
        result = search(model)
        self.assertEqual(result['status'], 'no_path')

    def test_via_only_keepout_on_collapsed_equal_inner_layer_stays_active(self):
        model = layered(); model['copper_layers'] = ['F.Cu','In1.Cu','In2.Cu','B.Cu']
        model['task']['permissions']['route_layers'] = list(model['copper_layers'])
        for layer in ('In1.Cu', 'In2.Cu'):
            model['routing_models'][layer] = copy.deepcopy(model['routing_models']['B.Cu'])
            model['via_models'][layer] = copy.deepcopy(model['via_models']['B.Cu'])
        model['via_models']['In2.Cu']['primitives'].append(dict(kind='keepout', net='',
            blocks_same_net=True, clearance_mm=0,
            polygons=[dict(shell=[[0,0],[10,0],[10,10],[0,10]], holes=[])]))
        self.assertEqual(search(model)['status'], 'no_path')

    def test_reverse_frontier_obeys_state_memory_cap(self):
        with patch('pcb_local.multilayer_geometry.MAX_STATES', 4):
            with self.assertRaises(SearchLimit) as caught: search(layered())
        self.assertIn('bound', str(caught.exception).lower())


if __name__ == '__main__': unittest.main()
