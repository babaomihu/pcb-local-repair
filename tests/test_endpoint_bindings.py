"""Native endpoint identities cannot be substituted, moved or granted away."""
import copy
import hashlib
import unittest
from pcb_local.discovery import discover,discover_automatic,select_task,validate_endpoints
from pcb_local.endpoints import catalog
from pcb_local.modification import witness
from pcb_local.validation import connectivity_delta


def snapshot():
    nc=dict(name='Default',track_width_mm=.25,clearance_mm=.2)
    def pad(uid,x):return dict(kind='pad',uuid=uid,net='DATA',copper_layers=['F.Cu'],position_mm=[x,0],reference=uid,number='1',netclass=nc,locked=False)
    def line(uid,a,b):return dict(kind='segment',uuid=uid,net='DATA',copper_layers=['F.Cu'],position_mm=[a,0],start_mm=[a,0],end_mm=[b,0],width_mm=.25,netclass=nc,locked=False)
    items={'pad:a':pad('a',0),'left':line('left',0,3),'right':line('right',7,10),'pad:b':pad('b',10)}
    return dict(items=items,copper_layers=['F.Cu','B.Cu'],components=[dict(net='DATA',items=['pad:a','left'],pads=['pad:a']),dict(net='DATA',items=['right','pad:b'],pads=['pad:b'])],component_of={'pad:a':0,'left':0,'right':1,'pad:b':1})


def line_task(native):
    listing=discover(native,[2,-1,8,1],'F.Cu');return select_task(listing,target=listing['targets'][0]['id'])


