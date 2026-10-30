import copy
import unittest
from pcb_local.recommendations import recommend,apply
from pcb_local.discovery import discover,select_task
from test_modification import snapshot


def example():
    s=snapshot();s['copper_layers']=['F.Cu','B.Cu']
    for comp in s['components']:comp['pads']=[k for k in comp['items'] if s['items'][k]['kind']=='pad']
    for uid,row in s['items'].items():
        row['netclass']={'track_width_mm':.25,'clearance_mm':.2}
        if row['kind']=='pad':row.update(reference=uid,number='1')
    # Target chord crosses the anchored two-track neighbour component.
    s['items']['t1']['position_mm']=[1,-1];s['items']['t2']['position_mm']=[1,1]
    listing=discover(s,[-2,-2,4,3],'F.Cu')
    return s,listing


class Recommendations(unittest.TestCase):
    def test_read_only_and_exact_object_permission(self):
        s,l=example();original=copy.deepcopy(s);proposals=recommend(s,l)
        self.assertEqual(s,original);self.assertEqual(len(proposals),1)
        p=proposals[0];task=select_task(l,target=p['target_id'])
        self.assertNotIn('modification',task)
        apply(s,task,proposals,proposals,[p['id']],'shove')
        self.assertEqual(task['modification']['routing_uuids'],['n1','n2'])
        self.assertEqual(task['modification']['max_modified_tracks'],2)
    def test_stale_proposal_cannot_expand_authorization(self):
        s,l=example();old=recommend(s,l);s['items']['n1']['locked']=True
        current=recommend(s,l)
        with self.assertRaises(ValueError):apply(s,select_task(l,target=old[0]['target_id']),current,old,[old[0]['id']],'shove')
    def test_wrong_target_or_multiple_proposals_refused(self):
        s,l=example();p=recommend(s,l);task=select_task(l,target=p[0]['target_id']);task['id']='different'
        with self.assertRaises(ValueError):apply(s,task,p,p,[p[0]['id']],'shove')
        with self.assertRaises(ValueError):apply(s,task,p,p,[p[0]['id'],p[0]['id']],'shove')
    def test_no_valid_anchored_group_means_no_suggestion(self):
        s,l=example();s['items']['n1']['locked']=True;s['items']['n2']['locked']=True
        self.assertEqual(recommend(s,l),[])

    def test_native_line_end_target_is_not_silently_skipped(self):
        s,_=example();row=s['items']['t1']
        row.update(kind='segment',start_mm=[1,-1],end_mm=[1,-2],width_mm=.25)
        s['components'][s['component_of']['t1']]['pads']=[]
        listing=discover(s,[-2,-3,4,3],'F.Cu');p=recommend(s,listing)
        self.assertTrue(p);self.assertTrue(any(t.get('endpoints') for t in listing['targets']))
        task=select_task(listing,target=p[0]['target_id'])
        apply(s,task,p,p,[p[0]['id']],'shove')
        self.assertEqual(task['modification']['routing_uuids'],['n1','n2'])


def crossing_example():
    s,_=example();s['items']['n1']['end_mm']=[2,0];s['items'].pop('n2');s['component_of'].pop('n2')
    s['components'][0]['items'].remove('n2')
    for row in s['items'].values():row['own_clearance_mm']=0
    s['routing_rules']=dict(board_minima_mm={'clearance':.2},netclasses_mm={'PLANE':{'clearance':.2}})
    s['zone_refill_choices']=[dict(uuid='plane',net='PLANE',layers=['F.Cu'],local_clearance_mm=.2,min_thickness_mm=.1)]
    return s,discover(s,[.4,-2,1.6,3],'F.Cu')


class LocalRecommendations(unittest.TestCase):
    def test_reviewed_local_authority_preserves_both_original_tails(self):
        s,l=crossing_example();original=copy.deepcopy(s);p=recommend(s,l)
        self.assertEqual(len(p),1);self.assertEqual(p[0]['scope'],'local_middle')
        self.assertEqual(s,original);task=select_task(l,target=p[0]['target_id'])
        apply(s,task,p,p,[p[0]['id']],'shove')
        self.assertEqual(task['modification']['routing_uuids'],['n1'])
        self.assertEqual(task['partial_segments'],p[0]['partial_segments'])
        self.assertEqual(task['partial_segments'][0]['children'][0]['start'],[0,0])
        self.assertEqual(task['partial_segments'][0]['children'][-1]['end'],[2,0])

    def test_local_recommendation_does_not_authorize_unimplemented_ripup(self):
        s,l=crossing_example();p=recommend(s,l)
        for mode in ('ripup','shove+ripup'):
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                apply(s,select_task(l,target=p[0]['target_id']),p,p,[p[0]['id']],mode)

    def test_new_fill_permission_only_narrows_middle_or_freezes_parent(self):
        from pcb_local.local_segments import restrict_for_refill
        for freeze in (False,True):
            s,l=crossing_example();p=recommend(s,l);t=select_task(l,target=p[0]['target_id'])
            apply(s,t,p,p,[p[0]['id']],'shove');old=t['partial_segments'][0]
            if freeze:s['zone_refill_choices'][0]['min_thickness_mm']=.5
            t['zone_refill']=dict(schema_version=1,zone_uuids=['plane']);report=restrict_for_refill(s,t)
            if freeze:
                self.assertEqual(report['frozen_uuids'],['n1']);self.assertNotIn('modification',t)
            else:
                new=t['partial_segments'][0]
                self.assertGreaterEqual(new['cut_indices'][0],old['cut_indices'][0])
                self.assertLessEqual(new['cut_indices'][1],old['cut_indices'][1])

    def test_stale_crossing_geometry_and_widened_review_are_rejected(self):
        s,l=crossing_example();p=recommend(s,l);s['items']['n1']['width_mm']=.21
        fresh=recommend(s,l)
        with self.assertRaises(ValueError):apply(s,select_task(l,target=p[0]['target_id']),fresh,p,[p[0]['id']],'shove')
