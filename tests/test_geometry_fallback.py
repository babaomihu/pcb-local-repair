import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from pcb_local.geometry_fallback import supplement
from pcb_local.jobs import Halt
from pcb_local.workspace import source_files


class FallbackChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.board=self.folder/'board.kicad_pcb';self.board.write_text('(kicad_pcb)')
        self.board.with_suffix('.kicad_pro').write_text('{}')
        self.candidate=self.board;self.report={};self.labels=[];self.budgets=[];self.model_status='ready';self.cancel=False
        self.task=dict(target_net='signal',target_pad_uuids=['a','b'])
        self.native={'items':{k:dict(kind='pad',uuid=k,net='signal') for k in ['a','b']},'components':[dict(net='signal',items=[k]) for k in ['a','b']],'component_of':{'a':0,'b':1}}
        self.after=copy.deepcopy(self.native);self.found=False;self.search_status="no_path";self.mutate=False;self.outline_raw=None;self.diagnostic_timeout=False
        outer=self
        class Job:
            folder=outer.folder
            def check(self):
                if outer.cancel:raise Halt('cancelled','test')
            def run(self,label,argv,timeout):
                outer.labels.append(label);outer.budgets.append(timeout)
                if label=='geometry-model':(self.folder/'geometry-model.json').write_text(json.dumps(dict(status=outer.model_status,reason='unsupported geometry',outline_diagnostics_raw=outer.outline_raw)))
                elif label=='krt-connectivity':(self.folder/'krt-connectivity.json').write_text(json.dumps(outer.after))
                elif label=='geometry-outline-diagnosis':
                    if outer.diagnostic_timeout:raise Halt('timed_out','diagnostic budget exhausted')
                    (self.folder/'geometry-outline-diagnosis.json').write_text(json.dumps({'status':'analyzed','acceptance_authorized':False}))
                elif label=='geometry-search':
                    dest=self.folder/'geometry-search';dest.mkdir()
                    result=dict(status=outer.search_status,input_unchanged=True)
                    if outer.found:result.update(status='found',candidate_board=str(outer.board),candidate_hashes=source_files(outer.board))
                    (dest/'search.json').write_text(json.dumps(result))
                if outer.mutate:outer.board.write_text("modified")
                return 0
        self.job=Job();self.config={'kicad_python':'native','router_python':'router'}
    def run_case(self,remaining=5):
        return supplement(self.job,self.config,self.board,self.candidate,self.task,self.native,self.report,time.monotonic()+remaining)
    def test_fallback_only_generates_and_preserves_acceptance_boundary(self):
        self.found=True;self.run_case()
        self.assertEqual(self.report['candidate_generator'],'geometry-visibility')
        self.assertNotIn('accepted',self.report)
        self.assertTrue(all(0<x<=5 for x in self.budgets))
    def test_already_connected_skips_search(self):
        self.after['component_of']['b']=0;self.run_case()
        self.assertNotIn('geometry-search',self.labels)
        self.assertEqual(self.report['geometry_fallback']['status'],'not_needed')
    def test_preserved_connections_must_not_split(self):
        self.native['components']=[dict(net='signal',items=['a','b'])];self.native['component_of']['b']=0
        with self.assertRaises(Halt) as cm:self.run_case()
        self.assertEqual(cm.exception.status,'protection_violation');self.assertNotIn('geometry-search',self.labels)
    def test_unsupported_model_returns_original_proposal(self):
        self.model_status='unsupported';self.assertEqual(self.run_case(),self.candidate)
        self.assertEqual(self.labels,['geometry-model'])
    def test_exhausted_budget_starts_nothing(self):
        self.run_case(0);self.assertEqual(self.labels,[])
        self.assertEqual(self.report['geometry_fallback']['status'],'budget_exhausted')
    def independent(self,remaining=5):
        return supplement(self.job,self.config,self.board,self.board,self.task,self.native,self.report,time.monotonic()+remaining,independent=True)
    def test_independent_geometry_never_probes_failed_krt_output(self):
        self.found=True;self.independent()
        self.assertEqual(self.labels,['geometry-model','geometry-search'])
        self.assertEqual(self.report['geometry_fallback']['mode'],'independent_frozen_input')
    def test_independent_distinguishes_unsupported_no_path_and_budget(self):
        self.model_status='unsupported'
        with self.assertRaises(Halt) as caught:self.independent()
        self.assertEqual(caught.exception.status,'unsupported')
        self.model_status='ready'
        with self.assertRaises(Halt) as caught:self.independent()
        self.assertEqual(caught.exception.status,'no_progress')
        (self.folder/'geometry-search/search.json').unlink();(self.folder/'geometry-search').rmdir()
        self.search_status='bounded_out'
        with self.assertRaises(Halt) as caught:self.independent()
        self.assertEqual(caught.exception.status,'no_progress')
        with self.assertRaises(Halt) as caught:self.independent(0)
        self.assertEqual(caught.exception.status,'timed_out')
    def test_worker_deadline_keeps_timeout_status(self):
        self.search_status='budget_exhausted'
        with self.assertRaises(Halt) as caught:self.independent()
        self.assertEqual(caught.exception.status,'timed_out')
        self.assertNotIn('accepted',self.report)

    def test_independent_model_cannot_change_frozen_board(self):
        self.mutate=True;self.model_status='unsupported'
        with self.assertRaises(Halt) as caught:self.independent()
        self.assertEqual(caught.exception.status,'protection_violation')
    def test_outline_diagnosis_does_not_change_unsupported_verdict(self):
        self.model_status='unsupported';self.outline_raw={'status':'raw'}
        with self.assertRaises(Halt) as caught:self.independent()
        self.assertEqual(caught.exception.status,'unsupported')
        self.assertEqual(self.report['geometry_fallback']['outline_diagnostics']['status'],'analyzed')
        self.assertTrue(all(0<x<=5 for x in self.budgets))
    def test_outline_diagnostic_timeout_does_not_invent_search_failure(self):
        self.model_status='unsupported';self.outline_raw={'status':'raw'};self.diagnostic_timeout=True
        with self.assertRaises(Halt) as caught:self.independent()
        self.assertEqual(caught.exception.status,'unsupported')
        self.assertEqual(self.report['geometry_fallback']['outline_diagnostics']['status'],'budget_exhausted')
    def test_cancel_is_not_swallowed(self):
        self.cancel=True
        with self.assertRaises(Halt) as cm:self.run_case()
        self.assertEqual(cm.exception.status,'cancelled');self.assertEqual(self.labels,[])
    def test_optional_outline_diagnostic_oserror_preserves_model_failure(self):
        self.model_status='unsupported';self.outline_raw={'status':'raw'}
        frozen=source_files(self.board);original_run=self.job.run
        def run(label,argv,timeout):
            if label=='geometry-outline-diagnosis':
                self.labels.append(label)
                raise OSError('Optional diagnostic runtime unavailable')
            return original_run(label,argv,timeout)
        self.job.run=run
        with self.assertRaises(Halt) as caught:self.independent()
        self.assertEqual(caught.exception.status,'unsupported')
        self.assertIn('unsupported geometry',str(caught.exception))
        evidence=self.report['geometry_fallback']
        self.assertEqual(evidence['reason'],'unsupported geometry')
        self.assertEqual(evidence['outline_diagnostics']['status'],'unavailable')
        self.assertFalse(evidence['outline_diagnostics']['acceptance_authorized'])
        self.assertEqual(self.labels,['geometry-model','geometry-outline-diagnosis'])
        self.assertEqual(source_files(self.board),frozen)
    def test_optional_outline_diagnostic_cancel_remains_decisive(self):
        self.model_status='unsupported';self.outline_raw={'status':'raw'}
        frozen=source_files(self.board);original_run=self.job.run
        def run(label,argv,timeout):
            if label=='geometry-outline-diagnosis':
                self.labels.append(label)
                raise Halt('cancelled','Cancelled during optional diagnosis')
            return original_run(label,argv,timeout)
        self.job.run=run
        with self.assertRaises(Halt) as caught:self.independent()
        self.assertEqual(caught.exception.status,'cancelled')
        self.assertIn('Cancelled during optional diagnosis',str(caught.exception))
        self.assertEqual(self.labels,['geometry-model','geometry-outline-diagnosis'])
        self.assertNotIn('accepted',self.report)
        self.assertEqual(source_files(self.board),frozen)

if __name__=='__main__':unittest.main()
