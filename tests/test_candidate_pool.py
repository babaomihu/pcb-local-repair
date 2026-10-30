import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pcb_local.candidate_pool import fingerprint, score, validate_candidates
from pcb_local.jobs import Halt
from pcb_local.workspace import copy_project, source_files
from test_scope_candidate import BASE, RULES, TASK, segment


class CandidatePoolTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root=Path(tmp.name)
        self.board=self.root/'board.kicad_pcb'; self.board.write_text(BASE)
        self.board.with_suffix('.kicad_pro').write_text(json.dumps(RULES))
        folder=self.root/'job'; folder.mkdir()
        self.task=dict(TASK,id='test')
        for name,value in [('task.json',self.task),('task-manifest.json',{'cases':[self.task]})]:
            (folder/name).write_text(json.dumps(value))
        self.job=SimpleNamespace(folder=folder,check=lambda:None)
        self.paths=[self.candidate('a',segment('new-a')),self.candidate('b',segment('new-b',end=(6,1)))]
        self.report={'candidate_pool':{'candidates':[dict(attempt=i,board=str(p),hashes=source_files(p),
            fingerprint=fingerprint(self.board,p,self.task)) for i,p in enumerate(self.paths)]}}

    def candidate(self,label,addition):
        p,_=copy_project(self.board,self.root/label)
        p.write_text(BASE[:-1]+addition+')'); return p

    def run_pool(self,validator,budget=120):
        return validate_candidates(self.job,{},self.board,self.paths[0],self.task,self.report,budget,validator)

    def test_new_uuid_direction_and_serialization_order_deduplicate(self):
        reverse=self.candidate('reverse',segment('another-id',start=(5,1),end=(2,1)))
        self.assertEqual(fingerprint(self.board,self.paths[0],self.task),fingerprint(self.board,reverse,self.task))
        copied,_=copy_project(self.board,self.root/'baseline-copy')
        self.assertEqual(fingerprint(copied,reverse,self.task),fingerprint(self.board,reverse,self.task))

    def test_original_identity_rules_width_permission_are_never_erased(self):
        first=fingerprint(self.board,self.paths[0],self.task)
        for label,text in [('identity',self.paths[0].read_text().replace('"old"','"replacement"')),
                           ('width',self.paths[0].read_text().replace('(width 0.2)','(width 0.21)'))]:
            p,_=copy_project(self.paths[0],self.root/label);p.write_text(text)
            self.assertNotEqual(first,fingerprint(self.board,p,self.task))
        task=dict(self.task,region_mm=[0,0,11,10]);self.assertNotEqual(first,fingerprint(self.board,self.paths[0],task))
        self.paths[0].with_suffix('.kicad_dru').write_text('(version 1)')
        self.assertNotEqual(first,fingerprint(self.board,self.paths[0],self.task))

    def test_drc_rejection_tries_next_with_remaining_budget_and_exact_path(self):
        now=[0.]; allocations=[]
        def validate(job,config,board,path,task,budget):
            allocations.append(budget);now[0]+=10
            return (1,{'verification_complete':True,'checks':{'full_drc_no_new_or_changed':False}}) if path==self.paths[0] else (0,{'verification_complete':True,'accepted':True})
        with patch('pcb_local.candidate_pool.time.monotonic',side_effect=lambda:now[0]):
            candidate,code,_=self.run_pool(validate)
        self.assertEqual(candidate,self.paths[1]);self.assertEqual(code,0)
        self.assertEqual(allocations,[120,110]);self.assertEqual(self.report['candidate_pool']['selected_attempt'],1)
        self.assertTrue(self.report['verification_directory'].endswith('candidate-validations/1/verification'))

    def test_protection_incomplete_and_cancel_stop_without_next_attempt(self):
        for status,result in [('protection_violation',{'verification_complete':True,'checks':{'existing_components_preserved':False}}),
                              ('verification_incomplete',{'verification_complete':False})]:
            calls=[]
            def validate(*a,**kw):calls.append(1);return 1,result
            with self.assertRaises(Halt) as caught:self.run_pool(validate)
            self.assertEqual(caught.exception.status,status);self.assertEqual(len(calls),1)
        def cancel():raise Halt('cancelled','stop')
        self.job.check=cancel
        with self.assertRaises(Halt) as caught:self.run_pool(lambda *a,**k:self.fail('must not validate'))
        self.assertEqual(caught.exception.status,'cancelled')

    def test_mutated_candidate_or_task_fails_binding_before_validation(self):
        self.task['region_mm']=[0,0,20,20]
        with self.assertRaises(Halt) as caught:self.run_pool(lambda *a,**k:self.fail('must not validate'))
        self.assertEqual(caught.exception.status,'protection_violation')

    def test_late_success_does_not_extend_budget(self):
        now=[0.]
        def validate(*args,**kw):now[0]+=121;return 0,{'accepted':True,'verification_complete':True}
        with patch('pcb_local.candidate_pool.time.monotonic',side_effect=lambda:now[0]),self.assertRaises(Halt) as caught:
            self.run_pool(validate)
        self.assertEqual(caught.exception.status,'timed_out')

    def test_rank_does_not_accept_and_prefers_less_added_length(self):
        self.assertLess(score(self.board,self.paths[0]),score(self.board,self.paths[1]))
        self.assertNotIn('accepted',self.report)

    def test_kiid_seed_is_input_bound_and_reproducible_across_directories(self):
        from pcb_local.pns_worker import identity_seed
        copy,_=copy_project(self.board,self.root/'copy')
        self.assertEqual(identity_seed(copy),identity_seed(self.board))
        self.assertNotEqual(identity_seed(self.paths[0]),identity_seed(self.board))
        self.assertGreaterEqual(identity_seed(self.board),0)
        self.assertLess(identity_seed(self.board),2**31)


if __name__=='__main__':unittest.main()
