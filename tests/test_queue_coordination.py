"""Scheduling mechanisms only: no mocked result is claimed as a PCB repair."""
import copy
import unittest
from unittest.mock import patch
from pcb_local.queue_coordination import choose_next


def scenario():
    targets = [dict(id='flexible', net='A', region_mm=[-2.,-4.,12.,4.],
                    pads=[dict(uuid='a1',position_mm=[5.,-3.]),dict(uuid='a2',position_mm=[5.,3.])]),
               dict(id='narrow', net='B', region_mm=[-.5,-.5,10.5,.5],
                    pads=[dict(uuid='b1',position_mm=[0.,0.]),dict(uuid='b2',position_mm=[10.,0.])])]
    items = {}
    for target in targets:
        for end in target['pads']:
            items[end['uuid']] = dict(kind='pad',uuid=end['uuid'],net=target['net'],
                copper_layers=['F.Cu'],position_mm=list(end['position_mm']))
    rules = dict(board_minima_mm=dict(track_width=.1,clearance=.1),
                 netclasses_mm={n:dict(track_width=.2,clearance=.1) for n in ('A','B','W')})
    return targets, dict(copper_layers=['F.Cu','B.Cu'],items=items,routing_rules=rules)


def wall(native, uid='wall', net='W', layer='F.Cu'):
    native['items'][uid]=dict(kind='segment',uuid=uid,net=net,copper_layers=[layer],
        start_mm=[4.,-1.],end_mm=[4.,1.],width_mm=.2,own_clearance_mm=.1)


