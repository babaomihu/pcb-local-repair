"""A net-wide count limit may narrow to fixed copper, never grant extra motion."""
import copy,unittest
from test_modification import snapshot
from pcb_local.modification import select,contract
from pcb_local.cli import report_text

class FixedFallback(unittest.TestCase):
    def task(self):return dict(target_net='T',target_pad_uuids=['t1','t2'],allowed_layer='F.Cu',region_mm=[-1,-1,3,3])
    def test_excess_count_freezes_all_without_arbitrary_subset(self):
        t=self.task();s=snapshot();original=copy.deepcopy(s)
        r=select(s,t,['N'],1,mode='shove',fixed_fallback=True)
        self.assertEqual(contract(t)['mode'],'none');self.assertEqual(r['selected'],[])
        self.assertEqual(t['permission_reduction']['eligible_track_count'],2)
        self.assertEqual(t['permission_reduction']['requested_max_modified_tracks'],1)
        self.assertEqual(s,original)
    def test_default_and_verify_style_selection_still_rejects(self):
        t=self.task();before=copy.deepcopy(t)
        with self.assertRaises(ValueError):select(snapshot(),t,['N'],1)
        self.assertEqual(t,before)
    def test_no_reduction_when_exact_limit_fits(self):
        t=self.task();r=select(snapshot(),t,['N'],2,fixed_fallback=True)
        self.assertNotIn('permission_reduction',t);self.assertEqual(set(r['selected']),{'n1','n2'})
    def test_invalid_budget_cannot_fall_back(self):
        for limit in [0,-1,33,True,1.0,None]:
            t=self.task()
            with self.subTest(limit=limit),self.assertRaises(ValueError):select(snapshot(),t,['N'],limit,fixed_fallback=True)
            self.assertNotIn('permission_reduction',t)
    def test_unknown_duplicate_and_empty_net_not_hidden(self):
        for nets in [[],['missing'],['N','N']]:
            with self.subTest(nets=nets),self.assertRaises(ValueError):select(snapshot(),self.task(),nets,1,fixed_fallback=True)
    def test_power_net_not_downgraded(self):
        s=snapshot()
        for row in s['items'].values():
            if row['net']=='N':row['net']='VCC'
        with self.assertRaises(ValueError):select(s,self.task(),['VCC'],1,fixed_fallback=True)
    def test_no_old_motion_or_partial_permission_survives(self):
        t=self.task();t.update(modification={'invalid':'old'},partial_segments=[{'old':True}],requested_modification_nets=['old'])
        select(snapshot(),t,['N'],1,fixed_fallback=True)
        for key in ['modification','partial_segments','requested_modification_nets']:self.assertNotIn(key,t)
    def test_other_witness_errors_remain_errors_within_budget(self):
        s=snapshot();s['items'].pop('p1');t=self.task();before=copy.deepcopy(t)
        with self.assertRaises(ValueError):select(s,t,['N'],2,fixed_fallback=True)
        self.assertEqual(t,before)
    def test_human_report_explains_effective_fixed_policy(self):
        t=self.task();select(snapshot(),t,['N'],1,fixed_fallback=True)
        text=report_text(dict(status='no_progress',task_dir='test',permission_reduction=t['permission_reduction']))
        self.assertIn('零原线修改权限',text)

if __name__=='__main__':unittest.main()
