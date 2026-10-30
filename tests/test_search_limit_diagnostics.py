"""Actual search termination reasons must survive worker and report boundaries."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pcb_local import geometry_search as single, multilayer_geometry as multi
from pcb_local.diagnostics import explain, markdown
from test_direct_visibility import complex_model
from test_multilayer_geometry import layered

class LimitDiagnostics(unittest.TestCase):
    def terminated(self, engine, model, kind, **kwargs):
        frozen=copy.deepcopy(model)
        with self.assertRaises(single.SearchLimit) as caught:engine.search(model,**kwargs)
        result=caught.exception.result()
        self.assertEqual(result['limit_kind'],kind)
        self.assertEqual(result['status'],'budget_exhausted' if kind=='deadline' else 'bounded_out')
        self.assertEqual(frozen,model)
        return result

    def test_deadlines_are_explicit_in_both_searches(self):
        self.terminated(single,complex_model(),'deadline',timeout=0)
        self.terminated(multi,layered(),'deadline',timeout=0)

    def test_primitive_limit_records_actual_count(self):
        m=complex_model()
        with patch.object(single,'MAX_PRIMITIVES',1):r=self.terminated(single,m,'primitive_limit')
        self.assertEqual(r['limit_details'],dict(observed=len(m['primitives']),maximum=1))

    def test_vertex_limits_on_single_and_multiple_layers(self):
        for engine,model,constant in [(single,complex_model(True),'MAX_VERTICES'),(multi,layered(),'MAX_NODES')]:
            with self.subTest(engine=engine.__name__),patch.object(engine,constant,1):
                r=self.terminated(engine,model,'vertex_limit')
                self.assertGreater(r['limit_details']['observed'],1)
                self.assertEqual(r['limit_details']['maximum'],1)

    def test_corridor_failure_is_scope_not_time(self):
        m=complex_model();m['primitives'].append(dict(kind='keepout',net='',blocks_same_net=True,clearance_mm=0,polygons=[dict(shell=[[14,11],[16,11],[16,16.5],[14,16.5]],holes=[])]))
        # This former failure now routes with the exterior-boundary strategy.
        # Force a real bounded failure to retain the scope-vs-time contract.
        with patch.object(single,'MAX_VERTICES',10):
            r=self.terminated(single,m,'local_corridor_disconnected')
        self.assertEqual(r['limit_details']['maximum'],10)
        self.assertEqual(len(r['limit_details']['boundary_attempts']),2)

    def test_state_limit_is_not_timeout(self):
        with patch.object(multi,'MAX_STATES',0):self.terminated(multi,layered(),'state_limit')

    def test_worker_writes_structured_limit_and_keeps_input(self):
        for engine,model,constant in [(single,complex_model(True),'MAX_VERTICES'),(multi,layered(),'MAX_NODES')]:
            with self.subTest(engine=engine.__name__), tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp);board=p/'board.kicad_pcb';board.write_text('(kicad_pcb)')
                model_path=p/'model.json';model_path.write_text(json.dumps(dict(status='ready',model=model)))
                argv=['worker','--board',str(board),'--model',str(model_path),'--out-dir',str(p/'out'),'--timeout','5']
                with patch('sys.argv',argv),patch.object(engine,constant,1),contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(engine.main(),0)
                r=json.loads((p/'out/search.json').read_text())
                self.assertEqual(r['status'],'bounded_out');self.assertEqual(r['limit_kind'],'vertex_limit')
                self.assertTrue(r['input_unchanged']);self.assertNotIn('candidate_board',r)
                self.assertEqual(board.read_text(),'(kicad_pcb)')

    def test_report_distinguishes_scope_time_unknown_and_no_path(self):
        cases=[('no_progress','bounded_out','search_scope_limit'),('timed_out','budget_exhausted','budget_exhausted'),('no_progress','no_path','search_not_found')]
        for status,search_status,category in cases:
            with self.subTest(status=status,worker=search_status):
                d=explain(dict(status=status,geometry_fallback=dict(status=search_status)))
                self.assertEqual(d['category'],category);self.assertFalse(d['physical_impossibility_proven'])
                if search_status=='bounded_out':
                    self.assertEqual(d['search_limits'][0]['kind'],'unspecified')
                    self.assertIn('搜索限制',markdown(d))

    def test_auto_attempts_keep_distinct_limits_without_overriding_accepted(self):
        d=explain(dict(status='accepted',automatic_strategy=dict(attempts=[dict(name='geometry',status='no_progress',details=dict(geometry_fallback=dict(status='bounded_out',limit_kind='vertex_limit',limit_details=dict(observed=1300,maximum=1200)))),dict(name='later',status='accepted',details={})])))
        self.assertEqual(d['category'],'accepted');self.assertEqual(d['attempts'][0]['category'],'search_scope_limit')
        self.assertEqual(d['search_limits'][0]['details']['maximum'],1200)

    def test_protection_cancel_and_incomplete_still_take_precedence(self):
        for status,expected in [('protection_violation','protected_state_violation'),('cancelled','cancelled'),('verification_incomplete','verification_incomplete')]:
            d=explain(dict(status=status,geometry_fallback=dict(status='bounded_out')))
            self.assertEqual(d['category'],expected)
