"""Automatic discovery must never turn read-only scan bounds into permissions."""
import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from pcb_local.discovery import discover_automatic, select_task


def snapshot():
    items = {str(i): {'kind':'pad','uuid':'p'+str(i),'net':'DATA' if i<2 else 'OTHER',
        'reference':'U'+str(i),'number':'1','position_mm':p,'copper_layers':['F.Cu']}
        for i,p in enumerate(([10.,10.],[11.,11.],[200.,200.]))}
    return {'copper_layers':['F.Cu'],'items':items,
        'components':[{'net':v['net'],'pads':[k]} for k,v in items.items()]}


class AutoRegionTests(unittest.TestCase):
    def test_far_away_items_cannot_expand_repair_scope(self):
        listing=discover_automatic(snapshot(),'F.Cu')
        self.assertGreater(listing['scan_region_mm'][2],200)
        target=listing['targets'][0]
        task=select_task(listing,target=target['id'])
        self.assertEqual(task['region_mm'],[8.,8.,13.,13.])
        self.assertEqual(task['target_pad_uuids'],target['pad_uuids'])

    def test_oversized_connection_is_not_automatically_authorized(self):
        s=snapshot();s['items']['1']['position_mm']=[40.,10.]
        listing=discover_automatic(s,'F.Cu');target=listing['targets'][0]
        self.assertFalse(target['supported'])
        with self.assertRaisesRegex(ValueError,'25 mm'):
            select_task(listing,target=target['id'])

    def test_whole_net_request_is_not_supported_in_auto_mode(self):
        with self.assertRaisesRegex(ValueError,'one target'):
            select_task(discover_automatic(snapshot(),'F.Cu'),net='DATA')

    def test_connected_nets_are_not_offered_as_repairs(self):
        s=snapshot()
        s['components']=[{'net':'DATA','pads':['0','1']},{'net':'OTHER','pads':['2']}]
        listing=discover_automatic(s,'F.Cu')
        self.assertEqual(listing['targets'],[])

    def test_pads_in_one_connected_component_do_not_create_extra_targets(self):
        s=snapshot()
        s['items']['3']={**s['items']['0'],'uuid':'p3','position_mm':[10.5,10.5]}
        s['components'][0]['pads'].append('3')
        listing=discover_automatic(s,'F.Cu')
        self.assertEqual(len(listing['targets']),1)
        self.assertNotEqual(set(listing['targets'][0]['pad_uuids']),{'p0','p3'})

    def test_policy_and_regions_are_deterministic_and_input_untouched(self):
        s=snapshot(); before=copy.deepcopy(s)
        self.assertEqual(discover_automatic(s,'F.Cu'),discover_automatic(s,'F.Cu'))
        self.assertEqual(s,before)

    def test_invalid_or_missing_endpoint_geometry_fails_closed(self):
        s=snapshot();s['items']['0']['position_mm']=[float('nan'),0]
        with self.assertRaisesRegex(ValueError,'Invalid native endpoint'):
            discover_automatic(s,'F.Cu')
        with self.assertRaises(ValueError):
            discover_automatic(snapshot(),'B.Cu')

    def test_read_only_scanning_honors_cancellation(self):
        def stop():raise InterruptedError('cancel')
        with self.assertRaises(InterruptedError):
            discover_automatic(snapshot(),'F.Cu',check=stop)

if __name__=='__main__':unittest.main()
