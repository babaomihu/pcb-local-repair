import copy
import unittest
from pcb_local.zone_refill import contract, search_model
from pcb_local.pns_geometry import copper, prepare, verify
from test_pns_geometry import source, projected


def model():
    s = source()
    s['task'].update(allowed_layer='F.Cu', target_net='signal', zone_refill=dict(schema_version=1, zone_uuids=['plane']))
    s['primitives'][0].update(locked=False, source_owner_uuid=None)
    s['refilled_geometry_sha256'] = 'bound-native-source'
    return s


class RefillSearch(unittest.TestCase):
    def test_explicit_only_and_no_source_mutation(self):
        s = model(); old = copy.deepcopy(s)
        changed, proof = search_model(s)
        self.assertEqual(s, old)
        self.assertTrue(proof['zones'][0]['outside_region_copper_preserved'])
        self.assertLess(copper(changed['primitives'][0]['polygons']).area, copper(s['primitives'][0]['polygons']).area)
        del s['task']['zone_refill']
        self.assertEqual(search_model(s), (s, None))

    def test_authorization_and_object_type(self):
        for key, value in [('locked', True), ('net', 'signal'), ('source_owner_uuid', 'fp')]:
            s=model();s['primitives'][0][key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):search_model(s)
        s=model();s['task']['zone_refill']['zone_uuids']=['absent']
        with self.assertRaises(ValueError):search_model(s)

    def test_fully_local_zone_can_leave_empty_solver_fill(self):
        s=model();s['primitives'][0]['polygons']=s['primitives'][0]['polygons'][:1]
        changed,proof=search_model(s)
        self.assertEqual(changed['primitives'][0]['polygons'],[])
        p=prepare(s)
        self.assertEqual(verify(s,projected(s,p),p)['status'],'verified')

    def test_invalid_contract(self):
        for ids in [[], ['plane','plane'], [None], 'plane']:
            s=model();s['task']['zone_refill']['zone_uuids']=ids
            with self.assertRaises(ValueError):contract(s['task'])

    def test_original_line_or_via_endpoints_do_not_change_fill_permission(self):
        # Endpoint identity/geometry is checked by native endpoint witnesses;
        # changing the terminal type must not alter which fill may be removed.
        s=model();expected,proof=search_model(s)
        for kind in ('segment','via'):
            bound=copy.deepcopy(s)
            bound['task']['target_endpoints']=[{'kind':'pad'},{'kind':kind}]
            changed,actual=search_model(bound)
            self.assertEqual(changed['primitives'],expected['primitives'])
            self.assertEqual(actual,proof)

    def test_endpoint_extension_does_not_grant_new_vias_or_refill_other_layers(self):
        s=model();s['task']['target_endpoints']=[{'kind':'pad'},{'kind':'via'}]
        for layers,vias in [(['F.Cu','B.Cu'],0),(['F.Cu','B.Cu'],1)]:
            s['task']['permissions']=dict(schema_version=1,route_layers=layers,max_new_vias=vias,via_type='through')
            with self.assertRaises(ValueError):contract(s['task'])

    def test_projected_geometry_proof_cannot_change_permission(self):
        s=model();p=prepare(s);r=projected(s,p)
        self.assertEqual(verify(s,r,p)['status'],'verified')
        p['zone_refill']['region_mm']=[0,0,10,10]
        with self.assertRaisesRegex(ValueError,'permission'):verify(s,r,p)

    def test_other_layer_and_other_zone_stay_fixed(self):
        s=model();s['auxiliary_layers']={'B.Cu':copy.deepcopy(s)}
        extra=copy.deepcopy(s['primitives'][0]);extra['uuid']='other';s['primitives'].append(extra)
        r,_=search_model(s)
        self.assertEqual(r['auxiliary_layers'],s['auxiliary_layers'])
        self.assertEqual(r['primitives'][1],s['primitives'][1])

    def test_fractional_nm_intersection_keeps_conservative_copper(self):
        s=model();s['task']['region_mm']=[3,1,9,9]
        s['primitives'][0]['polygons']=[dict(shell=[[2,2],[8,2.000001],[8,8],[2,8]],holes=[])]
        r,proof=search_model(s)
        from shapely.geometry import box
        self.assertTrue(copper(r['primitives'][0]['polygons']).covers(copper(s['primitives'][0]['polygons']).difference(box(*s['task']['region_mm']))))
        self.assertGreater(proof['zones'][0]['extra_obstacle_area_mm2'],0)
