import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pcb_local.candidate_pipeline import generate
from pcb_local.jobs import Halt


class EmptyKRTFallback(unittest.TestCase):
 def exercise(self,strategy='krt+geometry',code=0,mutate=False,modified=False):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);board=root/'board.kicad_pcb';board.write_text('(kicad_pcb)');board.with_suffix('.kicad_pro').write_text('{}')
   task=dict(target_net='SIGNAL',allowed_layer='F.Cu',target_endpoints=[{},{}]);report={};budgets=[]
   if modified:task['permissions']=dict(schema_version=1,route_layers=['F.Cu','B.Cu'],max_new_vias=1,via_type='through')
   def run(label,args,**kw):
    budgets.append(kw['timeout'])
    if mutate:board.write_text('changed')
    if code==2:
     (root/'engine-output/local-target-request.json').write_text('{}')
     (root/'engine-output/route-summary.json').write_text(json.dumps(dict(status='unsupported',reason='unmodeled copper')))
    return code
   job=SimpleNamespace(folder=root,run=run);cfg={'krt_root':'unused'}
   with patch('pcb_local.candidate_pipeline.engine_command',return_value=(['router'],{})),patch('pcb_local.geometry_fallback.supplement',return_value=board) as fallback:
    result=generate(job,cfg,board,task,{},report,strategy,'baseline',budget=9)
    self.assertEqual(result,board);self.assertEqual(fallback.call_count,1)
    self.assertEqual(fallback.call_args.args[2:4],(board,board));self.assertTrue(fallback.call_args.kwargs['independent'])
    self.assertLessEqual(budgets[0],9);self.assertIn('krt_fallback_reason',report)
 def test_empty_local_candidate_preserves_default_geometry_fallback(self):self.exercise()
 def test_unsupported_local_model_can_use_independent_native_geometry(self):self.exercise(code=2)
 def test_explicit_krt_is_never_mislabeled_geometry(self):
  with self.assertRaises(Halt) as caught:self.exercise(strategy='krt')
  self.assertEqual(caught.exception.status,'no_progress')
 def test_source_mutation_stops_fallback(self):
  with self.assertRaises(Halt) as caught:self.exercise(mutate=True)
  self.assertEqual(caught.exception.status,'protection_violation')
 def test_multilayer_permissions_cannot_enter_single_layer_fallback(self):
  with self.assertRaises(Halt) as caught:self.exercise(modified=True)
  self.assertEqual(caught.exception.status,'no_progress')

if __name__=='__main__':unittest.main()