class QueueCoordination(unittest.TestCase):
    def test_single_channel_before_connection_with_detour_alternatives(self):
        targets,native=scenario();frozen=copy.deepcopy((targets,native))
        chosen,evidence=choose_next(targets,native,'F.Cu')
        self.assertIs(chosen,targets[1])
        scores=evidence['scores']
        self.assertEqual(scores[0]['predicted_exhausted_neighbours'],1)
        self.assertEqual(scores[1]['predicted_exhausted_neighbours'],0)
        self.assertGreater(scores[1]['pair_losses'][0]['templates_before'],scores[1]['pair_losses'][0]['predicted_templates_lost'])
        self.assertEqual((targets,native),frozen)
        self.assertEqual(evidence['extra_attempts'],0);self.assertEqual(evidence['extra_native_inspections'],0)
        for key in ('selection_unchanged','regions_unchanged','permissions_unchanged','budgets_unchanged'):
            self.assertTrue(evidence[key])
        self.assertFalse(evidence['repair_count_gain_proven']);self.assertFalse(evidence['physical_blockage_proven'])

    def test_recompute_from_current_copper_without_stale_score_cache(self):
        targets,native=scenario()
        self.assertEqual(choose_next(targets,native,'F.Cu')[0]['id'],'narrow')
        wall(native)
        chosen,evidence=choose_next(targets,native,'F.Cu')
        self.assertEqual(chosen['id'],'flexible')
        self.assertEqual(evidence['scores'][1]['segment_clear_template_count'],0)
        self.assertTrue(all(p['segment_blocker_count'] for p in evidence['scores'][1]['templates']))

    def test_static_proximity_can_prioritize_flexible_channel_incorrectly(self):
        from pcb_local.queue_order import order_targets
        targets,native=scenario()
        native['items']['near-flexible']=dict(kind='segment',uuid='near-flexible',net='W',
            copper_layers=['F.Cu'],start_mm=[4.8,2.5],end_mm=[5.2,2.5],width_mm=.2,locked=True)
        static,_=order_targets(targets,native,'F.Cu')
        dynamic,evidence=choose_next(targets,native,'F.Cu')
        self.assertEqual(static[0]['id'],'flexible')
        self.assertEqual(dynamic['id'],'narrow')
        self.assertEqual(evidence['scores'][0]['predicted_exhausted_neighbours'],1)
        self.assertEqual(evidence['scores'][1]['predicted_exhausted_neighbours'],0)

    def test_same_net_and_off_layer_copper_do_not_become_foreign_obstacles(self):
        targets,native=scenario();wall(native,net='B');wall(native,'back',layer='B.Cu')
        _,evidence=choose_next(targets,native,'F.Cu')
        self.assertEqual(evidence['scores'][1]['segment_clear_template_count'],5)
        self.assertEqual(evidence['segments_considered'],1)

    def test_native_rules_not_display_metadata_drive_clearance(self):
        targets,native=scenario()
        for target in targets:
            for end in target['pads']:end['netclass']=dict(track_width_mm=.0001,clearance_mm=0)
        native['routing_rules']['board_minima_mm'].update(track_width=.3,clearance=.25)
        _,evidence=choose_next(targets,native,'F.Cu')
        self.assertTrue(all(r['width_mm']==.3 and r['clearance_mm']==.25 for r in evidence['scores']))

    def test_translated_and_renamed_geometry_has_same_decision(self):
        targets,native=scenario()
        for t in targets:
            t['id']='renamed-'+t['id']
            t['region_mm']=[v+(31.137 if i%2==0 else -12.333) for i,v in enumerate(t['region_mm'])]
            for e in t['pads']:e['position_mm']=[e['position_mm'][0]+31.137,e['position_mm'][1]-12.333]
        for item in native['items'].values():item['position_mm']=[item['position_mm'][0]+31.137,item['position_mm'][1]-12.333]
        self.assertEqual(choose_next(targets,native,'F.Cu')[0]['id'],'renamed-narrow')

    def test_ties_keep_selected_order_and_manual_roi_supported(self):
        targets,native=scenario();one=targets[:1];roi=one[0].pop('region_mm')
        self.assertIs(choose_next(one,native,'F.Cu',region_mm=roi)[0],one[0])
        targets,native=scenario();targets[1]['net']='A'
        for item in native['items'].values():item['net']='A'
        self.assertIs(choose_next(targets,native,'F.Cu')[0],targets[0])

    def test_identity_and_coordinate_substitution_rejected(self):
        for key,value in [('uuid','missing'),('kind','via'),('item_id','other'),('layer','B.Cu'),('net','W'),('position_mm',[5.000001,-3.])]:
            targets,native=scenario();targets[0]['pads'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):choose_next(targets,native,'F.Cu')
        targets,native=scenario();targets[0]['pads'][1]=copy.deepcopy(targets[0]['pads'][0])
        with self.assertRaises(ValueError):choose_next(targets,native,'F.Cu')

    def test_line_and_via_endpoints_remain_bound_to_original_items(self):
        targets,native=scenario();target=targets[0]
        native['items']['a1'].update(kind='segment',start_mm=[4.,-3.],end_mm=[5.,-3.],width_mm=.2)
        native['items']['a2'].update(kind='via',standard_through=True)
        target['endpoints']=target.pop('pads')
        target['endpoints'][0].update(item_id='a1',kind='segment',selector='end')
        target['endpoints'][1].update(item_id='a2',kind='via',selector='center')
        self.assertIn(choose_next(targets,native,'F.Cu')[0],targets)
        target['endpoints'][0]['selector']='start'
        with self.assertRaises(ValueError):choose_next(targets,native,'F.Cu')

    def test_missing_rules_invalid_roi_and_duplicate_ids_rejected(self):
        targets,native=scenario();native['routing_rules']['netclasses_mm'].pop('A')
        with self.assertRaises(ValueError):choose_next(targets,native,'F.Cu')
        targets,native=scenario();targets[0]['region_mm'][0]=float('nan')
        with self.assertRaises(ValueError):choose_next(targets,native,'F.Cu')
        targets,native=scenario();targets[1]['id']=targets[0]['id']
        with self.assertRaises(ValueError):choose_next(targets,native,'F.Cu')

    def test_sampling_and_complex_geometry_limitations_are_visible(self):
        targets,native=scenario();wall(native)
        native['items']['curve']=dict(kind='arc',uuid='curve',net='W',copper_layers=['F.Cu'])
        with patch('pcb_local.queue_coordination.MAX_SEGMENTS',0):
            _,evidence=choose_next(targets,native,'F.Cu')
        self.assertTrue(evidence['segment_sampling_truncated'])
        self.assertEqual(evidence['unmodelled_objects']['arc'],1)
        self.assertIn('filled copper',evidence['limitations'])
        self.assertFalse(evidence['physical_blockage_proven'])

    def test_cancellation_propagates(self):
        targets,native=scenario()
        def stop():raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError,'cancelled'):choose_next(targets,native,'F.Cu',check=stop)


if __name__=='__main__':unittest.main()
