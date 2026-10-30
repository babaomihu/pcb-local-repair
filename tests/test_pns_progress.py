import unittest
from pcb_local.pns_progress import observe, summarize


class Router:
    def __init__(self, head=(1.,2.,0), net=3):self.head=head;self.net=net
    def get_route_head(self):return self.head
    def get_current_net_code(self):return self.net


class ProgressTests(unittest.TestCase):
    def attempt(self, row, **kw):
        return dict(started=True,committed=False,end=[4.,6.],progress=[row],**kw)

    def test_move_true_does_not_mean_arrived(self):
        row=observe(Router(),[4.,6.],0,3,'endpoint')
        a=self.attempt(row,moved=True)
        self.assertEqual(row['distance_to_cursor_mm'],5.)
        self.assertEqual(summarize(a)['stage'],'endpoint_not_reached')

    def test_exact_cursor_is_not_accepted_connectivity(self):
        row=observe(Router((4.,6.,0)),[4.,6.],0,3,'endpoint')
        self.assertTrue(row['cursor_exact_nm'])
        self.assertEqual(summarize(self.attempt(row))['stage'],'commit_rejected_after_cursor_arrival')

    def test_committed_off_cursor_proposal_is_not_discarded(self):
        a=self.attempt(observe(Router(),[4.,6.],0,3,'endpoint'));a['committed']=True
        self.assertEqual(summarize(a)['stage'],'proposal_generated_requires_native_validation')

    def test_committed_proposal_keeps_head_and_remaining_distance(self):
        row=observe(Router(),[4.,6.],0,3,'endpoint');a=self.attempt(row);a['committed']=True
        result=summarize(a)
        self.assertEqual(result['last_active_observation'],row)
        self.assertEqual(result['distance_to_target_mm'],5.)

    def test_idle_is_not_a_position_at_origin(self):
        row=observe(Router((0.,0.,-1),-1),[4.,6.],0,3,'endpoint')
        self.assertIsNone(row['head_mm']);self.assertNotIn('distance_to_cursor_mm',row)
        self.assertEqual(summarize(self.attempt(row))['stage'],'no_active_trace_observed')

    def test_waypoint_failure_survives_later_cancelled_session(self):
        row=observe(Router(),[8.,2.],0,3,'waypoint-0');a=self.attempt(row)
        a['progress'].append(observe(Router((0.,0.,-1)),[4.,6.],0,3,'endpoint'))
        self.assertEqual(summarize(a)['stage'],'waypoint_not_reached')

    def test_wrong_layer_or_net_reported(self):
        for head,net,stage in [((1.,2.,2),3,'active_layer_mismatch'),((1.,2.,0),4,'active_net_mismatch')]:
            with self.subTest(stage=stage):self.assertEqual(summarize(self.attempt(observe(Router(head,net),[4.,6.],0,3,'endpoint')))['stage'],stage)

    def test_optional_api_failure_does_not_raise(self):
        row=observe(object(),[4.,6.],0,3,'endpoint')
        self.assertEqual(row['status'],'unavailable')
        self.assertEqual(summarize(self.attempt(row))['stage'],'progress_unavailable')

    def test_nonfinite_and_malformed_head_not_serialized_as_evidence(self):
        for head in [(float('nan'),2.,0),(1.,2.),(1.,2.,.5)]:
            with self.subTest(head=head):self.assertEqual(observe(Router(head),[4.,6.],0,3,'endpoint')['status'],'unavailable')

    def test_start_failure(self):
        self.assertEqual(summarize({'started':False})['stage'],'start_rejected')


class ReportProgress(unittest.TestCase):
    def test_failure_report_keeps_cursor_evidence_without_changing_acceptance(self):
        from pcb_local.diagnostics import explain,markdown
        report={'status':'no_progress','pns':{'worker':{'attempts':[{'progress_summary':{'stage':'endpoint_not_reached','distance_to_target_mm':3.,'last_active_observation':{'head_mm':[1.,2.]}}}]}}}
        d=explain(report)
        self.assertEqual(d['category'],'search_not_found')
        self.assertFalse(d['physical_impossibility_proven'])
        self.assertEqual(d['pns_progress'][0]['distance_to_target_mm'],3.)
        self.assertIn('实际停点 [1.0, 2.0]',markdown(d))

if __name__=='__main__':unittest.main()
