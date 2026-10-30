"""Cross-engine branch orchestration; no mocked gate is physical evidence."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pcb_local.auto_strategy import plans, repair, strategy_token
from pcb_local.batch import check_chain
from pcb_local.jobs import Halt, write_json
from pcb_local.joint_repair import JointCoordinator
from pcb_local.workspace import copy_project, source_files
from test_scope_candidate import BASE, RULES, TASK, segment
import test_batch


class CrossEngineStrategyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.board=self.root/'board.kicad_pcb';self.board.write_text(BASE)
        self.board.with_suffix('.kicad_pro').write_text(json.dumps(RULES))
        self.task=copy.deepcopy(TASK)
        self.config={'pns':{'configured':True},'source_fingerprint':{'engine':'known'},'verified_kicad_version':'10.0.4'}

    def job(self,label):
        folder=self.root/label;folder.mkdir();(folder/'netclasses.native.json').write_text('{}')
        return SimpleNamespace(folder=folder,check=lambda:None)

    def candidate(self,job,end=(5,1)):
        path,_=copy_project(self.board,job.folder/'candidate')
        path.write_text(BASE[:-1]+segment(job.label+'-new-uuid',end=end)+')')
        return path

    def route(self,label,generate,exclusions=(),board=None):
        report={'preflight':{},'candidate_exclusions':list(exclusions)}
        calls=[]
        def validate(job,*a,**kw):
            calls.append(job.label)
            return 0,{'verification_complete':True,'accepted':True}
        with patch('pcb_local.auto_strategy.witness'),patch('pcb_local.auto_strategy.generate',side_effect=generate),patch('pcb_local.auto_strategy.validate',side_effect=validate):
            result=repair(self.job(label),self.config,board or self.board,self.task,{},report,'quick')
        return report,calls,result

    def test_lazy_branches_do_not_claim_to_be_candidates(self):
        report,calls,_=self.route('first',lambda job,*a,**kw:self.candidate(job))
        self.assertEqual(calls,['add-only'])
        self.assertNotIn('candidate_pool',report)
        branches=report['strategy_pool']['branches']
        self.assertEqual([r['status'] for r in branches],['accepted','untried','untried'])
        self.assertTrue(report['strategy_pool']['selected_token'].startswith('strategy-v1:'))
        self.assertTrue(report['strategy_pool']['selected_candidate_fingerprint'])

    def test_tokens_survive_snapshot_copy_and_bind_rules_permissions_and_runtime(self):
        copied,_=copy_project(self.board,self.root/'copied')
        label,engine,child=plans(self.task,True)[0]
        def token(board,task=self.task,config=self.config,profile='quick'):
            return strategy_token(board,source_files(board),task,child,label,engine,config,profile)
        original=token(self.board)
        self.assertEqual(original,token(copied))
        self.assertNotEqual(original,token(copied,task=dict(self.task,region_mm=[0,0,20,20])))
        self.assertNotEqual(original,token(copied,config=dict(self.config,source_fingerprint={'engine':'changed'})))
        self.assertNotEqual(original,token(copied,profile='different'))
        copied.with_suffix('.kicad_dru').write_text('(version 1)')
        self.assertNotEqual(original,token(copied))

    def test_backtrack_excludes_krt_and_geometry_duplicate_then_uses_pns(self):
        report,_,_=self.route('first',lambda job,*a,**kw:self.candidate(job))
        coordinator=JointCoordinator(['one','two'],'auto')
        batch=dict(items=[dict(target={'id':'one'},status='accepted'),dict(target={'id':'two'},status='no_progress')],
                   accepted_chain=[],candidate=None,execution_order=['one','two'])
        checkpoint=coordinator.before(batch,1,self.board)
        coordinator.record(checkpoint,batch,dict(report,status='accepted',accepted=True,exit_code=0))
        retry=coordinator.backtrack(batch)
        self.assertEqual(retry,(1,str(self.board)))
        exclusions=coordinator.excluded('one',self.board)
        self.assertEqual(exclusions,{report['strategy_pool']['selected_token'],report['strategy_pool']['selected_candidate_fingerprint']})
        copied,_=copy_project(self.board,self.root/'fresh-snapshot')
        generated=[]
        def generate(job,*a,**kw):
            generated.append(job.label)
            return self.candidate(job,end=(6,1) if job.label=='pns-fixed' else (5,1))
        second,calls,_=self.route('second',generate,exclusions,copied)
        self.assertEqual(generated,['geometry','pns-fixed'])
        self.assertEqual(calls,['pns-fixed'])
        self.assertEqual(second['automatic_strategy']['selected'],'pns-fixed')
        self.assertEqual([r['status'] for r in second['strategy_pool']['branches']],['excluded_branch','excluded_branch','accepted'])
        self.assertEqual(coordinator.evidence['backtracks'][0]['alternative_kind'],'strategy')
        self.assertEqual(self.board.read_text(),BASE)

    def test_native_failure_stops_cross_engine_attempt(self):
        generated=[]
        def generate(job,*a,**kw):generated.append(job.label);return self.candidate(job)
        for status,result in [('protection_violation',{'verification_complete':True,'checks':{'existing_components_preserved':False}}),
                              ('verification_incomplete',{'verification_complete':False})]:
            generated.clear()
            report={'preflight':{}}
            with patch('pcb_local.auto_strategy.witness'),patch('pcb_local.auto_strategy.generate',side_effect=generate),patch('pcb_local.auto_strategy.validate',return_value=(1,result)),self.assertRaises(Halt) as caught:
                repair(self.job(status),self.config,self.board,self.task,{},report,'quick')
            self.assertEqual(caught.exception.status,status)
            self.assertEqual(generated,['add-only'])

    def test_actual_candidates_precede_untried_strategies(self):
        coordinator=JointCoordinator(['one','two'],'auto')
        frame=dict(index=1,board='original',target_id='one',snapshot={},
                   pool=dict(selected_fingerprint='pns-a',candidates=[dict(fingerprint='pns-a',status='accepted'),
                        dict(fingerprint='pns-b',status='pending'),dict(fingerprint='pns-rejected',status='rejected')]),
                   strategies=dict(selected_token='strategy-v1:pns',selected_candidate_fingerprint='pns-a',branches=[
                        dict(token='strategy-v1:krt',status='no_progress'),dict(token='strategy-v1:pns',status='accepted'),
                        dict(token='strategy-v1:multilayer',status='untried')]))
        coordinator.frames=[copy.deepcopy(frame)]
        self.assertEqual(coordinator.backtrack({}),(1,'original'))
        excluded=coordinator.excluded('one','original')
        self.assertEqual(excluded,{'pns-a','pns-rejected','strategy-v1:krt'})
        self.assertEqual(coordinator.evidence['backtracks'][-1]['alternatives'],['pns-b'])
        frame['pool']['selected_fingerprint']='pns-b'
        frame['strategies']['selected_candidate_fingerprint']='pns-b'
        coordinator.frames=[frame]
        self.assertEqual(coordinator.backtrack({}),(1,'original'))
        self.assertIn('strategy-v1:pns',coordinator.excluded('one','original'))
        self.assertEqual(coordinator.evidence['backtracks'][-1]['alternatives'],['strategy-v1:multilayer'])

    def test_exhausted_original_budget_prevents_switching_engines(self):
        coordinator=JointCoordinator(['one','two'],'auto')
        coordinator.frames=[dict(index=1,board='original',target_id='one',snapshot={},pool={},strategies=dict(
            selected_candidate_fingerprint='krt-a',selected_token='strategy-v1:krt',branches=[dict(token='strategy-v1:geometry',status='untried')]))]
        coordinator.budgets['one'].charge('generation',60)
        with self.assertRaises(Halt) as caught:coordinator.backtrack({})
        self.assertEqual(caught.exception.status,'timed_out')
        self.assertEqual(coordinator.evidence['backtracks'],[])


class CrossEngineBatchTests(unittest.TestCase):
    setUp=test_batch.BatchChecks.setUp
    run_batch=test_batch.BatchChecks.run_batch

    def fake_child(self,args):
        if args.command=='repair':
            args._shared_budget.charge('generation',2)
            args._shared_budget.charge('verification',3)
            if args.target=='two':
                self.statuses['two']='no_progress' if '# krt-route' in args.board.read_text() else 'accepted'
        code=test_batch.BatchChecks.fake_child(self,args)
        if args.command=='repair':
            path=args.out_dir/'result.json';result=json.loads(path.read_text())
            if result['accepted'] and args.target=='one':
                selected='geometry' if 'strategy-v1:krt' in args._candidate_exclusions else 'krt'
                candidate=Path(result['candidate']);candidate.write_text(candidate.read_text()+'\n# '+selected+'-route')
                result['candidate_hashes']=source_files(candidate)
                result['strategy_pool']=dict(selected_token='strategy-v1:'+selected,selected_candidate_fingerprint=selected+'-route',
                    branches=[dict(token='strategy-v1:krt',status='accepted' if selected=='krt' else 'excluded_branch'),
                              dict(token='strategy-v1:geometry',status='accepted' if selected=='geometry' else 'untried')])
            write_json(path,result)
        return code

    def test_batch_changes_engine_with_original_budget_and_verified_chain(self):
        self.args.order='joint';self.args.candidate_strategy='auto'
        code,report=self.run_batch()
        self.assertEqual(code,0)
        self.assertEqual(report['batch_outcome'],'all_selected_resolved')
        calls=[c for c in self.calls if c.command=='repair']
        self.assertEqual([c.target for c in calls],['one','two','one','two'])
        self.assertEqual(calls[0].board,calls[2].board)
        self.assertEqual(calls[2]._candidate_exclusions,{'strategy-v1:krt','krt-route'})
        planning=sum(p['generation_seconds'] for p in report['joint_repair']['planning'] if p['target_id']=='one')
        self.assertAlmostEqual(report['joint_repair']['budgets']['one']['used']['generation'],4+planning)
        self.assertEqual(report['joint_repair']['budgets']['two']['used']['verification'],6)
        self.assertEqual(report['joint_repair']['backtracks'][0]['alternative_kind'],'strategy')
        self.assertNotIn('# krt-route',Path(report['candidate']).read_text())
        self.assertIn('# geometry-route',Path(report['candidate']).read_text())
        check_chain(report)
        self.assertEqual(self.source.read_text(),'(kicad_pcb)')


if __name__=='__main__':unittest.main()
