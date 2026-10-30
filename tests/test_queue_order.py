"""Scheduling changes order only; geometry scores are explicitly heuristic."""
import copy
import unittest
from unittest.mock import patch
from pcb_local.queue_order import order_targets


def target(uid,a,b,net=None,margin=2.):
    return dict(id=uid,net=net or uid,pads=[dict(uuid=uid+'a',position_mm=a),dict(uuid=uid+'b',position_mm=b)],
                region_mm=[min(a[0],b[0])-margin,min(a[1],b[1])-margin,max(a[0],b[0])+margin,max(a[1],b[1])+margin])


def native(items=None):
    return dict(copper_layers=['F.Cu','B.Cu'],items=items or {})


class QueueOrder(unittest.TestCase):
    def test_constrained_first_preserves_identity_inputs_and_authority(self):
        easy=target('easy',[0,5],[10,5]);hard=target('hard',[0,0],[10,0])
        snapshot=native({'wall':dict(uuid='wall',kind='segment',net='N',copper_layers=['F.Cu'],start_mm=[5,-1],end_mm=[5,1],width_mm=.3,locked=True)})
        before=copy.deepcopy(([easy,hard],snapshot))
        result,evidence=order_targets([easy,hard],snapshot,'F.Cu')
        self.assertEqual([x['id'] for x in result],['hard','easy']);self.assertIs(result[0],hard)
        self.assertEqual(([easy,hard],snapshot),before)
        self.assertEqual(evidence['extra_attempts'],0);self.assertFalse(evidence['physical_blockage_proven'])
        self.assertEqual(evidence['scores'][1]['nearby_objects'][0]['uuid'],'wall')

    def test_ties_preserve_selected_order(self):
        a=target('b',[0,0],[1,0]);b=target('a',[0,5],[1,5])
        result,evidence=order_targets([a,b],native(),'F.Cu')
        self.assertEqual(result,[a,b]);self.assertEqual(evidence['original_order'],evidence['execution_order'])

    def test_duplicate_id_is_rejected_not_deduplicated(self):
        a=target('same',[0,0],[1,0])
        with self.assertRaisesRegex(ValueError,'unique'):order_targets([a,copy.deepcopy(a)],native(),'F.Cu')

    def test_crossing_corridors_raise_selected_pair_priority(self):
        independent=target('outside',[20,0],[30,0]);a=target('a',[0,0],[10,0]);b=target('b',[5,-3],[5,3])
        result,evidence=order_targets([independent,a,b],native(),'F.Cu')
        self.assertEqual([x['id'] for x in result],['a','b','outside'])
        self.assertEqual(evidence['scores'][1]['shared_corridor_targets'],['b'])

    def test_locked_offlayer_and_same_net_copper_do_not_invent_pressure(self):
        a=target('a',[0,0],[10,0])
        items={'same':dict(uuid='same',kind='segment',net='a',copper_layers=['F.Cu'],start_mm=[5,-1],end_mm=[5,1],width_mm=.3,locked=True),
               'back':dict(uuid='back',kind='via',net='N',copper_layers=['B.Cu'],position_mm=[5,0])}
        _,evidence=order_targets([a],native(items),'F.Cu')
        self.assertEqual(evidence['scores'][0]['nearby_object_count'],0)

    def test_generic_track_endpoint_rows_supported(self):
        a=target('a',[0,0],[10,0]);a['endpoints']=a.pop('pads')
        a['endpoints'][0].update(kind='segment',selector='end',item_id='segment-id')
        result,_=order_targets([a],native(),'F.Cu');self.assertEqual(result,[a])

    def test_sampling_and_unknown_geometry_are_reported_conservatively(self):
        a=target('a',[0,0],[10,0]);items={'x':dict(kind='pad',uuid='x',net='N',copper_layers=['F.Cu'],position_mm=[5,0]),'y':dict(kind='arc',uuid='y',net='N',copper_layers=['F.Cu'])}
        with patch('pcb_local.queue_order.MAX_SCORED_OBJECTS',1):
            _,evidence=order_targets([a],native(items),'F.Cu')
        self.assertTrue(evidence['object_sampling_truncated']);self.assertEqual(evidence['objects_considered'],1)
        self.assertFalse(evidence['physical_blockage_proven'])

    def test_invalid_layer_roi_or_endpoint_rejects(self):
        a=target('a',[0,0],[10,0])
        with self.assertRaisesRegex(ValueError,'layer'):order_targets([a],native(),'In1.Cu')
        a['pads'][0]['position_mm']=[float('nan'),0]
        with self.assertRaisesRegex(ValueError,'finite'):order_targets([a],native(),'F.Cu')
        a=target('a',[0,0],[10,0]);a['region_mm']=[1,-2,12,2]
        with self.assertRaisesRegex(ValueError,'outside'):order_targets([a],native(),'F.Cu')
