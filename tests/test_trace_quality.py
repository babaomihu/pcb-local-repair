"""Quality proposals must preserve original copper and incidental connections."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from pcb_local.trace_quality import propose, shorten, improve
from pcb_local.geometry_search import candidate
from pcb_local.validation import parse_sexpr, name, field
from pcb_local.scope_candidate import _serialize
from pcb_local.workspace import source_files
from pcb_local.jobs import Halt, write_json
from geometry_fixture import create
try:
    import shapely
    GEOMETRY=True
except ImportError:
    GEOMETRY=False


def model(task):
    return dict(task=task, minimum_track_width_mm=.25, minimum_clearance_mm=.2,
        edge_clearance_mm=.2, primitives=[], outline_edges=[[[2,2],[28,2]],[[28,2],[28,28]],[[28,28],[2,28]],[[2,28],[2,2]]],
        endpoints=[dict(uuid=uid,center=p,track_width_mm=.25,clearance_mm=.2) for uid,p in zip(task['target_pad_uuids'],[[6,15],[24,15]])])


@unittest.skipUnless(GEOMETRY,'Pinned geometry runtime required')
class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.before,self.task=create(self.root/'input',gates=())
        self.model=model(self.task)
        self.path=[[6,15],[8,15],[10,17],[20,17],[22,15],[24,15]]
        self.after=candidate(self.before,self.task,dict(path_mm=self.path,width_mm=.25),self.root/'raw')

    def run_proposal(self):return propose(self.before,self.after,self.task,self.model,self.root/'out',timeout=3)
    def mutate(self,fn):
        tree=parse_sexpr(self.after.read_text())[0];fn(tree);self.after.write_text(_serialize(tree)+'\n')

    def test_visible_shortcut_reduces_length_and_segments_without_touching_inputs(self):
        hashes=source_files(self.before),source_files(self.after)
        r=self.run_proposal()
        self.assertEqual(r['status'],'proposed');self.assertEqual(r['after_segments'],1)
        self.assertEqual(r['after_length_mm'],18);self.assertLess(r['after_length_mm'],r['before_length_mm'])
        self.assertEqual(hashes,(source_files(self.before),source_files(self.after)))

    def test_original_copper_edit_refused(self):
        # An original segment is added to both boards, then its width changed only in candidate.
        seg='(segment (start 5 10) (end 7 10) (width .25) (layer "F.Cu") (net 1) (uuid "76f06ad2-9089-43f3-9a2b-aed50dbfb052"))'
        for board in [self.before,self.after]:
            tree=parse_sexpr(board.read_text())[0];tree+=parse_sexpr(seg);board.write_text(_serialize(tree)+'\n')
        self.mutate(lambda tree:field(tree[-1],'width').__setitem__(1,'.3'))
        with self.assertRaisesRegex(ValueError,'Original routing changed'):self.run_proposal()

    def test_cross_net_added_track_refused(self):
        self.mutate(lambda tree:field(next(n for n in tree if name(n)=='segment'),'net').__setitem__(1,'0'))
        with self.assertRaisesRegex(ValueError,'same-net'):self.run_proposal()

    def test_wrong_layer_refused(self):
        self.mutate(lambda tree:field(next(n for n in tree if name(n)=='segment'),'layer').__setitem__(1,'B.Cu'))
        with self.assertRaisesRegex(ValueError,'same-layer'):self.run_proposal()

    def test_variable_width_refused(self):
        self.mutate(lambda tree:field(next(n for n in tree if name(n)=='segment'),'width').__setitem__(1,'.3'))
        with self.assertRaisesRegex(ValueError,'Variable widths'):self.run_proposal()

    def test_new_branch_refused(self):
        self.mutate(lambda tree:tree.extend(parse_sexpr('(segment (start 10 17) (end 10 16) (width .25) (layer "F.Cu") (net 1) (uuid "76f06ad2-9089-43f3-9a2b-aed50dbfb052"))')))
        with self.assertRaisesRegex(ValueError,'branched'):self.run_proposal()

    def test_incidental_same_net_original_contact_refused(self):
        self.model['primitives']=[dict(uuid='side-pad',kind='circle',center=[10,17],size=[1,1],net=self.task['target_net'],clearance_mm=.2)]
        with self.assertRaisesRegex(ValueError,'additional same-net contact'):self.run_proposal()

    def test_same_net_far_objects_do_not_block_optimization(self):
        self.model['primitives']=[dict(uuid='far-pad',kind='circle',center=[10,10],size=[1,1],net=self.task['target_net'],clearance_mm=.2)]
        self.assertEqual(self.run_proposal()['status'],'proposed')

    def test_other_net_obstacle_prevents_direct_shortcut(self):
        self.model['primitives']=[dict(uuid='obstacle',kind='circle',center=[15,15],size=[1,1],net='other',clearance_mm=.2)]
        r=self.run_proposal();self.assertEqual(r['status'],'proposed');self.assertGreater(r['after_segments'],1)
        from shapely.geometry import LineString,Point
        tree=parse_sexpr(Path(r['candidate_board']).read_text())[0]
        for row in tree:
            if name(row)=='segment':
                self.assertGreater(LineString([list(map(float,field(row,k)[1:3])) for k in ['start','end']]).distance(Point(15,15)),.825)

    def test_region_is_not_expanded(self):
        self.task['region_mm']=[4,14,26,16]
        with self.assertRaisesRegex(ValueError,'outside the conservative'):self.run_proposal()

    def test_existing_via_or_new_via_not_rewritten(self):
        self.mutate(lambda tree:tree.extend(parse_sexpr('(via (at 10 17) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (net 1) (uuid "76f06ad2-9089-43f3-9a2b-aed50dbfb052"))')))
        with self.assertRaisesRegex(ValueError,'same-net'):self.run_proposal()

    def test_modification_task_refused(self):
        self.task['modification']={'mode':'shove'}
        with self.assertRaisesRegex(ValueError,'additive'):self.run_proposal()

    def test_budget_has_no_partial_candidate(self):
        with self.assertRaises(TimeoutError):propose(self.before,self.after,self.task,self.model,self.root/'out',timeout=0)
        self.assertFalse((self.root/'out').exists())

    def test_unaccepted_result_is_not_optimized(self):
        r={};v={'verification_complete':True,'accepted':False,'checks':{'target_connected':False}}
        returned=improve(None,None,self.before,self.after,self.task,r,1,v,60,120)
        self.assertEqual(returned,(self.after,1,v));self.assertEqual(r['trace_quality']['status'],'not_attempted')

    def test_insufficient_budget_keeps_original(self):
        r={};v={'verification_complete':True,'accepted':True,'checks':{'target_connected':True}}
        returned=improve(None,None,self.before,self.after,self.task,r,0,v,.5,120)
        self.assertEqual(returned,(self.after,0,v))

    def assert_no_native_optimization(self):
        report={};v={'verification_complete':True,'accepted':True,'checks':{'native':True}}
        result=improve(None,None,self.before,self.after,self.task,report,0,v,60,120)
        self.assertEqual(result,(self.after,0,v));self.assertEqual(report['trace_quality']['status'],'not_attempted')
        self.assertGreater(report['trace_quality']['generation_seconds'],0)

    def test_fast_via_gate_starts_no_native_worker(self):
        self.mutate(lambda tree:tree.extend(parse_sexpr('(via (at 10 17) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (net 1) (uuid "76f06ad2-9089-43f3-9a2b-aed50dbfb052"))')))
        self.assert_no_native_optimization()

    def test_fast_layer_gate_starts_no_native_worker(self):
        self.mutate(lambda tree:field(next(n for n in tree if name(n)=='segment'),'layer').__setitem__(1,'B.Cu'))
        self.assert_no_native_optimization()

    def test_fast_net_gate_starts_no_native_worker(self):
        self.mutate(lambda tree:field(next(n for n in tree if name(n)=='segment'),'net').__setitem__(1,'0'))
        self.assert_no_native_optimization()

    def test_fast_width_gate_starts_no_native_worker(self):
        self.mutate(lambda tree:field(next(n for n in tree if name(n)=='segment'),'width').__setitem__(1,'.3'))
        self.assert_no_native_optimization()

    def wrapper(self, validator):
        outer=self
        class FakeJob:
            folder=outer.root/'job'
            def check(self):pass
            def run(self,label,argv,**kwargs):
                if label=='quality-model':
                    write_json(Path(argv[argv.index('--output')+1]),{'status':'ready','model':outer.model})
                elif label=='quality-proposal':
                    result=propose(outer.before,outer.after,outer.task,outer.model,Path(argv[argv.index('--out-dir')+1]))
                    write_json(Path(argv[argv.index('--output')+1]),result)
                else:raise AssertionError(label)
                return 0
        job=FakeJob();job.folder.mkdir()
        report={};v={'verification_complete':True,'accepted':True,'checks':{'target_connected':True}}
        config={'kicad_python':sys.executable,'router_python':sys.executable}
        return improve(job,config,self.before,self.after,self.task,report,0,v,60,120,validator),report

    def test_rejected_optimization_retains_original_accepted_result(self):
        returned,report=self.wrapper(lambda *a,**kw:(1,dict(verification_complete=True,accepted=False,checks={'drc_no_new_violations':False})))
        self.assertEqual(returned[0],self.after);self.assertTrue(returned[2]['accepted'])
        self.assertEqual(report['trace_quality']['status'],'kept_original')

    def test_protection_failure_is_never_hidden_by_original_candidate(self):
        with self.assertRaises(Halt) as cm:
            self.wrapper(lambda *a,**kw:(1,dict(verification_complete=True,accepted=False,checks={'existing_components_preserved':False})))
        self.assertEqual(cm.exception.status,'protection_violation')

    def test_incomplete_validation_stops(self):
        with self.assertRaises(Halt) as cm:
            self.wrapper(lambda *a,**kw:(2,dict(verification_complete=False,accepted=False,checks={})))
        self.assertEqual(cm.exception.status,'verification_incomplete')

    def test_cancelled_validation_stops(self):
        def cancelled(*a,**kw):raise Halt('cancelled','injected cancellation')
        with self.assertRaises(Halt) as cm:self.wrapper(cancelled)
        self.assertEqual(cm.exception.status,'cancelled')

    def test_validator_cannot_mutate_original_candidate(self):
        def corrupt(*a,**kw):
            self.after.write_text(self.after.read_text()+'\n')
            return 0,dict(verification_complete=True,accepted=True,checks={'target_connected':True})
        with self.assertRaises(Halt) as cm:self.wrapper(corrupt)
        self.assertEqual(cm.exception.status,'protection_violation')

if __name__=='__main__':unittest.main()
