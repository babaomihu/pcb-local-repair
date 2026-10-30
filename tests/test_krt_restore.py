"""Fault injection for bounded neighbour restoration, never electrical proof."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcb_local.auto_strategy import plans
from pcb_local.candidate_pipeline import generate
from pcb_local.jobs import Halt
from pcb_local.krt_restore import restore
from pcb_local.validation import read_board
from pcb_local.workspace import source_files


class KRTRestoration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.baseline = self.folder/'original.kicad_pcb'
        self.baseline.with_suffix('.kicad_pro').write_text('{}')
        self.baseline.with_suffix('.kicad_dru').write_text('(version 1)')
        self.task = dict(id='repair', target_net='DATA', allowed_layer='F.Cu',
                         region_mm=[-1., -1., 4., 3.], target_item_ids=['target-left', 'target-right'],
                         target_endpoints=[{'native': 'placeholder'}],
                         modification=dict(schema_version=1, mode='ripup', routing_uuids=['remove'],
                                           nets=['NEIGHBOUR'], max_modified_tracks=1),
                         permissions=dict(schema_version=1, route_layers=['F.Cu', 'B.Cu'],
                                          max_new_vias=2, via_type='through'))
        def line(uid, net, a, b, locked=False):
            return dict(kind='segment', uuid=uid, net=net, start_mm=a, end_mm=b,
                        position_mm=a, width_mm=.25, copper_layers=['F.Cu'], locked=locked)
        self.items = {
            'left': line('left', 'NEIGHBOUR', [0., 0.], [1., 0.], True),
            'remove': line('remove', 'NEIGHBOUR', [1., 0.], [2., 0.]),
            'right': line('right', 'NEIGHBOUR', [2., 0.], [3., 0.], True),
            'target-left': line('target-left', 'DATA', [0., 2.], [.5, 2.], True),
            'target-right': line('target-right', 'DATA', [2.5, 2.], [3., 2.], True)}
        self.native = dict(items=copy.deepcopy(self.items), copper_layers=['F.Cu', 'B.Cu'],
                           components=[dict(net='NEIGHBOUR', items=['left', 'remove', 'right']),
                                       dict(net='DATA', items=['target-left']),
                                       dict(net='DATA', items=['target-right'])],
                           component_of={'left': 0, 'remove': 0, 'right': 0,
                                         'target-left': 1, 'target-right': 2})
        self.baseline.write_text('(kicad_pcb (net 1 "NEIGHBOUR") (net 2 "DATA")\n'+
                                 '\n'.join(self.token(row) for row in self.items.values())+'\n)')
        folder = self.folder/'candidate'
        folder.mkdir()
        self.candidate = folder/'board.kicad_pcb'
        self.candidate.write_text(self.baseline.read_text().replace(self.token(self.items['remove']), ''))
        for suffix in ['.kicad_pro', '.kicad_dru']:
            shutil.copy2(self.baseline.with_suffix(suffix), self.candidate.with_suffix(suffix))
        job_folder = self.folder/'job'
        job_folder.mkdir()
        self.job = SimpleNamespace(folder=job_folder)
        self.config = dict(kicad_python='native', krt_root='upstream')
        self.report = {}
        self.commands = []
        self.measurements = 0
        self.route_count = 0
        self.mode = 'success'
        self.before = source_files(self.baseline)

    @staticmethod
    def token(row):
        return '(segment (start %g %g) (end %g %g) (width .25) (layer "F.Cu") (net %s) (uuid "%s"))' % (
            *row['start_mm'], *row['end_mm'], '1' if row['net']=='NEIGHBOUR' else '2', row['uuid'])

    def measurement(self):
        value = copy.deepcopy(self.native)
        del value['items']['remove']
        connected = self.mode in {'already-connected', 'measure-mutation-connected'} or (
            self.route_count > 0 and self.mode != 'no-merge')
        value['component_of'] = {'left': 0, 'right': 0 if connected else 1,
                                 'target-left': 2, 'target-right': 2}
        value['components'] = [dict(net='NEIGHBOUR', items=['left', 'right'])] if connected else [
            dict(net='NEIGHBOUR', items=['left']), dict(net='NEIGHBOUR', items=['right'])]
        value['components'].append(dict(net='DATA', items=['target-left', 'target-right']))
        if self.mode == 'missing-anchor':
            value['component_of'].pop('left')
            value['items'].pop('left')
        if self.mode == 'other-layer':
            for key in ['left', 'right']:
                value['items'][key]['copper_layers'] = ['B.Cu']
        return value

    def command(self, config, board, output, task, summary, profile, native):
        self.commands.append(copy.deepcopy(task))
        return ['fake-route', str(board), str(output), str(summary)], {}

    def run_stage(self, label, argv, **kwargs):
        if self.mode in {'timed_out', 'cancelled'}:
            raise Halt(self.mode, 'parent deadline/cancellation')
        if label.startswith('restore-measure'):
            self.measurements += 1
            if self.mode == 'missing-measurement':
                return 0
            if self.mode == 'measure-mutation-connected':
                Path(argv[-2]).write_text(Path(argv[-2]).read_text()+'\n')
            Path(argv[-1]).write_text(json.dumps(self.measurement()))
            return 0
        self.route_count += 1
        board, output, summary = map(Path, argv[1:])
        if self.mode == 'router-mutation':
            board.write_text(board.read_text()+'\n')
        if self.mode in {'no-output', 'unsupported'}:
            summary.write_text(json.dumps(dict(status='unsupported' if self.mode=='unsupported' else 'no_progress', reason='fixture refused')))
            return 2 if self.mode=='unsupported' else 0
        shutil.copy2(board, output)
        for suffix in ['.kicad_pro', '.kicad_dru']:
            shutil.copy2(board.with_suffix(suffix), output.with_suffix(suffix))
        # This is only a transaction mock. The native suite must establish that
        # the added copper really connects both original anchors under DRC.
        text=output.read_text();output.write_text(text[:-1]+'\n(segment (start 1 0) (end 2 0) (width .25) (layer "F.Cu") (net 1) (uuid "restored"))\n)')
        summary.write_text(json.dumps(dict(status='proposed')))
        return 0

    def exercise(self):
        with patch('pcb_local.krt_restore.command', side_effect=self.command):
            return restore(self.job, self.config, self.baseline, self.candidate,
                           self.task, self.native, self.report, self.run_stage, 'baseline')

    def test_restores_only_original_group_using_permission_subset(self):
        result = self.exercise()
        self.assertNotEqual(result, self.candidate)
        self.assertEqual(self.route_count, 1)
        self.assertEqual(self.measurements, 2)
        child = self.commands[0]
        self.assertEqual(child['target_net'], 'NEIGHBOUR')
        self.assertEqual(set(child['target_item_ids']), {'left', 'right'})
        self.assertEqual(child['region_mm'], self.task['region_mm'])
        self.assertEqual(child['permissions'], self.task['permissions'])
        self.assertNotIn('modification', child)
        self.assertNotIn('zone_refill', child)
        self.assertEqual(self.report['neighbour_restoration']['status'], 'proposed')
        self.assertNotIn('accepted', self.report)
        self.assertEqual(source_files(self.baseline), self.before)

    def test_connected_anchors_need_no_additional_route(self):
        self.mode = 'already-connected'
        self.assertEqual(self.exercise(), self.candidate)
        self.assertEqual(self.route_count, 0)

    def test_missing_original_anchor_is_protection_failure(self):
        self.mode = 'missing-anchor'
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'protection_violation')
        self.assertEqual(self.route_count, 0)

    def test_missing_native_measurement_is_not_no_progress(self):
        self.mode = 'missing-measurement'
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'verification_incomplete')

    def test_no_anchor_on_task_layer_is_explicitly_unsupported(self):
        self.mode = 'other-layer'
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'unsupported')
        self.assertEqual(self.route_count, 0)

    def test_measurement_mutation_rejected_even_if_groups_already_connected(self):
        self.mode = 'measure-mutation-connected'
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'protection_violation')
        self.assertEqual(self.route_count, 0)

    def test_router_input_mutation_is_protection_failure(self):
        self.mode = 'router-mutation'
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'protection_violation')

    def test_child_via_budget_deducts_target_additions(self):
        text = self.candidate.read_text()
        self.candidate.write_text(text[:-1]+'(via (at .5 2) (size .8) (drill .4) (layers "F.Cu" "B.Cu") (net 2) (uuid "target-via")))')
        self.exercise()
        self.assertEqual(self.commands[0]['permissions']['max_new_vias'], 1)
        self.assertEqual(self.task['permissions']['max_new_vias'], 2)

    def test_target_candidate_cannot_already_exceed_via_budget(self):
        self.task['permissions']['max_new_vias'] = 0
        text = self.candidate.read_text()
        self.candidate.write_text(text[:-1]+'(via (at .5 2) (size .8) (drill .4) (layers "F.Cu" "B.Cu") (net 2) (uuid "target-via")))')
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'protection_violation')
        self.assertEqual(self.route_count, 0)

    def test_failed_restoration_does_not_return_target_only_candidate(self):
        self.mode = 'no-output'
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'no_progress')
        self.assertEqual(source_files(self.baseline), self.before)

    def test_unsupported_child_remains_unsupported(self):
        self.mode = 'unsupported'
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'unsupported')

    def test_connected_candidate_claim_cannot_hide_no_native_merge(self):
        self.mode = 'no-merge'
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'no_progress')
        self.assertEqual(self.route_count, 1)
        self.assertFalse(self.report['neighbour_restoration'].get('all_anchor_groups_connected'))

    def test_original_deadline_and_cancel_are_propagated_without_retry(self):
        for mode in ['timed_out', 'cancelled']:
            # Independent folders prevent a retry from overwriting evidence.
            folder = self.folder/mode
            folder.mkdir()
            self.job = SimpleNamespace(folder=folder)
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(Halt) as caught:
                self.exercise()
            self.assertEqual(caught.exception.status, mode)
            self.assertEqual(self.route_count, 0)

    def test_failure_log_does_not_remain_running(self):
        self.mode = 'unsupported'
        with self.assertRaises(Halt):
            self.exercise()
        saved = json.loads((self.job.folder/'neighbour-restoration/restoration.json').read_text())
        self.assertEqual(saved['status'], 'unsupported')
        self.assertEqual(self.report['neighbour_restoration']['status'], 'unsupported')

    def test_auto_endpoint_strategies_keep_original_authorization(self):
        original = copy.deepcopy(self.task)
        rows = plans(self.task, True)
        self.assertIn('ripup', [label for label, _, _ in rows])
        self.assertIn('multilayer', [label for label, _, _ in rows])
        for label, strategy, child in rows:
            self.assertEqual(child['region_mm'], self.task['region_mm'])
            self.assertEqual(child['target_item_ids'], self.task['target_item_ids'])
            self.assertTrue(set(child['permissions']['route_layers']) <= set(self.task['permissions']['route_layers']))
            self.assertLessEqual(child['permissions']['max_new_vias'], self.task['permissions']['max_new_vias'])
            if label in {'ripup', 'multilayer'}:
                self.assertEqual(child['modification']['routing_uuids'], ['remove'])
            else:
                self.assertNotIn('modification', child)
        self.assertEqual(self.task, original)

    def test_pipeline_shares_one_generation_deadline_with_restoration(self):
        elapsed = [0.]
        dispatched = []
        def execute(label, argv, **kwargs):
            dispatched.append((label, kwargs['timeout']))
            if label == 'routing':
                shutil.copy2(self.candidate, self.job.folder/'engine-output/board.kicad_pcb')
            elapsed[0] += 20.
            return 0
        self.job.run = execute
        def restore_using_parent_runner(job, config, baseline, candidate, task, native, report, run, profile):
            run('restore-measure', ['native'])
            run('restore-route', ['router'])
            # The combined target + measurement + restoration exhausted the
            # original 60 seconds. A fourth child must never be dispatched.
            run('forbidden-extra-route', ['router'])
            return candidate
        with patch('pcb_local.candidate_pipeline.time.monotonic', side_effect=lambda: elapsed[0]), \
                patch('pcb_local.candidate_pipeline.engine_command', return_value=(['router'], {})), \
                patch('pcb_local.krt_restore.restore', side_effect=restore_using_parent_runner):
            with self.assertRaises(Halt) as caught:
                generate(self.job, self.config, self.baseline, self.task, self.native,
                         {}, 'krt', 'baseline', budget=60)
        self.assertEqual(caught.exception.status, 'timed_out')
        self.assertEqual(dispatched, [('routing', 60.), ('restore-measure', 40.), ('restore-route', 20.)])


if __name__ == '__main__':
    unittest.main()
