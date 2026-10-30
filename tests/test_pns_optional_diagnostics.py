import json
from pathlib import Path
import tempfile
import unittest
from pcb_local.pns_backend import generate
from pcb_local.jobs import Halt

class OptionalDiagnostic(unittest.TestCase):
    def test_diagnostic_failure_retains_known_geometry_reason(self):
        for fault in (Halt('timed_out','diagnostic budget'),OSError('worker unavailable')):
            with self.subTest(fault=str(fault)),tempfile.TemporaryDirectory() as folder:
                job=self.job(folder,fault);report={'preflight':{'measurement':{}}}
                with self.assertRaises(Halt) as caught:self.run_pns(job,report)
                self.assertEqual(caught.exception.status,'unsupported')
                self.assertIn('straight board-edge',str(caught.exception))
                self.assertIn('straight board-edge',report['pns']['unsupported_reason'])
                self.assertIn(report['pns']['outline_diagnostics']['status'],('unavailable','budget_exhausted'))
    def test_cancel_or_protection_is_never_swallowed(self):
        for status in ('cancelled','protection_violation','verification_incomplete'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(Halt) as caught:self.run_pns(self.job(folder,Halt(status,'stop')),{'preflight':{'measurement':{}}})
                self.assertEqual(caught.exception.status,status)
    @staticmethod
    def run_pns(job,report):
        return generate(job,{'pns':{'python':'unused','module_dir':'unused'},'kicad_python':'unused','router_python':'unused'},job.folder/'board.kicad_pcb',{'allowed_layer':'F.Cu'},None,report)
    @staticmethod
    def job(folder,fault):
        class Job:
            def __init__(self):self.folder=Path(folder);self.labels=[]
            def run(self,label,args,timeout):
                self.labels.append(label)
                if len(self.labels)==1:
                    output=Path(args[args.index('--output')+1]);output.write_text(json.dumps(dict(status='unsupported',reason='Geometric search requires straight board-edge segments',outline_diagnostics_raw={'status':'raw'})));return 0
                raise fault
        return Job()
