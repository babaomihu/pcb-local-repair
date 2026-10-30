import copy
import json
from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_local.batch import batch_workflow, check_chain, match_target
from pcb_local.jobs import Job, Halt, write_json
from pcb_local.workspace import source_files


class BatchChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'original.kicad_pcb'
        self.source.write_text('(kicad_pcb)')
        self.source.with_suffix('.kicad_pro').write_text('{}')
        self.targets = [dict(id=n, net=n, pad_uuids=[n+'a', n+'b'], supported=True,
                             region_mm=[0, 0, 5, 5], pads=[]) for n in ('one', 'two')]
        self.listing = dict(mode='automatic-regions', allowed_layer='F.Cu',
                            automatic_region_policy={'name': 'test'}, targets=self.targets)
        self.inspection = self.root / 'inspection.json'
        write_json(self.inspection, dict(status='complete', source_files_untouched=True,
                   source_board=str(self.source), source_hashes=source_files(self.source), discovery=self.listing))
        self.args = Namespace(command='batch', targets=['one', 'two'], out_dir=self.root/'batch',
            board=self.source, inspection=self.inspection, auto_region=True, roi=None,
            layer='F.Cu', config=self.root/'unused.json', profile='external-pour-check', signal_kind='ordinary-single-ended')
        self.calls = []
        self.statuses = {}
        self.fresh_targets = self.targets
        self.cancel_inspect = False
        self.fresh_proposals = []

    def fake_child(self, args):
        self.calls.append(args)
        args.out_dir.mkdir()
        if args.command == 'inspect':
            fresh = copy.deepcopy(self.listing); fresh['targets'] = self.fresh_targets
            native = {'items': {}, 'component_of': {}}
            for t in self.targets:
                for uid in t['pad_uuids']:
                    native['items'][uid] = dict(kind='pad', uuid=uid, net=t['net'])
                    native['component_of'][uid] = uid
            result = dict(status='complete', discovery=fresh, preflight={'measurement':{'snapshot':native}},
                          modification_recommendations=copy.deepcopy(self.fresh_proposals))
            if self.cancel_inspect:
                (self.args.out_dir/'cancel.request').touch()
            code = 0
        else:
            status = self.statuses.get(args.target, 'accepted')
            code = 0 if status == 'accepted' else 31 if status == 'protection_violation' else 10
            result = dict(status=status, accepted=status=='accepted', exit_code=code,
                          source_board=str(args.board), source_hashes=source_files(args.board), reason='test')
            if status == 'accepted':
                dest = args.out_dir/'scoping/candidate/board.kicad_pcb';dest.parent.mkdir(parents=True)
                dest.write_text(args.board.read_text()+'\n')
                dest.with_suffix('.kicad_pro').write_text('{}')
                result.update(candidate=str(dest), candidate_hashes=source_files(dest))
        write_json(args.out_dir/'result.json', result)
        return code

    def run_batch(self):
        with patch('pcb_local.cli.workflow', side_effect=self.fake_child):
            code = batch_workflow(self.args)
        return code, json.loads((self.args.out_dir/'result.json').read_text())

    def test_batch_rebinds_exact_user_reviewed_proposal_before_repair(self):
        proposal=dict(id='proposal',target_id='one',objects=[dict(uuid='actual')],region_mm=[0,0,5,5])
        self.fresh_proposals=[proposal]
        r=json.loads(self.inspection.read_text())
        r['modification_recommendations']=[dict(proposal,objects=[dict(uuid='different-reviewed-object')])]
        write_json(self.inspection,r)
        self.args.accept_recommendation=['proposal'];self.args.recommendation_mode='shove'
        code,report=self.run_batch()
        self.assertEqual(code,20);self.assertEqual(report['status'],'unsupported')
        self.assertFalse(any(c.command=='repair' for c in self.calls))
        self.assertEqual(report['reviewed_recommendations'],r['modification_recommendations'])
        self.assertEqual(report['items'][1]['status'],'not_run')

    def test_unchanged_exact_proposal_is_forwarded_only_to_its_target(self):
        self.fresh_proposals=[dict(id='proposal',target_id='one',objects=[dict(uuid='actual')])]
        r=json.loads(self.inspection.read_text());r['modification_recommendations']=self.fresh_proposals
        write_json(self.inspection,r);self.args.accept_recommendation=['proposal']
        code,report=self.run_batch();self.assertEqual(code,0)
        self.assertEqual([c.accept_recommendation for c in self.calls if c.command=='repair'],[['proposal'],None])

    def test_success_accumulates_only_accepted_candidate(self):
        code, report = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(report['batch_outcome'], 'all_selected_resolved')
        repairs = [c for c in self.calls if c.command=='repair']
        self.assertEqual(repairs[0].board, self.source)
        self.assertEqual(repairs[1].board, repairs[0].out_dir/'scoping/candidate/board.kicad_pcb')
        self.assertEqual(len(report['accepted_chain']), 2)
        check_chain(report)
        self.assertEqual(self.source.read_text(), '(kicad_pcb)')

    def test_failed_item_does_not_advance_input(self):
        self.statuses['one'] = 'no_progress'
        code, report = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(report['batch_outcome'], 'partial')
        self.assertEqual([c.board for c in self.calls if c.command=='repair'], [self.source, self.source])
        self.assertEqual(len(report['accepted_chain']), 1)

    def test_protection_failure_stops_remaining(self):
        self.statuses['one'] = 'protection_violation'
        code, report = self.run_batch()
        self.assertEqual(code, 31)
        self.assertFalse(report['accepted'])
        self.assertEqual(report['items'][1]['status'], 'not_run')
        self.assertEqual(len(self.calls), 2)

    def test_target_changed_never_substitutes_other_target(self):
        self.fresh_targets = [dict(self.targets[0], region_mm=[-1, -1, 6, 6]), self.targets[1]]
        code, report = self.run_batch()
        self.assertEqual(report['items'][0]['status'], 'target_changed')
        self.assertEqual([c.target for c in self.calls if c.command=='repair'], ['two'])

    def test_cancel_between_inspect_and_repair(self):
        self.cancel_inspect = True
        code, report = self.run_batch()
        self.assertEqual(code, 130)
        self.assertFalse(report['accepted'])
        self.assertEqual(len(self.calls), 1)

    def test_stale_original_never_starts(self):
        self.source.write_text('(kicad_pcb)\n')
        code, report = self.run_batch()
        self.assertEqual(code, 20)
        self.assertEqual(self.calls, [])

    def test_unselected_targets_not_attempted(self):
        self.args.targets = ['two']
        code, report = self.run_batch()
        self.assertEqual([c.target for c in self.calls if c.command=='repair'], ['two'])

    def test_duplicate_and_unknown_targets_reject(self):
        self.args.targets = ['one', 'one']
        with self.assertRaises(ValueError): self.run_batch()
        self.args.targets = ['unknown']
        code, report = self.run_batch()
        self.assertEqual(code, 20)
        self.assertEqual(self.calls, [])

    def test_intermediate_changes_invalidate_chain(self):
        _, report = self.run_batch()
        child = json.loads(Path(report['accepted_chain'][0]['result']).read_text())
        Path(child['candidate']).write_text('(kicad_pcb)\n\n\n')
        with self.assertRaises(ValueError): check_chain(report)

    def test_child_report_changes_invalidate_chain(self):
        _, report = self.run_batch()
        p=Path(report['accepted_chain'][0]['result']);p.write_text(p.read_text()+' ')
        with self.assertRaises(ValueError): check_chain(report)

    def test_already_connected_is_skipped(self):
        t=self.targets[0];native={'items':{}, 'component_of':{}}
        for uid in t['pad_uuids']:
            native['items'][uid]=dict(kind='pad',uuid=uid,net=t['net'])
            native['component_of'][uid]='same'
        self.assertEqual(match_target(t, {'targets':[]}, native), 'already_connected')

    def test_child_job_observes_parent_cancel(self):
        parent=Job(self.root/'parent')
        child=Job(self.root/'child',parent_check=parent.check)
        try:
            (parent.folder/'cancel.request').touch()
            with self.assertRaises(Halt) as cm:child.check()
            self.assertEqual(cm.exception.status,'cancelled')
        finally:
            child.finish('cancelled');parent.finish('cancelled')

    def test_nonzero_child_cannot_leave_accepted_batch(self):
        original = self.fake_child
        def contradictory(args):
            code = original(args)
            return 50 if args.command == 'repair' else code
        with patch('pcb_local.cli.workflow', side_effect=contradictory):
            code = batch_workflow(self.args)
        result = json.loads((self.args.out_dir/'result.json').read_text())
        self.assertEqual(code, 50)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['items'][0]['status'],'error')
        self.assertNotIn('accepted',result['counts'])
        self.assertEqual(len(self.calls), 2)

if __name__ == '__main__': unittest.main()