class EndpointBindingTests(unittest.TestCase):
    def test_local_gap_selects_distinct_native_line_components(self):
        native=snapshot();task=line_task(native)
        self.assertEqual([e['position_mm'] for e in task['target_endpoints']],[[3,0],[7,0]])
        self.assertEqual(task['target_item_ids'],['left','right'])
        self.assertEqual(task['target_pad_uuids'],[])
        self.assertTrue(validate_endpoints(task,native))
        unchanged=copy.deepcopy(native)
        self.assertFalse(connectivity_delta(native,unchanged,task)['target_connected'])
    def test_legacy_pad_target_identity_is_preserved(self):
        native=snapshot();native['items']={k:v for k,v in native['items'].items() if v['kind']=='pad'}
        native['components']=[dict(net='DATA',items=['pad:a'],pads=['pad:a']),dict(net='DATA',items=['pad:b'],pads=['pad:b'])]
        native['component_of']={'pad:a':0,'pad:b':1}
        listing=discover(native,[-1,-1,11,1],'F.Cu');row=listing['targets'][0]
        expected=hashlib.sha256(('DATA\0a\0b').encode()).hexdigest()[:16]
        self.assertEqual(row['id'],expected)
        self.assertNotIn('endpoints',row)
        self.assertEqual(select_task(listing,target=row['id'])['target_pad_uuids'],['a','b'])
    def test_equal_distance_pad_centres_win_over_attached_line_ends(self):
        native=snapshot()
        native['items']['left'].update(start_mm=[0,0],end_mm=[-3,0])
        native['items']['right'].update(start_mm=[10,0],end_mm=[13,0])
        listing=discover(native,[-4,-1,14,1],'F.Cu');target=listing['targets'][0]
        self.assertEqual(target['pad_uuids'],['a','b'])
        self.assertNotIn('endpoints',target)
    def test_automatic_discovery_uses_near_gap_despite_remote_pads(self):
        native=snapshot()
        native['items']['pad:a']['position_mm']=[-40,0]
        native['items']['left'].update(position_mm=[-40,0],start_mm=[-40,0],end_mm=[3,0])
        native['items']['pad:b']['position_mm']=[50,0]
        native['items']['right'].update(start_mm=[7,0],end_mm=[50,0])
        listing=discover_automatic(native,'F.Cu')
        self.assertEqual(listing['supported_target_count'],1)
        target=listing['targets'][0]
        self.assertEqual([e['position_mm'] for e in target['endpoints']],[[3,0],[7,0]])
        self.assertEqual(target['region_mm'],[1,-2,9,2])
        self.assertLess(max(target['region_mm'][2]-target['region_mm'][0],target['region_mm'][3]-target['region_mm'][1]),25)
        self.assertTrue(validate_endpoints(select_task(listing,target=target['id']),native))
    def test_whole_net_pad_representatives_survive_nearer_track_choices(self):
        native=snapshot();listing=discover(native,[-1,-1,11,1],'F.Cu')
        task=select_task(listing,net='DATA')
        self.assertEqual(task['target_pad_uuids'],['a','b'])
        self.assertNotIn('target_endpoints',task)
    def test_more_than_512_endpoint_choices_is_explicitly_unsupported(self):
        native=snapshot()
        # Existing pads cannot hide the additional straight-end search work.
        for i in range(255):
            uid='chain-'+str(i)
            row=copy.deepcopy(native['items']['left']);row.update(uuid=uid,start_mm=[i/100,0],end_mm=[(i+1)/100,0])
            native['items'][uid]=row;native['components'][0]['items'].append(uid);native['component_of'][uid]=0
        listing=discover(native,[-1,-1,11,1],'F.Cu')
        self.assertEqual(listing['supported_target_count'],0)
        self.assertEqual(listing['targets'],[])
        self.assertFalse(listing['nets'][0]['supported'])
        self.assertIn('512',listing['nets'][0]['reason'])
    def test_more_than_65_components_is_explicitly_unsupported(self):
        native=snapshot();prototype=native['items']['left'];native['items']={};native['components']=[];native['component_of']={}
        for i in range(66):
            uid='isolated-'+str(i);row=copy.deepcopy(prototype)
            row.update(uuid=uid,start_mm=[i/10,0],end_mm=[i/10+.05,0])
            native['items'][uid]=row;native['components'].append(dict(net='DATA',items=[uid],pads=[]));native['component_of'][uid]=i
        listing=discover(native,[-1,-1,11,1],'F.Cu')
        self.assertEqual(listing['targets'],[])
        self.assertFalse(listing['nets'][0]['supported'])
        self.assertIn('65',listing['nets'][0]['reason'])
    def test_endpoint_identity_coordinates_layer_and_selector_are_bound(self):
        native=snapshot();task=line_task(native)
        for key,value in [('uuid','other'),('item_id','right'),('kind','via'),('selector','start'),('position_mm',[3.000001,0]),('layer','B.Cu'),('net','OTHER')]:
            with self.subTest(key=key):
                changed=copy.deepcopy(task);changed['target_endpoints'][0][key]=value
                with self.assertRaises(ValueError):validate_endpoints(changed,native)
        changed=copy.deepcopy(task);changed['target_item_ids'].reverse()
        with self.assertRaises(ValueError):validate_endpoints(changed,native)
    def test_changed_original_item_invalidates_binding(self):
        native=snapshot();task=line_task(native);altered=copy.deepcopy(native)
        altered['items']['left']['end_mm']=[3.1,0]
        with self.assertRaises(ValueError):validate_endpoints(task,altered)
        del altered['items']['left']
        with self.assertRaises(ValueError):validate_endpoints(task,altered)
    def test_connected_components_are_not_new_repair_tasks(self):
        native=snapshot();task=line_task(native);native['component_of']={key:0 for key in native['items']}
        native['components']=[dict(net='DATA',items=list(native['items']),pads=['pad:a','pad:b'])]
        self.assertFalse(validate_endpoints(task,native))
        self.assertEqual(discover(native,[2,-1,8,1],'F.Cu')['supported_target_count'],0)
    def test_fixed_through_via_discovery_and_unsupported_via_exclusion(self):
        native=snapshot();native['items']={k:v for k,v in native['items'].items() if k=='pad:a'}
        native['items']['through']=dict(kind='via',uuid='through',net='DATA',copper_layers=['F.Cu','B.Cu'],position_mm=[4,0],standard_through=True,locked=True)
        native['components']=[dict(net='DATA',items=['pad:a'],pads=['pad:a']),dict(net='DATA',items=['through'],pads=[])]
        native['component_of']={'pad:a':0,'through':1}
        listing=discover_automatic(native,'F.Cu');task=select_task(listing,target=listing['targets'][0]['id'])
        self.assertEqual({e['kind'] for e in task['target_endpoints']},{'pad','via'})
        self.assertTrue(validate_endpoints(task,native))
        native['items']['through']['standard_through']=False
        self.assertNotIn('through:center',catalog(native,'F.Cu'))
        with self.assertRaises(ValueError):validate_endpoints(task,native)
    def test_target_object_cannot_become_modifiable(self):
        native=snapshot();task=line_task(native)
        task['region_mm']=[-1,-1,11,1]
        task['modification']=dict(schema_version=1,mode='shove',routing_uuids=['left'],nets=['DATA'],max_modified_tracks=1)
        with self.assertRaisesRegex(ValueError,'endpoint objects must remain fixed'):witness(native,task)
    def test_native_gate_rejects_manifest_with_forged_endpoint_binding(self):
        native=snapshot();task=line_task(native);task['target_endpoints'][0]['position_mm']=[3.25,0]
        joined=copy.deepcopy(native);joined['component_of']={k:0 for k in joined['items']};joined['components']=[dict(net='DATA',items=list(joined['items']),pads=['pad:a','pad:b'])]
        with self.assertRaises(ValueError):connectivity_delta(native,joined,task)
    def test_endpoint_outside_roi_or_duplicate_item_refused(self):
        native=snapshot();task=line_task(native);task['region_mm']=[3.1,-1,8,1]
        with self.assertRaises(ValueError):validate_endpoints(task,native)
        task=line_task(native);task['target_endpoints'][1]=copy.deepcopy(task['target_endpoints'][0]);task['target_item_ids']=['left','left']
        with self.assertRaises(ValueError):validate_endpoints(task,native)
    def test_duplicate_pad_uuid_is_not_offered(self):
        native=snapshot();native['items']['pad:b']['uuid']='a'
        listing=discover(native,[-1,-1,11,1],'F.Cu')
        self.assertTrue(all('a' not in t['pad_uuids'] for t in listing['targets']))


if __name__=='__main__':unittest.main()
