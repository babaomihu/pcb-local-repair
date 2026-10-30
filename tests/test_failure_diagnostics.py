"""Explanations carry evidence; they cannot invent proof or grant permissions."""
import unittest
from pcb_local.diagnostics import explain

class FailureDiagnostics(unittest.TestCase):
    def test_auto_rejection_uses_actual_attempt_drc(self):
        issue=dict(type='clearance',items=[dict(uuid='actual-copper',pos={'x':1,'y':2})])
        v=dict(checks={'full_drc_no_new_or_changed':False},drc={'sections':{'violations':{'new_or_changed':[issue]}}})
        d=explain(dict(status='rejected',automatic_strategy={'attempts':[dict(name='push',status='rejected',details={'verification':v})]}))
        self.assertEqual(d['category'],'candidate_drc_rejected')
        self.assertEqual(d['drc_findings'][0]['items'][0]['uuid'],'actual-copper')
    def test_rejection_without_drc_is_not_claimed_to_be_drc(self):
        self.assertEqual(explain(dict(status='rejected'))['category'],'candidate_rejected')
    def test_auto_unsupported_uses_attempt_geometry_evidence(self):
        d=explain(dict(status='unsupported',automatic_strategy={'attempts':[dict(name='push',status='unsupported',reason='unsupported copper stack')]}))
        self.assertEqual(d['category'],'unsupported_geometry')
    def test_search_failure_is_not_infeasibility_or_permission_failure(self):
        d=explain({'status':'no_progress','task':{'allowed_layer':'F.Cu'}})
        self.assertEqual(d['category'],'search_not_found');self.assertFalse(d['physical_impossibility_proven'])
        self.assertTrue(d['permission_limits']);self.assertIn('有界',d['summary'])
    def test_empty_supported_list_not_claim_of_complete_board(self):
        d=explain({'status':'complete','discovery':{'supported_target_count':0}})
        self.assertEqual(d['category'],'no_supported_targets');self.assertIn('支持范围',d['summary'])
    def test_actual_drc_items_retained(self):
        issue=dict(type='clearance',description='gap below class clearance',items=[dict(uuid='fixed-via',pos={'x':1,'y':2})])
        d=explain({'status':'rejected','verification':{'checks':{'full_drc_no_new_or_changed':False},'drc':{'sections':{'violations':{'new_or_changed':[issue]}}}}})
        self.assertEqual(d['category'],'candidate_drc_rejected');self.assertEqual(d['drc_findings'][0]['items'],issue['items'])
    def test_verification_cap_remains_incomplete(self):
        d=explain({'status':'verification_incomplete','preflight':{'measurement':{'coverage':{'reasons':['native DRC limit reached']}}}})
        self.assertEqual(d['category'],'verification_incomplete');self.assertEqual(d['coverage_reasons'],['native DRC limit reached'])
    def test_attempt_failure_does_not_override_accepted_result(self):
        d=explain({'status':'accepted','automatic_strategy':{'attempts':[dict(name='add-only',status='timed_out',reason='stage timeout'),dict(name='push',status='accepted',details={'pns':{'direct_path_obstacles':{'items':[dict(uuid='old-line',net='N',layer='F.Cu')]}}})]}})
        self.assertEqual(d['category'],'accepted');self.assertEqual(d['attempts'][0]['category'],'budget_exhausted')
        self.assertEqual(d['objects'][0]['evidence_kind'],'direct_corridor_intersection')
    def test_unsupported_object_keeps_identity_and_position(self):
        obj=dict(uuid='custom-hole',net='N',layer='In1.Cu',position_mm=[3,4])
        d=explain({'status':'unsupported','reason':'Unsupported pad geometry','pns':{'unsupported_items':[obj]}})
        self.assertEqual(d['category'],'unsupported_geometry');self.assertEqual(d['objects'][0]['uuid'],'custom-hole')
        self.assertEqual(d['objects'][0]['position_mm'],[3,4])
