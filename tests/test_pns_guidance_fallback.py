"""An optional cursor planner cannot consume the whole native search budget."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from pcb_local.jobs import Halt
from pcb_local.pns_backend import optional_guidance


class FakeJob:
    def __init__(self,run):self.action=run;self.calls=[];self.cancelled=False
    def check(self):
        if self.cancelled:raise Halt('cancelled','cancelled')
    def run(self,label,args,timeout):
        self.calls.append((label,args,timeout));return self.action(self)


class GuidanceFallbackChecks(unittest.TestCase):
    def invoke(self,action,deadline=30,clock=None):
        with tempfile.TemporaryDirectory() as td:
            output=Path(td)/'guidance.json'
            def run(job):return action(job,output)
            job=FakeJob(run)
            with patch('pcb_local.pns_backend.time.monotonic',side_effect=clock) if clock else patch('pcb_local.pns_backend.time.monotonic',return_value=10):
                result=optional_guidance(job,{'router_python':'python'},Path(td)/'source.json',output,deadline)
            return result,job

    def test_nonzero_exit_falls_back_with_hard_five_second_limit(self):
        result,job=self.invoke(lambda *_:2)
        self.assertEqual(result['plans'],[]);self.assertEqual(job.calls[0][2],5)
        self.assertIn('code 2',result['reason'])

    def test_limit_never_extends_remaining_search_budget(self):
        result,job=self.invoke(lambda *_:1,deadline=12)
        self.assertEqual(job.calls[0][2],2)

    def test_missing_and_corrupt_output_fall_back(self):
        for body in [None,'not json','[]','{"status":"ready","plans":null}',
                     '{"status":"unavailable","plans":[{}]}']:
            def action(_job,out):
                if body is not None:out.write_text(body)
                return 0
            with self.subTest(body=body):self.assertEqual(self.invoke(action)[0]['plans'],[])

    def test_local_timeout_falls_back(self):
        def action(*_):raise Halt('timed_out','planner stage expired')
        self.assertEqual(self.invoke(action)[0]['status'],'unavailable')

    def test_whole_search_deadline_propagates(self):
        def action(*_):raise Halt('timed_out','stage expired')
        with self.assertRaises(Halt) as context:self.invoke(action,deadline=12,clock=[10,10,12])
        self.assertEqual(context.exception.status,'timed_out')

    def test_cancel_and_protection_never_fall_back(self):
        for status in ['cancelled','protection_violation','verification_incomplete']:
            def action(*_):raise Halt(status,'decisive')
            with self.subTest(status=status),self.assertRaises(Halt) as context:self.invoke(action)
            self.assertEqual(context.exception.status,status)

    def test_cancellation_after_worker_exit_propagates(self):
        def action(job,_out):job.cancelled=True;return 1
        with self.assertRaises(Halt) as context:self.invoke(action)
        self.assertEqual(context.exception.status,'cancelled')

    def test_nonfinite_guidance_is_ignored(self):
        def action(_job,out):
            out.write_text(json.dumps({'status':'ready','plans':[{'policy':'guide-low','fixed_space_witness':True,'waypoints':[[1,float('nan')],[3,4]]}]}));return 0
        self.assertEqual(self.invoke(action)[0]['plans'],[])

    def test_valid_witnessed_geometry_retained(self):
        value={'status':'ready','plans':[{'policy':'guide-low','fixed_space_witness':True,'waypoints':[[1,2],[3,4]]}]}
        def action(_job,out):out.write_text(json.dumps(value));return 0
        self.assertEqual(self.invoke(action)[0],value)

if __name__=='__main__':unittest.main()
