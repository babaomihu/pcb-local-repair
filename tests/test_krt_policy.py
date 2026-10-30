"""Exercise the adapter against pinned upstream policy/floor implementations.

Search is replaced with a capture seam; these are parameter-boundary tests,
not evidence of native routing success. Source-only packages without the
separate KRT checkout skip this integration check explicitly.
"""
import copy
import importlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pcb_local.krt_local import route
from pcb_local.workspace import sha256


KRT = Path(os.environ.get('PCB_LOCAL_TEST_KRT_ROOT',
    str(Path(__file__).resolve().parents[1] / 'KiCadRoutingTools')))


@unittest.skipUnless((KRT / 'py_router/fab_tiers.py').is_file(), 'Separate pinned KRT checkout required')
class LocalKRTPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_path = list(sys.path)
        sys.path[:0] = [str(KRT / 'py_router'), str(KRT / 'rust_router')]
        cls.modules = {n: importlib.import_module(n) for n in (
            'fab_tiers', 'kicad_parser', 'obstacle_map', 'single_ended_routing', 'kicad_dru', 'pour_guard')}

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls.old_path

    def setUp(self):
        self.fab = self.modules['fab_tiers']
        self.previous_policy = self.fab.get_escalation_policy()
        self.previous_tier = self.fab.get_default_fab_tier()
        self.fab.set_default_fab_tier('auto')
        self.fab.set_escalation_policy('fab')

    def tearDown(self):
        self.fab.set_default_fab_tier(*self.previous_tier)
        self.fab.set_escalation_policy(*self.previous_policy)

    def exercise(self, value, layer_count=2, delivered_width=None):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / 'board.kicad_pcb'
            board.write_text('(kicad_pcb)\n')
            positions = [[2., 2.], [4., 2.], [8., 8.]]
            endpoints = [dict(uuid=['first','second','extra'][i],reference='J'+str(i), number='1', position_mm=xy) for i, xy in enumerate(positions)]
            pads = [SimpleNamespace(component_ref=e['reference'], pad_number='1',
                global_x=e['position_mm'][0], global_y=e['position_mm'][1], layers=['F.Cu']) for e in endpoints]
            pcb = SimpleNamespace(nets={1: SimpleNamespace(name='SIGNAL'), 2: SimpleNamespace(name='FOREIGN')},
                pads_by_net={1: pads}, board_info=SimpleNamespace(copper_layers=['F.Cu']+['In%d.Cu'%i for i in range(1,layer_count-1)]+['B.Cu']))
            native = dict(clearance=value, track_width=value, via_diameter=value,
                via_drill=value, via_annular_width=.2, board_edge_clearance=value,
                hole_clearance=value, hole_to_hole_clearance=value)
            request = dict(board=str(board), board_sha256=sha256(board), profile='baseline',
                task=dict(target_net='SIGNAL', target_pad_uuids=['first','second'], allowed_layer='F.Cu', region_mm=[1.,1.,5.,3.]),
                endpoints=endpoints[:2], source_net_pad_count=3,
                routing_plan=dict(native_rules=dict(board_minima_mm=native,
                    netclasses_mm={'SIGNAL': {k:value for k in ['track_width','clearance','via_drill','via_diameter']}}),
                    net_clearances_mm={'SIGNAL':value,'FOREIGN':.9}))
            original = copy.deepcopy(request)
            captured = {}
            def search(_pcb, _nid, cfg, _obstacles, **_kw):
                captured.update(width=cfg.track_width, clearance=cfg.clearance,
                    via_size=cfg.via_size, drill=cfg.via_drill, hole_to_hole=cfg.hole_to_hole_clearance,
                    edge=cfg.board_edge_clearance, foreign_clearance=cfg.net_clearances[2],
                    descent_rungs=self.fab.escalation_rungs(layer_count))
                return dict(new_vias=[], new_segments=[SimpleNamespace(start_x=2.,start_y=2.,end_x=4.,end_y=2.,
                    width=cfg.track_width if delivered_width is None else delivered_width, net_id=1,layer='F.Cu')])
            with patch.object(self.modules['kicad_parser'], 'parse_kicad_pcb', return_value=pcb), \
                 patch.object(self.modules['obstacle_map'], 'build_base_obstacle_map', return_value=object()), \
                 patch.object(self.modules['obstacle_map'], '_add_rectangular_edge_obstacles'), \
                 patch.object(self.modules['single_ended_routing'], 'route_net_with_obstacles', side_effect=search), \
                 patch.object(self.modules['kicad_dru'], 'install_layer_clearances'), \
                 patch.object(self.modules['kicad_dru'], 'install_track_clearances'), \
                 patch.object(self.modules['pour_guard'], 'install_pour_barriers'):
                result, report = route(request, KRT)
            self.assertEqual(request, original)
            self.assertEqual(sha256(board), original['board_sha256'])
            return captured, result, report

    def test_enabled_descent_policy_is_disabled_before_search(self):
        self.assertTrue(self.fab.escalation_rungs(2))
        cfg, result, report = self.exercise(.6)
        self.assertEqual(cfg['descent_rungs'], [])
        self.assertEqual(cfg['width'], .6)
        self.assertEqual(cfg['clearance'], .6)
        self.assertEqual(cfg['foreign_clearance'], .9)
        self.assertEqual(result['segments'][0]['width'], .6)
        self.assertEqual(report['fabrication_clamps'], {})

    def test_subphysical_request_is_raised_but_annular_minimum_preserved(self):
        for layers, width_floor, clearance_floor in [(2,.10,.10),(4,.0762,.09)]:
            with self.subTest(layers=layers):
                cfg, result, report = self.exercise(.01, layers)
                self.assertAlmostEqual(cfg['width'], width_floor)
                self.assertAlmostEqual(cfg['clearance'], clearance_floor)
                self.assertGreaterEqual(cfg['drill'], .15)
                self.assertGreaterEqual(cfg['via_size'], cfg['drill'] + .4)
                self.assertGreaterEqual(cfg['hole_to_hole'], .2)
                self.assertGreaterEqual(cfg['edge'], .2)
                self.assertEqual(cfg['foreign_clearance'], .9)
                self.assertEqual(cfg['descent_rungs'], [])
                self.assertEqual(result['segments'][0]['width'], cfg['width'])
                self.assertTrue(report['fabrication_clamps'])

    def test_search_cannot_return_original_subphysical_width_after_clamp(self):
        with self.assertRaisesRegex(ValueError, 'net/layer/width'):
            self.exercise(.01, delivered_width=.01)


if __name__ == '__main__':
    unittest.main()
