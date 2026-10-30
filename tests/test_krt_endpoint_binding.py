"""Independent contract checks for the KRT fixed native-endpoint adapter.

The upstream via-layer accessor is stubbed only at the module boundary; its
native parser/geometry behaviour is exercised by check_extended_endpoints.py.
These checks target identity binding and dispatch, not the upstream solver.
"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcb_local.candidate_pipeline import generate
from pcb_local.discovery import discover, select_task
from pcb_local.engine import command, local_pair_request
from pcb_local.jobs import Halt
from pcb_local.krt_local import validate_endpoints
from pcb_local.krt_result import checked_result, candidate
from pcb_local.validation import read_board, canonical


class NativeKRTEndpointBinding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.board = self.folder / 'board.kicad_pcb'
        self.board.with_suffix('.kicad_pro').write_text('{}')
        # The returned layer span represents the independently parsed object's
        # native full-stack span. A bad request cannot control this return.
        module = SimpleNamespace(via_copper_layers=lambda via, layers: list(layers))
        boundary = patch.dict(sys.modules, {'geometry_utils': module})
        boundary.start()
        self.addCleanup(boundary.stop)

    def fixture(self, via=False):
        layers = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
        nc = dict(name='Default', track_width_mm=.25, clearance_mm=.2)
        left = dict(kind='segment', uuid='left', net='DATA', copper_layers=['F.Cu'],
                    start_mm=[0., 0.], end_mm=[3., 0.], position_mm=[0., 0.],
                    width_mm=.25, netclass=nc, locked=True)
        if via:
            right = dict(kind='via', uuid='right', net='DATA', copper_layers=layers,
                         position_mm=[7., 0.], netclass=nc, locked=True,
                         standard_through=True)
            token = '(via (at 7 0) (size .8) (drill .4) (layers "F.Cu" "B.Cu") (net 1) (uuid "right"))'
        else:
            right = dict(kind='segment', uuid='right', net='DATA', copper_layers=['F.Cu'],
                         start_mm=[7., 0.], end_mm=[10., 0.], position_mm=[7., 0.],
                         width_mm=.25, netclass=nc, locked=True)
            token = '(segment (start 7 0) (end 10 0) (width .25) (layer "F.Cu") (net 1) (uuid "right"))'
        self.board.write_text('(kicad_pcb (net 1 "DATA") (segment (start 0 0) (end 3 0) '
                              '(width .25) (layer "F.Cu") (net 1) (uuid "left")) '+token+')')
        dims = dict(clearance=.2, track_width=.25, via_diameter=.8, via_drill=.4)
        snapshot = dict(items={'left': left, 'right': right}, copper_layers=layers,
                        component_of={'left': 0, 'right': 1},
                        components=[dict(net='DATA', items=['left'], pads=[]),
                                    dict(net='DATA', items=['right'], pads=[])],
                        routing_rules=dict(board_minima_mm={**dims, 'via_annular_width': .15},
                                           netclasses_mm={'': dims, 'DATA': dims}))
        listing = discover(snapshot, [2., -1., 8., 1.], 'F.Cu')
        task = select_task(listing, target=listing['targets'][0]['id'])
        def segment(row):
            return SimpleNamespace(uuid=row['uuid'], net_id=1, layer='F.Cu',
                                   start_x=row['start_mm'][0], start_y=0.,
                                   end_x=row['end_mm'][0], end_y=0., width=.25)
        pcb = SimpleNamespace(nets={1: SimpleNamespace(name='DATA')}, pads_by_net={},
                              segments=[segment(left)], vias=[],
                              board_info=SimpleNamespace(copper_layers=layers))
        if via:
            pcb.vias = [SimpleNamespace(uuid='right', net_id=1, x=7., y=0.,
                                        size=.8, drill=.4, via_type='through')]
        else:
            pcb.segments.append(segment(right))
        return snapshot, task, pcb

    def request(self, via=False):
        native, task, pcb = self.fixture(via)
        return local_pair_request(self.board, task, native), pcb, native

    def test_pure_tracks_have_no_pad_inventory_and_preserve_native_metadata(self):
        native, task, pcb = self.fixture()
        original = copy.deepcopy(native)
        request = local_pair_request(self.board, task, native)
        self.assertEqual(request['source_net_pad_count'], 0)
        self.assertEqual([e['position_mm'] for e in request['endpoints']], [[3., 0.], [7., 0.]])
        self.assertEqual(native, original)
        self.assertEqual(validate_endpoints(request, pcb), (1, []))

    def test_parent_refuses_changed_endpoint_bindings(self):
        native, task, _ = self.fixture()
        for key, value in [('uuid', 'other'), ('selector', 'start'), ('position_mm', [3.1, 0.]),
                           ('net', 'OTHER'), ('layer', 'B.Cu'), ('kind', 'via')]:
            changed = copy.deepcopy(task)
            changed['target_endpoints'][0][key] = value
            with self.subTest(key=key), self.assertRaises(Halt) as caught:
                local_pair_request(self.board, changed, native)
            self.assertEqual(caught.exception.status, 'unsupported')

    def test_parent_refuses_already_connected_objects(self):
        native, task, _ = self.fixture()
        native['component_of']['right'] = 0
        with self.assertRaises(Halt):
            local_pair_request(self.board, task, native)

    def test_parent_does_not_silently_route_whole_net_for_unsupported_permission(self):
        native, task, _ = self.fixture()
        task['permissions'] = dict(schema_version=1, route_layers=['F.Cu', 'B.Cu'],
                                   max_new_vias=1, via_type='through')
        with self.assertRaises(Halt):
            local_pair_request(self.board, task, native)
        config = dict(kicad_python='native', router_python='router', krt_root='unused')
        with self.assertRaises(Halt) as caught:
            command(config, self.board, self.folder/'out.kicad_pcb', task,
                    self.folder/'summary.json', 'baseline', native)
        self.assertEqual(caught.exception.status, 'unsupported')

    def test_worker_rejects_missing_or_duplicate_segment_identity(self):
        request, pcb, _ = self.request()
        for segments in [pcb.segments[1:], pcb.segments + [copy.deepcopy(pcb.segments[0])]]:
            changed = copy.deepcopy(pcb)
            changed.segments = segments
            with self.subTest(count=len(segments)), self.assertRaises(ValueError):
                validate_endpoints(request, changed)

    def test_worker_rejects_segment_parser_geometry_net_layer_and_size_drift(self):
        request, pcb, _ = self.request()
        for key, value in [('start_x', .1), ('end_y', .1), ('net_id', 2),
                           ('layer', 'B.Cu'), ('width', .20)]:
            changed = copy.deepcopy(pcb)
            setattr(changed.segments[0], key, value)
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_endpoints(request, changed)

    def test_worker_rejects_selector_and_endpoint_coordinate_mismatch(self):
        request, pcb, _ = self.request()
        for field, value in [('selector', 'start'), ('selector', 'middle')]:
            changed = copy.deepcopy(request)
            changed['task']['target_endpoints'][0][field] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_endpoints(changed, pcb)
        changed = copy.deepcopy(request)
        changed['endpoints'][0]['position_mm'] = [3.01, 0.]
        with self.assertRaises(ValueError):
            validate_endpoints(changed, pcb)

    def test_worker_rejects_task_and_request_endpoint_drift(self):
        request, pcb, _ = self.request()
        for key, value in [('position_mm', [3.1, 0.]), ('layer', 'B.Cu'), ('net', 'OTHER')]:
            changed = copy.deepcopy(request)
            changed['task']['target_endpoints'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_endpoints(changed, pcb)

    def test_worker_rejects_changed_native_width(self):
        request, pcb, _ = self.request()
        request['endpoints'][0]['width_mm'] = .3
        with self.assertRaises(ValueError):
            validate_endpoints(request, pcb)

    def test_full_span_via_can_be_fixed_single_layer_endpoint(self):
        request, pcb, _ = self.request(via=True)
        self.assertEqual(validate_endpoints(request, pcb), (1, []))
        self.assertEqual(request['task']['allowed_layer'], 'F.Cu')
        self.assertNotIn('permissions', request['task'])

    def test_via_span_type_and_native_standard_flag_cannot_be_changed(self):
        request, pcb, _ = self.request(via=True)
        for layers in [['F.Cu', 'B.Cu'], ['F.Cu'], ['B.Cu'], ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu', 'In3.Cu']]:
            changed = copy.deepcopy(request)
            changed['endpoints'][1]['copper_layers'] = layers
            with self.subTest(layers=layers), self.assertRaises(ValueError):
                validate_endpoints(changed, pcb)
        changed = copy.deepcopy(request)
        changed['endpoints'][1]['standard_through'] = False
        with self.assertRaises(ValueError):
            validate_endpoints(changed, pcb)
        for kind in ['blind', 'micro']:
            changed = copy.deepcopy(pcb)
            changed.vias[0].via_type = kind
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_endpoints(request, changed)

    def test_via_parsed_size_drill_net_and_position_must_match(self):
        request, pcb, _ = self.request(via=True)
        for key, value in [('size', .81), ('drill', .41), ('net_id', 2), ('x', 7.01)]:
            changed = copy.deepcopy(pcb)
            setattr(changed.vias[0], key, value)
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_endpoints(request, changed)

    def test_via_selector_must_remain_center(self):
        request, pcb, _ = self.request(via=True)
        request['task']['target_endpoints'][1]['selector'] = 'start'
        with self.assertRaises(ValueError):
            validate_endpoints(request, pcb)

    def test_declared_krt_endpoint_job_does_not_call_geometric_fallback(self):
        native, task, _ = self.fixture()
        folder = self.folder/'job'
        folder.mkdir()
        job = SimpleNamespace(folder=folder)
        with patch('pcb_local.candidate_pipeline.engine_command', side_effect=Halt('unsupported', 'sentinel')) as engine, \
                patch('pcb_local.geometry_fallback.supplement') as geometry:
            with self.assertRaises(Halt):
                generate(job, {}, self.board, task, native, {}, 'krt', 'baseline')
            engine.assert_called_once()
            geometry.assert_not_called()

    def test_explicit_geometry_remains_geometry(self):
        native, task, _ = self.fixture()
        job = SimpleNamespace(folder=self.folder)
        with patch('pcb_local.candidate_pipeline.engine_command') as engine, \
                patch('pcb_local.geometry_fallback.supplement', return_value=self.board) as geometry:
            self.assertEqual(generate(job, {}, self.board, task, native, {}, 'geometry', 'baseline'), self.board)
            engine.assert_not_called()
            geometry.assert_called_once()

    def test_native_endpoint_request_accepts_explicit_complete_layer_stack(self):
        native, task, _ = self.fixture(via=True)
        task['permissions'] = dict(schema_version=1, route_layers=list(native['copper_layers']),
                                   max_new_vias=2, via_type='through')
        request = local_pair_request(self.board, task, native)
        self.assertEqual(request['task']['permissions'], task['permissions'])
        self.assertEqual(request['endpoints'][1]['copper_layers'], native['copper_layers'])

    def result_fixture(self, budget=1):
        layers = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
        task = dict(target_net='DATA', allowed_layer='F.Cu', region_mm=[-1., -1., 11., 1.],
                    permissions=dict(schema_version=1, route_layers=layers,
                                     max_new_vias=budget, via_type='through'))
        result = dict(new_segments=[SimpleNamespace(start_x=3., start_y=0., end_x=7.,
                      end_y=0., width=.25, layer='B.Cu', net_id=1)],
                      new_vias=[SimpleNamespace(x=3., y=0., size=.8, drill=.4,
                                 via_type='through', layers=['F.Cu', 'B.Cu'], net_id=1)])
        return result, task, layers

    def test_result_cannot_use_unauthorized_layer(self):
        result, task, layers = self.result_fixture()
        del task['permissions']
        result['new_vias'] = []
        with self.assertRaises(ValueError):
            checked_result(result, 1, task, layers, .25, .8, .4)
        result['new_segments'][0].layer = 'F.Cu'
        self.assertEqual(checked_result(result, 1, task, layers, .25, .8, .4)['vias'], [])

    def test_result_cannot_exceed_original_via_budget(self):
        result, task, layers = self.result_fixture()
        result['new_vias'].append(copy.deepcopy(result['new_vias'][0]))
        with self.assertRaisesRegex(ValueError, 'via budget'):
            checked_result(result, 1, task, layers, .25, .8, .4)
        result['new_vias'].pop()
        task['permissions']['max_new_vias'] = 0
        with self.assertRaisesRegex(ValueError, 'via budget'):
            checked_result(result, 1, task, layers, .25, .8, .4)

    def test_result_new_via_requires_authorization_for_every_crossed_layer(self):
        result, task, layers = self.result_fixture()
        task['permissions']['route_layers'] = ['F.Cu', 'B.Cu']
        with self.assertRaises(ValueError):
            checked_result(result, 1, task, layers, .25, .8, .4)

    def test_result_preserves_net_width_via_type_and_dimensions(self):
        result, task, layers = self.result_fixture()
        for family, key, value in [('new_segments', 'net_id', 2), ('new_segments', 'width', .2),
                                    ('new_segments', 'end_y', float('nan')),
                                    ('new_vias', 'via_type', 'blind'), ('new_vias', 'drill', .41),
                                    ('new_vias', 'size', .81), ('new_vias', 'net_id', 2)]:
            changed = copy.deepcopy(result)
            setattr(changed[family][0], key, value)
            with self.subTest(family=family, key=key), self.assertRaises(ValueError):
                checked_result(changed, 1, task, layers, .25, .8, .4)

    def test_result_cannot_relabel_a_partial_via_as_full_through(self):
        result, task, layers = self.result_fixture()
        result['new_vias'][0].layers = ['F.Cu', 'In1.Cu']
        with self.assertRaises(ValueError):
            checked_result(result, 1, task, layers, .25, .8, .4)

    def test_named_net_track_only_output_keeps_originals_and_context(self):
        self.fixture()
        self.board.write_text(self.board.read_text().replace('(net 1 "DATA")', '').replace('(net 1)', '(net "DATA")'))
        before = read_board(self.board)
        original_text = self.board.read_text()
        result, task, layers = self.result_fixture()
        result = checked_result(result, 1, task, layers, .25, .8, .4)
        made = candidate(self.board, task, result, self.folder/'candidate')
        after = read_board(made)
        self.assertEqual(self.board.read_text(), original_text)
        self.assertEqual(made.with_suffix('.kicad_pro').read_bytes(), self.board.with_suffix('.kicad_pro').read_bytes())
        self.assertEqual(after['static'], before['static'])
        for key, value in before['routing'].items():
            self.assertEqual(canonical(after['routing'][key]), canonical(value))
        added = set(after['routing']) - set(before['routing'])
        self.assertEqual(len(added), 2)
        self.assertEqual({after['identities'][key]['net'] for key in added}, {'DATA'})
        self.assertNotIn('(net 1)', made.read_text())


if __name__ == '__main__':
    unittest.main()
