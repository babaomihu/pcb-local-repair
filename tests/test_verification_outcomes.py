import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pcb_local.diagnostics import explain,markdown
from pcb_local.explain_result import reexplain
from pcb_local.visual_report import build_model,render_html


def report():
    return dict(status='protection_violation',accepted=False,verification=dict(
        verification_complete=True,checks=dict(target_connected=True,existing_components_preserved=True,
        full_drc_no_new_or_changed=True,supplemental_drc_no_new_or_changed=True,drc_coverage_complete=True,
        zone_refill_scope_preserved=False),zone_refill={'zones':[dict(uuid='zone',layer='F.Cu',
        exact_equal=False,scope_preserved=False,authorized_local_refill=True,
        unauthorized_change_area_mm2=1e-12,unauthorized_change_bounds_mm=[1,2,3,4])]}))


class Outcomes(unittest.TestCase):
    def test_exact_fill_only_failure_has_precise_category(self):
        r=report();saved=copy.deepcopy(r);d=explain(r)
        self.assertEqual(d['category'],'zone_refill_scope_violation')
        self.assertEqual(d['verification_outcomes'][0]['failed_checks'],['zone_refill_scope_preserved'])
        self.assertIn('原连接保护 通过',markdown(d));self.assertEqual(r,saved)
    def test_other_protection_failure_not_hidden(self):
        for key in ['existing_routing_unchanged','rules_unchanged','existing_components_preserved']:
            r=report();r['verification']['checks'][key]=False
            self.assertEqual(explain(r)['category'],'protected_state_violation')
    def test_drc_failure_or_missing_check_not_claimed_pass(self):
        for value in [False,None,'true',1]:
            r=report();r['verification']['checks']['full_drc_no_new_or_changed']=value
            d=explain(r);self.assertEqual(d['category'],'protected_state_violation')
            self.assertIsNot(d['verification_outcomes'][0]['drc_passed'],True)
    def test_incomplete_does_not_become_scope_only(self):
        for status in ['protection_violation','verification_incomplete']:
            r=report();r['status']=status;r['verification']['verification_complete']=False
            self.assertNotEqual(explain(r)['category'],'zone_refill_scope_violation')
    def test_missing_electrical_check_not_claimed_connected(self):
        r=report();del r['verification']['checks']['target_connected']
        d=explain(r);self.assertIsNone(d['verification_outcomes'][0]['target_connected'])
        self.assertEqual(d['category'],'protected_state_violation')
    def test_auto_keeps_independent_attempt_stages(self):
        a=report();b=report();b['verification']['checks']['target_connected']=False
        r=dict(status='protection_violation',automatic_strategy={'attempts':[
            dict(name='pns',status=a['status'],details=a),dict(name='geometry',status=b['status'],details=b)]})
        d=explain(r);self.assertEqual(d['category'],'protected_state_violation')
        self.assertEqual(d['attempts'][0]['category'],'zone_refill_scope_violation')
        self.assertEqual([v['target_connected'] for v in d['verification_outcomes']],[True,False])
    def test_accepted_not_overridden_by_old_attempt(self):
        r=dict(status='accepted',accepted=True,automatic_strategy={'attempts':[dict(name='pns',status='protection_violation',details=report())]})
        self.assertEqual(explain(r)['category'],'accepted')


class OfflineExplanation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.original=self.root/'frozen';self.original.mkdir()
        self.board=self.original/'board.kicad_pcb'
        self.board.write_text('(kicad_pcb (version 20241229) (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))')
        self.r=report();self.r.update(source_board=str(self.board),candidate=None,
            input_hashes={str(self.board):hashlib.sha256(self.board.read_bytes()).hexdigest()})
        self.path=self.original/'result.json';self.path.write_text(json.dumps(self.r))
    def tearDown(self): self.tmp.cleanup()
    def model(self,bounds):
        self.r['verification']['zone_refill']['zones'][0]['unauthorized_change_bounds_mm']=bounds
        self.r['diagnosis']=explain(self.r)
        return build_model(self.r,before=self.board)
    def test_scope_overlay_preserves_bounds_layer_and_identity(self):
        m=self.model([1,2,3,4]);i=m['issues'][0]
        self.assertEqual((i['uuid'],i['layers'],i['bounds']),('zone',['F.Cu'],[1.,2.,3.,4.]))
        self.assertEqual(i['kind'],'zone_refill_scope');self.assertIn('不是实际铜形',i['description'])
        self.assertIn('zone_refill_scope',render_html(m));self.assertFalse(m['adoption_available'])
    def test_invalid_bounds_leave_unlocated_issue(self):
        for bounds in [None,[3,2,1,4],[0,0,float('inf'),2],[False,0,2,2],['1',0,2,2]]:
            m=self.model(bounds);self.assertIsNone(m['issues'][0]['bounds']);self.assertTrue(m['warnings'])
    def test_permitted_refill_not_displayed_as_violation(self):
        self.r['verification']['zone_refill']['zones'][0]['scope_preserved']=True
        self.assertEqual(self.model([1,2,3,4])['issues'],[])
    def test_reexplanation_does_not_accept_or_mutate(self):
        old=self.path.read_bytes();r=reexplain(self.path,self.root/'view')
        self.assertFalse(r['accepted']);self.assertFalse(r['revalidation_performed']);self.assertFalse(r['adoption_available'])
        self.assertEqual(self.path.read_bytes(),old);self.assertTrue((self.root/'view/comparison.html').exists())
    def test_stale_context_refused_without_output(self):
        self.board.write_text(self.board.read_text()+'\n')
        with self.assertRaises(ValueError):reexplain(self.path,self.root/'view')
        self.assertFalse((self.root/'view').exists())
    def test_unbound_candidate_refused(self):
        self.r['candidate']=str(self.board);self.path.write_text(json.dumps(self.r))
        with self.assertRaises(ValueError):reexplain(self.path,self.root/'view')
    def test_new_rule_file_not_in_recorded_context_refused(self):
        self.board.with_suffix('.kicad_dru').write_text('(version 1)')
        with self.assertRaises(ValueError):reexplain(self.path,self.root/'view')
        self.assertFalse((self.root/'view').exists())
    def test_history_directory_and_existing_output_refused(self):
        with self.assertRaises(ValueError):reexplain(self.path,self.original/'view')
        (self.root/'view').mkdir()
        with self.assertRaises(ValueError):reexplain(self.path,self.root/'view')


if __name__=='__main__':unittest.main()
