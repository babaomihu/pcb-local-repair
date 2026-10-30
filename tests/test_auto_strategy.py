"""Portfolio safety/termination tests; physical routing is tested separately."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pcb_local.auto_strategy import plans,repair
from pcb_local.jobs import Halt
from pcb_local.workspace import copy_project
from test_scope_candidate import BASE,RULES,TASK,segment


class AutoStrategyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.board=self.root/'board.kicad_pcb';self.board.write_text(BASE)
        self.board.with_suffix('.kicad_pro').write_text(json.dumps(RULES))
        folder=self.root/'job';folder.mkdir();(folder/'netclasses.native.json').write_text('{}')
        self.job=SimpleNamespace(folder=folder,check=lambda:None)
        self.task=copy.deepcopy(TASK)
        self.task['modification']=dict(schema_version=1,mode='shove+ripup',routing_uuids=['old'],nets=['N'],max_modified_tracks=1)
        self.report={'preflight':{}}

    def candidate(self,job):
        p,_=copy_project(self.board,job.folder/'candidate')
        p.write_text(p.read_text()[:-1]+segment('proposal')+')')
        return p

    def test_plans_never_grant_unrequested_actions(self):
        rows=plans(TASK,True)
        self.assertEqual([x[0] for x in rows],['add-only','geometry','pns-fixed'])
        self.assertTrue(all('modification' not in x[2] for x in rows))
        for mode,expected in [('shove',['add-only','geometry','push']),('ripup',['add-only','geometry','pns-fixed','ripup'])]:
            task=copy.deepcopy(self.task);task['modification']['mode']=mode
            self.assertEqual([x[0] for x in plans(task,True)],expected)
        self.assertEqual([x[0] for x in plans(self.task,False)],['add-only','geometry','ripup'])

    def test_multilayer_appears_only_with_original_layer_via_permission(self):
        task=copy.deepcopy(self.task);task['permissions']=dict(schema_version=1,route_layers=['F.Cu','B.Cu'],max_new_vias=2,via_type='through')
        rows=plans(task,True)
        self.assertEqual([r[0] for r in rows[-2:]],['multilayer','geometry-multilayer'])
        self.assertEqual(rows[-1][2]['permissions'],task['permissions'])
        self.assertNotIn('modification',rows[-1][2])
        self.assertTrue(all(x[2]['permissions']['max_new_vias']==0 for x in rows[:-2]))
        self.assertEqual(task['modification']['mode'],'shove+ripup')

    def exercise(self,generate,validate):
        with patch('pcb_local.auto_strategy.witness'),patch('pcb_local.auto_strategy.generate',side_effect=generate),patch('pcb_local.auto_strategy.validate',side_effect=validate):
            return repair(self.job,{'pns':{}},self.board,self.task,{},self.report,'quick')

    def test_rejected_proposal_never_becomes_next_baseline(self):
        seen=[]
        def gen(job,config,board,task,*args,**kwargs):
            seen.append(board.read_text());return self.candidate(job)
        failures={'verification_complete':True,'accepted':False,'checks':{'full_drc_no_new_or_changed':False},'protection':{'checks':{}}}
        passed={'verification_complete':True,'accepted':True}
        outcomes=iter([(1,failures),(0,passed)])
        self.exercise(gen,lambda *a,**k:next(outcomes))
        self.assertEqual(seen,[BASE,BASE,BASE]);self.assertEqual(self.report['automatic_strategy']['selected'],'ripup')

    def test_protection_failure_stops_portfolio(self):
        calls=[]
        def gen(job,*a,**k):calls.append(job.label);return self.candidate(job)
        with self.assertRaises(Halt) as caught:
            self.exercise(gen,lambda *a,**k:(1,{'verification_complete':True,'accepted':False,'checks':{'existing_components_preserved':False}}))
        self.assertEqual(caught.exception.status,'protection_violation');self.assertEqual(calls,['add-only'])

    def test_incomplete_validation_is_never_accepted_or_retried(self):
        calls=[]
        def gen(job,*a,**k):calls.append(job.label);return self.candidate(job)
        with self.assertRaises(Halt) as caught:
            self.exercise(gen,lambda *a,**k:(2,{'verification_complete':False,'accepted':True}))
        self.assertEqual(caught.exception.status,'verification_incomplete');self.assertEqual(len(calls),1)

    def test_generation_budget_is_shared_across_strategy_timeout(self):
        now=[100.];budgets=[]
        def gen(job,*a,**k):
            budgets.append(k['budget']);now[0]+=k['budget'];raise Halt('timed_out','bounded fake stage')
        with patch('pcb_local.auto_strategy.time.monotonic',side_effect=lambda:now[0]),self.assertRaises(Halt) as caught:
            self.exercise(gen,lambda *a:None)
        self.assertEqual(caught.exception.status,'timed_out');self.assertEqual(sum(budgets),60)
        self.assertEqual(self.report['automatic_strategy']['generation_seconds'],60)

    def test_initial_strategy_planning_uses_original_generation_budget(self):
        now=[100.];actual=plans
        def slow_plans(*args,**kwargs):
            now[0]+=60.01
            return actual(*args,**kwargs)
        with patch('pcb_local.auto_strategy.time.monotonic',side_effect=lambda:now[0]),patch('pcb_local.auto_strategy.plans',side_effect=slow_plans),patch('pcb_local.auto_strategy.generate') as gen,self.assertRaises(Halt) as caught:
            repair(self.job,{},self.board,self.task,{},self.report,'quick')
        self.assertEqual(caught.exception.status,'timed_out');gen.assert_not_called()
        self.assertAlmostEqual(self.report['automatic_strategy']['generation_seconds'],60.01)

    def test_feedback_work_uses_original_generation_budget(self):
        now=[100.]
        def slow_feedback(*args):
            now[0]+=31
            return {'objects':[]}
        with patch('pcb_local.auto_strategy.time.monotonic',side_effect=lambda:now[0]),patch('pcb_local.conflict_feedback.collect',side_effect=slow_feedback),patch('pcb_local.auto_strategy.generate',side_effect=Halt('no_progress','no proposal')) as gen,self.assertRaises(Halt) as caught:
            repair(self.job,{},self.board,self.task,{},self.report,'quick')
        self.assertEqual(caught.exception.status,'timed_out');self.assertEqual(gen.call_count,1)
        self.assertGreaterEqual(self.report['automatic_strategy']['generation_seconds'],62)

    def test_validation_budget_is_shared_and_not_reset(self):
        now=[100.];budgets=[]
        def val(*a,**kw):
            budgets.append(kw['budget']);now[0]+=60
            return 1,{'verification_complete':True,'accepted':False,'checks':{'full_drc_no_new_or_changed':False}}
        with patch('pcb_local.auto_strategy.time.monotonic',side_effect=lambda:now[0]),self.assertRaises(Halt):
            self.exercise(lambda job,*a,**kw:self.candidate(job),val)
        self.assertEqual(budgets,[120,60]);self.assertEqual(self.report['automatic_strategy']['verification_seconds'],120)

    def test_late_success_cannot_exceed_shared_verification_budget(self):
        now=[100.]
        def val(*a,**kw):
            now[0]+=120.001
            return 0,{'verification_complete':True,'accepted':True}
        with patch('pcb_local.auto_strategy.time.monotonic',side_effect=lambda:now[0]),self.assertRaises(Halt) as caught:
            self.exercise(lambda job,*a,**kw:self.candidate(job),val)
        self.assertEqual(caught.exception.status,'timed_out')
        self.assertEqual(self.report['automatic_strategy']['status'],'timed_out')

    def test_identical_rejected_board_and_permissions_are_not_revalidated(self):
        calls=[]
        def val(*args,**kw):
            calls.append(1)
            return 1,{'verification_complete':True,'accepted':False,'checks':{'full_drc_no_new_or_changed':False}}
        with patch('pcb_local.auto_strategy.witness'),patch('pcb_local.auto_strategy.generate',side_effect=lambda job,*a,**kw:self.candidate(job)),patch('pcb_local.auto_strategy.validate',side_effect=val),self.assertRaises(Halt):
            repair(self.job,{'pns':{'configured':True}},self.board,TASK,{},self.report,'quick')
        self.assertEqual(len(calls),1)
        self.assertEqual(self.report['automatic_strategy']['attempts'][1]['skip_reason'],'duplicate_verified_candidate')

    def test_native_unsupported_stack_skips_only_pns(self):
        calls=[]
        def gen(job,*a,**kw):calls.append(job.label);raise Halt('no_progress','not found')
        with patch('pcb_local.auto_strategy.witness'),patch('pcb_local.auto_strategy.generate',side_effect=gen),self.assertRaises(Halt):
            repair(self.job,{'pns':{'configured':True}},self.board,self.task,{'copper_layers':['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','In5.Cu','In6.Cu','In7.Cu','In8.Cu','B.Cu']},self.report,'quick')
        self.assertEqual(calls,['add-only','geometry','ripup'])
        self.assertEqual(self.report['automatic_strategy']['attempts'][2]['skip_reason'],'native_stack')

    def test_timeout_residual_is_not_geometry_input(self):
        calls=[]
        def gen(job,config,board,task,native,detail,strategy,profile,budget):
            calls.append((job.label,strategy,budget,board.read_text()))
            if job.label=='add-only':
                self.candidate(job).write_text('untrusted timed-out output')
                raise Halt('timed_out','injected KRT timeout')
            return self.candidate(job)
        self.exercise(gen,lambda *a,**kw:(0,{'verification_complete':True,'accepted':True}))
        self.assertEqual([x[:2] for x in calls],[('add-only','krt'),('geometry','geometry')])
        self.assertEqual(calls[0][2],12)
        self.assertEqual(calls[1][3],BASE)
        self.assertEqual(self.report['automatic_strategy']['selected'],'geometry')

    def test_line_and_via_endpoints_have_one_geometry_stage(self):
        task=copy.deepcopy(self.task);task['target_endpoints']=[{'kind':'pad'},{'kind':'via'}]
        self.assertEqual([x[:2] for x in plans(task,True)],[('add-only','krt'),('geometry','geometry'),('push','pns'),('ripup','krt')])
        self.assertEqual([x[:2] for x in plans(task,False)],[('add-only','krt'),('geometry','geometry'),('ripup','krt')])
        self.assertNotIn('modification',plans(task,True)[0][2])
        self.assertEqual(plans(task,True)[2][2]['modification'],dict(task['modification'],mode='shove'))

    def test_cancel_or_mutated_baseline_never_falls_back(self):
        def gen(job,*a,**k):
            self.board.write_text(BASE+'\n');raise Halt('unsupported','generation refused')
        with self.assertRaises(Halt) as caught:self.exercise(gen,lambda *a:None)
        self.assertEqual(caught.exception.status,'protection_violation')
        self.board.write_text(BASE)
        self.job.folder=self.root/'cancel-job';self.job.folder.mkdir();(self.job.folder/'netclasses.native.json').write_text('{}')
        def cancel(*a,**k):raise Halt('cancelled','requested')
        with self.assertRaises(Halt) as caught:self.exercise(cancel,lambda *a:None)
        self.assertEqual(caught.exception.status,'cancelled')

if __name__=='__main__':unittest.main()
