"""Branch bookkeeping and hard-stop tests; native routing is tested separately."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pcb_local.batch import check_chain
from pcb_local.jobs import Halt, write_json
from pcb_local.joint_repair import JointBudget, JointCoordinator
from pcb_local.workspace import source_files
import test_batch


class JointBatchChecks(unittest.TestCase):
    setUp = test_batch.BatchChecks.setUp
    run_batch = test_batch.BatchChecks.run_batch

    def fake_child(self, args):
        if args.command == 'repair':
            self.assertIsInstance(args._shared_budget, JointBudget)
            self.assertIsInstance(args._candidate_exclusions, set)
            args._shared_budget.charge('generation', 2)
            args._shared_budget.charge('verification', 3)
            if args.target == 'two':
                self.statuses['two'] = getattr(self, 'forced_second_status', None) or (
                    'no_progress' if 'route-a1' in args.board.read_text() else 'accepted')
        code = test_batch.BatchChecks.fake_child(self, args)
        if args.command == 'repair':
            result = json.loads((args.out_dir/'result.json').read_text())
            result['task_dir'] = str(args.out_dir)
            if result['accepted']:
                path = Path(result['candidate'])
                if args.target == 'one':
                    selected = 'a2' if 'a1' in args._candidate_exclusions else 'a1'
                    path.write_text(path.read_text() + '\n# route-' + selected)
                    result['candidate_pool'] = dict(selected_fingerprint=selected,
                        candidates=[dict(fingerprint=key, status='accepted' if selected == key else 'pending')
                                    for key in ('a1', 'a2')])
                result['candidate_hashes'] = source_files(path)
            write_json(args.out_dir/'result.json', result)
        return code

    def joint_args(self):
        self.args.order = 'joint'
        self.args.candidate_strategy = 'pns'

    def test_second_failure_replans_first_route_and_keeps_valid_chain(self):
        self.joint_args()
        code, report = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(report['batch_outcome'], 'all_selected_resolved')
        repairs = [a for a in self.calls if a.command == 'repair']
        self.assertEqual([a.target for a in repairs], ['one', 'two', 'one', 'two'])
        self.assertEqual(repairs[0].board, repairs[2].board)
        self.assertEqual(repairs[2]._candidate_exclusions, {'a1'})
        self.assertEqual(len(report['joint_repair']['backtracks']), 1)
        planning=sum(p['generation_seconds'] for p in report['joint_repair']['planning'] if p['target_id']=='one')
        self.assertAlmostEqual(report['joint_repair']['budgets']['one']['used']['generation'], 4+planning)
        self.assertEqual(report['joint_repair']['budgets']['two']['used']['verification'], 6)
        self.assertEqual(len(report['accepted_chain']), 2)
        self.assertIn('03-repair', report['accepted_chain'][0]['result'])
        check_chain(report)
        self.assertEqual(self.source.read_text(), '(kicad_pcb)')

    def test_protection_failure_does_not_backtrack(self):
        self.joint_args(); self.forced_second_status = 'protection_violation'
        code, report = self.run_batch()
        self.assertEqual(code, 31)
        self.assertEqual(report['joint_repair']['backtracks'], [])
        self.assertEqual([a.target for a in self.calls if a.command == 'repair'], ['one', 'two'])
        self.assertFalse(report['accepted'])

    def test_incomplete_verification_does_not_backtrack(self):
        self.joint_args(); self.forced_second_status = 'verification_incomplete'
        code, report = self.run_batch()
        self.assertFalse(report['accepted'])
        self.assertEqual(report['joint_repair']['backtracks'], [])
        self.assertEqual(report['status'], 'verification_incomplete')

    def test_exhausted_candidates_keep_verified_partial_and_all_failures(self):
        self.joint_args(); self.forced_second_status = 'no_progress'
        code, report = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(report['batch_outcome'], 'partial')
        self.assertEqual(len(report['joint_repair']['backtracks']), 1)
        self.assertEqual(len([a for a in self.calls if a.command == 'repair']), 4)
        self.assertEqual(report['joint_repair']['termination'], 'no_remaining_candidate_within_budget')
        check_chain(report)

    def test_changed_branch_report_invalidates_result(self):
        self.joint_args()
        _, report = self.run_batch()
        path = Path(report['accepted_chain'][0]['result'])
        path.write_text(path.read_text()+' ')
        with self.assertRaises(ValueError): check_chain(report)

    def test_joint_final_chain_can_be_exported_without_discarded_branch(self):
        from pcb_local.delivery import export_result
        self.joint_args()
        _, report = self.run_batch()
        exported = export_result(self.args.out_dir, self.root/'joint-export')
        self.assertEqual(Path(exported['board']).read_bytes(), Path(report['candidate']).read_bytes())
        self.assertNotIn('route-a1', Path(exported['board']).read_text())
        self.assertIn('route-a2', Path(exported['board']).read_text())

    def test_three_target_failed_branch_restores_best_verified_prefix(self):
        self.joint_args(); self.forced_second_status = 'accepted'
        self.targets.append(dict(id='three', net='three', pad_uuids=['three-a','three-b'],
                                 supported=True, region_mm=[0,0,5,5], pads=[]))
        self.args.targets.append('three')
        previous = json.loads(self.inspection.read_text())
        previous['discovery']['targets'] = self.targets
        write_json(self.inspection, previous)
        original = self.fake_child
        def fail_replacement(args):
            self.statuses['three'] = 'no_progress'
            if args.command == 'repair' and args.target == 'one' and 'a1' in args._candidate_exclusions:
                self.statuses['one'] = 'no_progress'
            return original(args)
        with patch.object(self, 'fake_child', side_effect=fail_replacement):
            code, report = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(report['counts'], {'accepted':2, 'no_progress':1})
        self.assertTrue(report['joint_repair']['restored_best_verified_partial'])
        self.assertIn('01-repair', report['accepted_chain'][0]['result'])
        check_chain(report)

    def test_no_candidate_pool_is_reported_without_inventing_routes(self):
        self.joint_args(); self.forced_second_status = 'no_progress'
        original = self.fake_child
        def no_pool(args):
            code = original(args)
            if args.command == 'repair':
                path = args.out_dir/'result.json'; value = json.loads(path.read_text())
                value.pop('candidate_pool', None); write_json(path, value)
            return code
        with patch.object(self, 'fake_child', side_effect=no_pool):
            code, report = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(report['batch_outcome'], 'partial')
        self.assertEqual(report['joint_repair']['backtracks'], [])

    def test_single_target_and_nonpool_strategy_are_explicitly_unsupported(self):
        self.joint_args(); self.args.targets = ['one']
        code, report = self.run_batch()
        self.assertEqual(code, 20); self.assertEqual(self.calls, [])
        with self.assertRaises(Halt): JointCoordinator(['one','two'], 'krt')


class JointBudgetChecks(unittest.TestCase):
    def test_original_target_budget_does_not_reset(self):
        budget = JointBudget()
        budget.charge('generation', 24)
        budget.charge('generation', 36)
        self.assertEqual(budget.remaining('generation'), 0)
        self.assertFalse(budget.available())
        budget.charge('total', 240)
        with self.assertRaises(Halt): budget.check()

    def test_invalid_charges_are_not_budget_credit(self):
        for value in (-1, float('nan'), float('inf'), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                JointBudget().charge('generation', value)

    def test_backtrack_never_spends_an_exhausted_targets_budget(self):
        coordinator = JointCoordinator(['one','two'], 'pns')
        coordinator.frames = [dict(index=1, board='original', target_id='one', snapshot={},
            pool=dict(selected_fingerprint='first', candidates=[dict(fingerprint='first'),dict(fingerprint='other')]))]
        coordinator.budgets['one'].charge('verification', 120)
        self.assertIsNone(coordinator.backtrack({}))
        self.assertEqual(coordinator.evidence['backtracks'], [])

    def test_late_success_exceeding_cumulative_stage_is_rejected(self):
        coordinator = JointCoordinator(['one','two'], 'pns')
        args = SimpleNamespace(_parent_check=lambda: None, board='original')
        def late(_args):
            _args._shared_budget.charge('verification', 120.001)
            return 0
        with self.assertRaises(Halt) as caught:
            coordinator.call(late, args, 'one')
        self.assertEqual(caught.exception.status, 'timed_out')


if __name__ == '__main__': unittest.main()
