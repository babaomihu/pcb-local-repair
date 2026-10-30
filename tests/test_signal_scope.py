import copy
import unittest
from pcb_local.signal_scope import net_restriction, restrictions
from pcb_local.discovery import discover_automatic, select_task, validate_endpoints
from pcb_local.modification import select, witness
from pcb_local.recommendations import recommend
from test_auto_region import snapshot
from test_recommendations import example

class SignalScope(unittest.TestCase):
    def test_authored_pin_semantics_override_anonymous_net_and_old_target(self):
        for info in [{'pin_type':'power_in'}, {'pin_type':'power_out'},
                     {'pin_type':'passive','pin_function':'SHIELD'},
                     {'pin_function':'CHASSIS'}, {'pin_function':'AGND'},
                     {'pin_type':'passive', 'pin_function':'GND'}]:
            with self.subTest(info=info):
                s=snapshot();s['component_of']={'0':0,'1':1,'2':2}
                old=discover_automatic(s,'F.Cu');task=select_task(old,target=old['targets'][0]['id'])
                s['items']['1'].update(info)
                self.assertEqual(discover_automatic(s,'F.Cu')['targets'],[])
                with self.assertRaisesRegex(ValueError,'Authored pin'):validate_endpoints(task,s)

    def test_pin_semantics_on_remote_pad_and_modification_neighbour(self):
        s=snapshot();s['items']['far']={**s['items']['0'],'uuid':'far','position_mm':[500,500], 'pin_type':'power_in'}
        self.assertEqual(discover_automatic(s,'F.Cu')['targets'],[])
        s,l=example();task=select_task(l,target=l['targets'][0]['id'])
        pad=next(x for x in s['items'].values() if x['kind']=='pad' and x['net']=='N')
        pad['pin_function']='SHIELD';self.assertEqual(recommend(s,l),[])
        with self.assertRaises(ValueError):select(s,copy.deepcopy(task),['N'],2)

    def test_passive_input_and_shield_enable_are_not_inferred_power(self):
        for info in [{}, {'pin_type':'passive'}, {'pin_type':'input'},
                     {'pin_type':'output','pin_function':'SHIELD_ENABLE'}]:
            s=snapshot();s['items']['0'].update(info)
            self.assertEqual(len(discover_automatic(s,'F.Cu')['targets']),1)

    def test_rail_names_with_general_suffixes(self):
        for net in ['VCC-IO','/cpu/AVDD.ANALOG','VSS-core','VBAT+BACKUP','GND-isolated','+3V3']:
            with self.subTest(net=net):self.assertIsNotNone(net_restriction(net,{net}))
        self.assertIsNone(net_restriction('DATA_VALID',{'DATA_VALID'}))

    def test_class_exclusions_even_when_net_is_anonymous(self):
        for label in ['Power','pwr','Power-Analog','Ground','Differential','High Speed','Length-Matched','Timing','电源','差分','PowerPlane','USBdiff','高速网络']:
            s=snapshot();s['items']['0']['netclass']={'name':'Default','members':['Default',label]}
            with self.subTest(label=label):
                l=discover_automatic(s,'F.Cu');self.assertEqual(l['targets'],[])
                row=next(x for x in l['nets'] if x['net']=='DATA');self.assertFalse(row['supported']);self.assertIn(label,row['reason'])

    def test_direct_task_and_old_target_cannot_bypass_class(self):
        s=snapshot();s['component_of']={'0':0,'1':1,'2':2}
        old=discover_automatic(s,'F.Cu');task=select_task(old,target=old['targets'][0]['id'])
        s['items']['1']['netclass']={'name':'Power','members':['Power']}
        fresh=discover_automatic(s,'F.Cu')
        with self.assertRaises(ValueError):select_task(fresh,target=task['id'])
        with self.assertRaisesRegex(ValueError,'signal scope'):validate_endpoints(task,s)
        fresh.pop('mode')
        with self.assertRaises(ValueError):select_task(fresh,net='DATA',pads=['p0','p1'])
        with self.assertRaises(ValueError):select_task(fresh,net='DATA')

    def test_unclassified_signal_still_requires_external_intent_confirmation(self):
        s=snapshot();s['items']['0']['netclass']={'name':'GPIO','members':['GPIO']}
        l=discover_automatic(s,'F.Cu');self.assertEqual(len(l['targets']),1)
        self.assertIn('design-intent confirmation',l['signal_review'])

    def test_class_on_remote_net_object_cannot_be_ignored(self):
        s=snapshot();s['items']['3']={**s['items']['0'],'uuid':'far','position_mm':[500,500],'netclass':{'name':'Power'}}
        self.assertIsNotNone(restrictions(s)['DATA'])
        self.assertEqual(discover_automatic(s,'F.Cu')['targets'],[])

    def test_neighbour_modification_and_recommendations_share_scope(self):
        s,l=example();task=select_task(l,target=l['targets'][0]['id']);select(s,task,['N'],2)
        s['items']['n1']['netclass']['name']='Power'
        self.assertEqual(recommend(s,l),[])
        with self.assertRaises(ValueError):select(s,copy.deepcopy(task),['N'],2)
        with self.assertRaisesRegex(ValueError,'unsupported'):witness(s,task)

if __name__=='__main__':unittest.main()
