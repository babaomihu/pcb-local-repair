"""Independent review regressions: proposal hard stops and report binding.

No KiCad process is required: real serialized boards and compose/protection
code are used, while isolated native workers are replaced by bounded witnesses.
"""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pcb_local.candidate_pool import fingerprint, validate_candidates
from pcb_local.jobs import Halt
from pcb_local.pns_backend import generate
from pcb_local.workspace import copy_project, source_files
from test_scope_candidate import BASE, RULES, TASK, segment


class CandidatePoolIndependentReview(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.board=self.root/'board.kicad_pcb';self.board.write_text(BASE)
        self.board.with_suffix('.kicad_pro').write_text(json.dumps(RULES))
        self.task=dict(TASK,id='review')
        self.folder=self.root/'job';self.folder.mkdir()
        for filename,value in [('task.json',self.task),('task-manifest.json',{'cases':[self.task]}),
                               ('netclasses.native.json',{'nets':{'N':{'values':{}}}})]:
            (self.folder/filename).write_text(json.dumps(value))
        self.paths=[]
        for label,end in [('first',(5,1)),('second',(5,2))]:
            path,_=copy_project(self.board,self.root/label)
            path.write_text(BASE[:-1]+segment(label,end=end)+')');self.paths.append(path)
        self.job=SimpleNamespace(folder=self.folder,check=lambda:None)

    def pool(self):
        return {'candidate':str(self.paths[0]),'candidate_pool':{'candidates':[
            dict(attempt=index,board=str(path),hashes=source_files(path),
                 fingerprint=fingerprint(self.board,path,self.task))
            for index,path in enumerate(self.paths)]}}

    def test_composition_protection_failure_stops_before_next_proposal(self):
        # Real compose() must reject this unauthorized movement of old copper.
        bad,_=copy_project(self.board,self.root/'bad')
        bad.write_text(BASE.replace('(end 2 1)','(end 3 1)'))
        proposal=dict(status='proposed',final_restore_verified=True,
            loaded_board=str(self.board),restored_board=str(self.board),
            attempts=[dict(committed=True,candidate_board=str(path)) for path in [bad,self.paths[0]]])
        model=dict(primitives=[{'net':'N'}],endpoints=[{'uuid':'one','center':[2,1]},
                    {'uuid':'two','center':[5,1]}],minimum_clearance_mm=.2,
                    minimum_track_width_mm=.2,edge_clearance_mm=.2)
        def run(label,argv,timeout):
            if label=='pns-worker':
                dest=self.folder/'pns-worker';dest.mkdir()
                (dest/'proposal.json').write_text(json.dumps(proposal))
            elif '--pns-model' in argv:
                Path(argv[argv.index('--output')+1]).write_text(json.dumps(dict(status='ready',model=model)))
            elif '--output' in argv:
                Path(argv[argv.index('--output')+1]).write_text(json.dumps(dict(local_relevance={})))
            else:
                Path(argv[-1]).write_text('{}')
            return 0
        self.job.run=run
        config={'pns':{'python':'unused','module_dir':'unused'},'kicad_python':'unused','router_python':'unused'}
        report={'preflight':{'measurement':{}}}
        with patch('pcb_local.pns_backend.make_problem',return_value=self.board),\
             patch('pcb_local.partial_routing.validated_plans',return_value=[]),\
             patch('pcb_local.pns_backend.connectivity_delta',return_value={
                 'existing_components_preserved':True,'added_copper_connected_to_target':True,'target_connected':True}):
            with self.assertRaises(Halt) as caught:
                generate(self.job,config,self.board,self.task,{},report)
        self.assertEqual(caught.exception.status,'protection_violation')
        self.assertEqual(len(report['pns']['attempts']),1)

    def test_hard_failure_report_points_to_the_actually_checked_candidate(self):
        report=self.pool()
        def validator(job,config,board,path,task,budget):
            failed='full_drc_no_new_or_changed' if path==self.paths[0] else 'existing_components_preserved'
            return 1,dict(verification_complete=True,accepted=False,checks={failed:False},
                          source_hashes_after=source_files(path))
        with self.assertRaises(Halt) as caught:
            validate_candidates(self.job,{},self.board,self.paths[0],self.task,report,validator=validator)
        self.assertEqual(caught.exception.status,'protection_violation')
        self.assertEqual(report['candidate_construction']['candidate_board'],str(self.paths[1]))
        self.assertEqual(report['candidate'],str(self.paths[1]),
                         'Rejected result must identify the board used by its native verifier')

    def test_original_identity_and_project_rules_survive_uuid_normalization(self):
        old=fingerprint(self.board,self.paths[0],self.task)
        replacement,_=copy_project(self.paths[0],self.root/'normalized')
        replacement.write_text(replacement.read_text().replace('(uuid "first")','(uuid "new-random")'))
        self.assertEqual(old,fingerprint(self.board,replacement,self.task))
        replacement.write_text(replacement.read_text().replace('(uuid "old")','(uuid "old-replaced")'))
        self.assertNotEqual(old,fingerprint(self.board,replacement,self.task))
        replacement.write_text(self.paths[0].read_text())
        replacement.with_suffix('.kicad_pro').write_text('{}')
        self.assertNotEqual(old,fingerprint(self.board,replacement,self.task))

    def test_interrupted_second_validation_cannot_publish_first_result_for_second_board(self):
        report=self.pool()
        first_result=dict(verification_complete=True,accepted=False,
                          checks={'full_drc_no_new_or_changed':False},
                          source_hashes_after=source_files(self.paths[0]))
        def validator(job,config,board,path,task,budget):
            if path==self.paths[0]:return 1,first_result
            raise Halt('timed_out','second verifier interrupted before producing evidence')
        with self.assertRaises(Halt) as caught:
            validate_candidates(self.job,{},self.board,self.paths[0],self.task,report,validator=validator)
        self.assertEqual(caught.exception.status,'timed_out')
        self.assertEqual(report['candidate'],str(self.paths[1]))
        self.assertNotEqual(report.get('verification'),first_result,
                            'An interrupted candidate must not inherit another candidate\'s verification')

    def test_regenerated_branch_exclusion_requires_same_frozen_input(self):
        report=self.pool();excluded=report['candidate_pool']['candidates'][0]['fingerprint']
        copied,_=copy_project(self.board,self.root/'new-attempt-baseline')
        regenerated,_=copy_project(self.paths[0],self.root/'regenerated')
        regenerated.write_text(regenerated.read_text().replace('(uuid "first")','(uuid "different-new")'))
        self.assertEqual(excluded,fingerprint(copied,regenerated,self.task))
        copied.write_text(copied.read_text()+'\n')
        self.assertNotEqual(excluded,fingerprint(copied,regenerated,self.task))

    def test_proposal_sidecar_mutation_is_hard_failure_before_verifier(self):
        report=self.pool();self.paths[0].with_suffix('.kicad_dru').write_text('(version 1)')
        with self.assertRaises(Halt) as caught:
            validate_candidates(self.job,{},self.board,self.paths[0],self.task,report,
                validator=lambda *a,**k:self.fail('changed proposal must not enter verifier'))
        self.assertEqual(caught.exception.status,'protection_violation')


if __name__=='__main__':unittest.main()
