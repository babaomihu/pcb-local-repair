"""Fault-injected branch semantics; native DRC remains an independent gate."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcb_local.jobs import Halt
from pcb_local.krt_restore import restore
from pcb_local.workspace import source_files


class RestorationBranches(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = self.root / 'original.kicad_pcb'
        self.original.write_text('(kicad_pcb (net 1 "N") (segment (start 8 8) (end 9 8) (width .25) (layer "F.Cu") (net 1) (uuid "fixed")))')
        self.original.with_suffix('.kicad_pro').write_text('{}')
        self.board = self.root / 'input' / 'board.kicad_pcb'
        self.board.parent.mkdir()
        shutil.copy2(self.original, self.board)
        shutil.copy2(self.original.with_suffix('.kicad_pro'), self.board.with_suffix('.kicad_pro'))
        self.job = SimpleNamespace(folder=self.root / 'job')
        self.job.folder.mkdir()
        self.task = dict(id='target', target_net='T', allowed_layer='F.Cu', region_mm=[-1, -1, 5, 2],
                         modification={'routing_uuids': ['removed']},
                         target_endpoints=[{'must': 'be replaced'}],
                         permissions=dict(schema_version=1, route_layers=['F.Cu', 'B.Cu'],
                                          max_new_vias=2, via_type='through'))
        self.config = dict(kicad_python='native', krt_root='engine')
        self.items = {key: dict(kind='pad', uuid=key, net='N', position_mm=[x, 0.],
                               copper_layers=['F.Cu'], reference=key, number='1')
                      for key, x in [('A', 0.), ('B', 1.), ('C', 3.)]}
        self.native = dict(items=copy.deepcopy(self.items), component_of={key: 0 for key in self.items})
        self.states = {str(self.board): []}
        self.commands = []
        self.requests = {}
        self.report = {}
        self.failed_pairs = set()
        self.block_after_ab = False
        self.add_via_for_ab = False
        self.route_halt = None
        self.summary_status = None
        self.corrupt_anchor = False
        self.break_partition = False
        self.before = source_files(self.original)

    def snapshot(self, board):
        edges = self.states[str(board)]
        components = {key: key for key in self.items}
        for a, b in edges:
            old = components[b]
            new = components[a]
            components = {k: new if v == old else v for k, v in components.items()}
        current = dict(items=copy.deepcopy(self.items), component_of=components)
        if edges and self.corrupt_anchor:
            current['items']['A']['position_mm'][1] += .5
        if edges and self.break_partition:
            current['component_of'] = {'A': 'A', 'B': 'B', 'C': 'B', 'D': 'B', 'E': 'B'}
        return current

    def command(self, config, board, output, task, summary, profile, native):
        self.commands.append((str(board), copy.deepcopy(task)))
        self.requests[str(output)] = copy.deepcopy(task)
        return ['route', str(board), str(output), str(summary)], {}

    def run_stage(self, label, argv, **kwargs):
        if label.startswith('restore-measure'):
            board, measurement = map(Path, argv[-2:])
            measurement.write_text(json.dumps(self.snapshot(board)))
            return 0
        board, output, summary = map(Path, argv[1:])
        if self.route_halt:
            raise Halt(self.route_halt, 'injected deadline/protection')
        task = self.requests[str(output)]
        pair = tuple(task['target_item_ids'])
        previous = self.states[str(board)]
        if self.summary_status:
            summary.write_text(json.dumps(dict(status=self.summary_status, reason='injected critical stop')))
            return 0
        if pair in self.failed_pairs or (self.block_after_ab and previous == [('A', 'B')]):
            summary.write_text(json.dumps(dict(status='no_progress')))
            return 0
        shutil.copy2(board, output)
        shutil.copy2(board.with_suffix('.kicad_pro'), output.with_suffix('.kicad_pro'))
        if pair == ('A', 'B') and self.add_via_for_ab:
            text = output.read_text()
            output.write_text(text[:-1] + '(via (at 0 0) (size .8) (drill .4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-ab")))')
        self.states[str(output)] = previous + [pair]
        summary.write_text(json.dumps(dict(status='proposed')))
        return 0

    def exercise(self):
        with patch('pcb_local.krt_restore.witness', return_value={
                'components': [0], 'anchors': list(self.items)}), \
                patch('pcb_local.krt_restore.command', side_effect=self.command):
            return restore(self.job, self.config, self.original, self.board,
                           self.task, self.native, self.report, self.run_stage, 'baseline')

    def test_failed_nearest_pair_tries_other_fixed_anchors(self):
        self.failed_pairs.add(('A', 'B'))
        result = self.exercise()
        self.assertEqual(len(self.commands), 4)
        self.assertEqual(self.commands[0][0], self.commands[1][0])
        self.assertEqual(self.states[str(result)], [('B', 'C'), ('A', 'C')])
        self.assertEqual(source_files(self.original), self.before)
        self.assertEqual(self.report['neighbour_restoration']['status'], 'proposed')
        self.assertNotIn('accepted', self.report)

    def test_dead_end_backtracks_to_unchanged_parent_board(self):
        self.block_after_ab = True
        result = self.exercise()
        self.assertEqual(self.states[str(result)], [('B', 'C'), ('A', 'B')])
        self.assertEqual(self.commands[3][0], str(self.board))
        log = self.report['neighbour_restoration']
        self.assertEqual(log['search']['backtracks'], 1)
        self.assertEqual(log['selected_attempts'], [3, 4])
        self.assertNotIn(str(self.job.folder / 'neighbour-restoration/step-0/board.kicad_pcb'), log['selected_branch'])

    def test_abandoned_branch_vias_do_not_consume_sibling_permission(self):
        self.block_after_ab = self.add_via_for_ab = True
        self.exercise()
        limits = [child['permissions']['max_new_vias'] for _, child in self.commands]
        self.assertEqual(limits, [2, 1, 1, 2, 2])
        self.assertEqual(self.task['permissions']['max_new_vias'], 2)

    def test_all_misses_are_explicit_no_progress_not_infeasible(self):
        self.failed_pairs.update({('A', 'B'), ('A', 'C'), ('B', 'C')})
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'no_progress')
        self.assertIn('no infeasibility claim', str(caught.exception))
        self.assertEqual(len(self.commands), 3)
        self.assertTrue(all(board == str(self.board) for board, _ in self.commands))

    def test_failed_branch_never_expands_authorization(self):
        self.block_after_ab = True
        self.exercise()
        for _, child in self.commands:
            self.assertEqual(child['region_mm'], self.task['region_mm'])
            self.assertEqual(child['permissions']['route_layers'], self.task['permissions']['route_layers'])
            self.assertLessEqual(child['permissions']['max_new_vias'], 2)
            self.assertNotIn('modification', child)
            self.assertTrue(set(child['target_item_ids']) <= set(self.items))

    def test_deadline_and_cancel_never_trigger_an_alternative(self):
        for status in ['timed_out', 'cancelled']:
            with self.subTest(status=status):
                self.route_halt = status
                self.job.folder = self.root / status
                self.job.folder.mkdir()
                self.commands = []
                with self.assertRaises(Halt) as caught:
                    self.exercise()
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(self.commands), 1)

    def test_critical_summary_status_cannot_be_treated_as_search_miss(self):
        for status in ['protection_violation', 'verification_incomplete', 'timed_out', 'cancelled', 'error']:
            with self.subTest(status=status):
                self.summary_status = status
                self.job.folder = self.root / status
                self.job.folder.mkdir()
                self.commands = []
                with self.assertRaises(Halt) as caught:
                    self.exercise()
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(self.commands), 1)

    def test_changed_fixed_anchor_stops_all_branches(self):
        self.corrupt_anchor = True
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'protection_violation')
        self.assertEqual(len(self.commands), 1)

    def test_decreased_total_score_cannot_hide_split_original_partition(self):
        for key, x in [('D', 3.5), ('E', 4.)]:
            self.items[key] = dict(kind='pad', uuid=key, net='N', position_mm=[x, 0.],
                                  copper_layers=['F.Cu'], reference=key, number='1')
            self.native['component_of'][key] = 0
        self.states[str(self.board)] = [('A', 'B')]
        self.break_partition = True
        original_snapshot = self.snapshot
        def measured(board):
            if board == self.board:
                self.break_partition = False
                try:
                    return original_snapshot(board)
                finally:
                    self.break_partition = True
            return original_snapshot(board)
        with patch.object(self, 'snapshot', side_effect=measured):
            with self.assertRaises(Halt) as caught:
                self.exercise()
        self.assertEqual(caught.exception.status, 'protection_violation')
        self.assertEqual(len(self.commands), 1)

    def test_bounded_alternatives_stop_without_restarting_time_budget(self):
        for index in range(5):
            key = 'X%d' % index
            self.items[key] = dict(kind='pad', uuid=key, net='N', position_mm=[3.1 + index * .1, 0.],
                                  copper_layers=['F.Cu'], reference=key, number='1')
            self.native['component_of'][key] = 0
        keys = list(self.items)
        self.failed_pairs.update((a, b) for i, a in enumerate(keys) for b in keys[i + 1:])
        with self.assertRaises(Halt) as caught:
            self.exercise()
        self.assertEqual(caught.exception.status, 'no_progress')
        log = self.report['neighbour_restoration']
        self.assertEqual(len(self.commands), (len(self.items) - 1) + 8)
        self.assertEqual(log['search']['attempts'], log['search']['max_attempts'])


if __name__ == '__main__':
    unittest.main()
