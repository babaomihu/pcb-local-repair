"""Exact arc scope and additive quality safety; native DRC is tested separately."""
import copy,json,math,tempfile,unittest
from pathlib import Path
from pcb_local.arc_geometry import circle,contained,sample
from pcb_local.arc_quality import propose
from pcb_local.validation import read_board,protection
from pcb_local.cli import describe_changes
from pcb_local.visual_report import _board
from test_scope_candidate import BASE,RULES,TASK
from test_trace_quality import model,GEOMETRY

class ArcScopeTests(unittest.TestCase):
    def test_circle_sense_major_and_translation(self):
        for p,sweep,sense in [(([1,0],[0,1],[-1,0]),math.pi,1),(([1,0],[0,-1],[0,1]),1.5*math.pi,-1)]:
            for offset in [(0,0),(40.137,.221)]:
                pts=[[round(x+offset[0],6),round(y+offset[1],6)] for x,y in p]
                g=circle(*pts);self.assertAlmostEqual(g['radius'],1);self.assertAlmostEqual(g['sweep'],sweep);self.assertEqual(g['sense'],sense)
    def test_exact_scope_includes_copper_width(self):
        p=([1,0],[0,1],[-1,0])
        self.assertTrue(contained(*p,.2,[-1.1,-1.1,1.1,1.1])[0])
        self.assertFalse(contained(*p,.2,[-1.1,-1.1,1.099999,1.1])[0])
    def test_three_inside_points_do_not_prove_swept_arc_inside(self):
        # All stored points have y <= .8; the minor arc sweeps through y=1.
        p=([1,0],[.6,.8],[-1,0]);self.assertFalse(contained(*p,.2,[-2,-2,2,.95])[0])
    def test_degenerate_nonfinite_fractional_rejected(self):
        for p in [([0,0],[1,1],[2,2]),([0,0],[0,0],[1,1]),([float('nan'),0],[0,1],[1,0]),([.0000001,0],[0,1],[1,0])]:
            with self.assertRaises(ValueError):circle(*p)
    def test_sampling_error_and_bound(self):
        pts,g=sample([1,0],[0,1],[-1,0]);self.assertEqual(pts[0],[1,0]);self.assertEqual(pts[-1],[-1,0])
        for a,b in zip(pts,pts[1:]):self.assertLessEqual(1-math.hypot((a[0]+b[0])/2,(a[1]+b[1])/2),.000002+1e-14)
        with self.assertRaises(ValueError):sample([10000,0],[0,10000],[-10000,0])
    def gate(self,arc,task=None,original=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);before=root/'before.kicad_pcb';after=root/'after.kicad_pcb'
            before.write_text(BASE);after.write_text((BASE.replace('(end 2 1)','(end 2.1 1)') if original else BASE)[:-1]+arc+'\n)')
            for path in [before,after]:path.with_suffix('.kicad_pro').write_text(json.dumps(RULES))
            r=protection(read_board(before),read_board(after),before,after,task or TASK)
            if r['checks']['additions_within_permission']:
                details=describe_changes(after,r,before)['added_details'];self.assertIn('mid_mm',details[0]);self.assertIsNotNone(next(x for x in _board(after)['objects'] if x['kind']=='arc')['circle'])
            return r
    @staticmethod
    def arc(**changes):
        values=dict(start='6 5',mid='5 6',end='4 5',width='.2',layer='"F.Cu"',net='1',uuid='"new"');values.update(changes)
        return '(arc '+ ' '.join('('+k+' '+v+')' for k,v in values.items())+')'
    def test_additive_arc_length_and_unchanged_originals(self):
        r=self.gate(self.arc());self.assertTrue(all(r['checks'].values()));self.assertAlmostEqual(r['new_track_length_mm'],math.pi)
    def test_reject_outside_wrong_layer_wrong_net_malformed(self):
        for a in [self.arc(mid='5 11'),self.arc(layer='"B.Cu"'),self.arc(net='2'),self.arc(mid='5 5'),self.arc(width='-.2'),self.arc(start='nan 5'),self.arc(start='6.0000001 5'),self.arc(padstack='x'),self.arc().replace('(width .2)','(width .2) (width .1)')]:
            with self.subTest(arc=a):self.assertFalse(self.gate(a)['checks']['additions_within_permission'])
    def test_original_changes_still_rejected(self):
        self.assertFalse(self.gate(self.arc(),original=True)['checks']['existing_routing_unchanged'])
    def test_motion_scope_cannot_convert_to_arcs(self):
        t=copy.deepcopy(TASK);t['modification']=dict(schema_version=1,mode='shove',routing_uuids=['old'],nets=['N'],max_modified_tracks=1)
        self.assertFalse(self.gate(self.arc(),t)['checks']['additions_within_permission'])

@unittest.skipUnless(GEOMETRY,'Pinned geometry runtime required')
class ArcSearchTests(unittest.TestCase):
    def problem(self):
        t=dict(target_net='signal',target_pad_uuids=['a','b'],region_mm=[4,4,26,26]);m=model(t)
        m['primitives']=[dict(kind='circle',net='other',uuid='barrier',center=[15,15],size=[2,2],clearance_mm=.2)]
        p=[[10,15],[12,18],[15,18],[18,18],[20,15]]
        return p,m
    def test_tangent_round_obstacle_shrinks_general_path(self):
        p,m=self.problem();frozen=copy.deepcopy(m);r=propose(p,.25,m)
        self.assertIsNotNone(r);self.assertLess(r['length_mm'],r['before_length_mm']);self.assertEqual(m,frozen)
    def test_restricted_roi_keeps_permission(self):
        p,m=self.problem();m['task']['region_mm']=[4,14,26,16]
        self.assertIsNone(propose(p,.25,m))
    def test_no_candidate_when_budget_cancelled(self):
        p,m=self.problem()
        def check():raise TimeoutError('cancel')
        with self.assertRaises(TimeoutError):propose(p,.25,m,check)
    def test_no_improvement_keeps_shortest_straight(self):
        p,m=self.problem();p=[[10,20],[12,20],[15,20],[18,20],[20,20]]
        self.assertIsNone(propose(p,.25,m))
if __name__=='__main__':unittest.main()
