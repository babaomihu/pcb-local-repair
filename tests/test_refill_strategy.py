import copy
import unittest
from pcb_local.refill_strategy import select
from pcb_local.modification import select as authorize
from pcb_local.auto_strategy import plans
from test_modification import snapshot


def case():
    s=snapshot()
    for row in s['items'].values():row.update(netclass={'clearance_mm':.2},own_clearance_mm=0)
    s['routing_rules']=dict(board_minima_mm={'clearance':.2},netclasses_mm={'PLANE':{'clearance':.2}})
    s['zone_refill_choices']=[dict(uuid='plane',net='PLANE',layers=['F.Cu'],local_clearance_mm=.2,min_thickness_mm=.1)]
    t=dict(target_net='T',allowed_layer='F.Cu',region_mm=[-.15,-1,3,3],zone_refill=dict(schema_version=1,zone_uuids=['plane']))
    authorize(s,t,['N'],2,'shove+ripup')
    return s,t


class RefillStrategy(unittest.TestCase):
    def test_subset_preserves_request_and_rebuilds_terminal_witness(self):
        s,t=case();original=copy.deepcopy(t);child,r=select(s,t)
        self.assertEqual(t,original);self.assertEqual(r['frozen_uuids'],['n1'])
        self.assertEqual(child['modification']['routing_uuids'],['n2'])
        self.assertEqual(r['terminal_witness']['anchors'],['n1','p2'])
        self.assertEqual(child['region_mm'],t['region_mm'])
        self.assertEqual(child['zone_refill'],t['zone_refill'])
        self.assertFalse(r['physical_impossibility_proven'])

    def test_no_refill_does_not_change_existing_modes(self):
        s,t=case();t.pop('zone_refill');child,r=select(s,t)
        self.assertEqual(child,t);self.assertIsNone(r)

    def test_all_frozen_removes_obsolete_ripup_plan(self):
        s,t=case();t['region_mm'][2]=2.15
        child,r=select(s,t)
        self.assertEqual(set(r['frozen_uuids']),{'n1','n2'})
        self.assertNotIn('modification',child)
        self.assertNotIn('requested_modification_nets',child)
        self.assertNotIn('ripup',[p[0] for p in plans(child,True)])
        self.assertTrue(all('modification' not in p[2] for p in plans(child,True)))

    def test_zone_own_and_track_own_clearance_respected(self):
        for kind in ['zone','track']:
            s,t=case()
            if kind=='zone':s['zone_refill_choices'][0]['local_clearance_mm']=1
            else:s['items']['n2']['own_clearance_mm']=1.2
            child,r=select(s,t)
            self.assertIn('n2',r['frozen_uuids'])

    def test_min_thickness_margin_also_applies_to_larger_track_override(self):
        s,t=case();s['items']['n2']['own_clearance_mm']=.8
        child,r=select(s,t)
        self.assertEqual(next(x for x in r['tracks'] if x['uuid']=='n2')['margin_mm'],1.0)

    def test_one_nm_boundary_overflow_is_not_rounded_to_permission(self):
        s,t=case();t['region_mm']=[-.4,-.4,2.4,3]
        self.assertEqual(select(s,t)[1]['frozen_uuids'],[])
        t['region_mm'][0]=-.399999
        self.assertEqual(select(s,t)[1]['frozen_uuids'],['n1'])

    def test_missing_and_invalid_native_values_do_not_assume_safe(self):
        for value in [None,-1,float('nan')]:
            s,t=case();s['zone_refill_choices'][0]['min_thickness_mm']=value
            with self.assertRaises(ValueError):select(s,t)

    def test_transform_independent(self):
        s,t=case();c,r=select(s,t)
        def transform(p):return [round(20-p[1],6),round(p[0]+7,6)]
        for row in s['items'].values():
            for k in ['position_mm','start_mm','end_mm']:
                if k in row:row[k]=transform(row[k])
        a,b,c,d=t['region_mm'];t['region_mm']=[20-d,a+7,20-b,c+7]
        self.assertEqual(select(s,t)[1]['frozen_uuids'],r['frozen_uuids'])
