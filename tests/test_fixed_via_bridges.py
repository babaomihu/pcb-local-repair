"""Fixed native via bridges: no permission/resource growth or inferred span."""
import copy
import unittest
from test_multilayer_geometry import layered
from test_geometry_search import GEOMETRY
from pcb_local.multilayer_geometry import search, fixed_via_bridges


def bridge_model(four=False):
    model=layered()
    if four:
        model['copper_layers']=['F.Cu','In1.Cu','In2.Cu','B.Cu']
        for layer in ['In1.Cu','In2.Cu']:
            model['routing_models'][layer]=copy.deepcopy(model['routing_models']['F.Cu'])
            model['via_models'][layer]=copy.deepcopy(model['via_models']['F.Cu'])
    model['task']['permissions'].update(route_layers=['F.Cu','B.Cu'],max_new_vias=0)
    for layer in set(model['routing_models'])-{'F.Cu','B.Cu'}:del model['routing_models'][layer]
    rows=[dict(uuid='fixed-'+str(i),net=model['task']['target_net'],center=point,
        diameter_mm=.6,drill_mm=.3,layers=['F.Cu','B.Cu'],copper_layers=model['copper_layers'],
        standard_through=True,locked=True) for i,point in enumerate([[3,5],[7,5]])]
    for collection in [model['routing_models'],model['via_models']]:
        for layer,m in collection.items():
            m['vias']=copy.deepcopy(rows)
            for row in rows:
                m['primitives'].append(dict(uuid=row['uuid'],kind='hole',net=row['net'],center=row['center'],
                    diameter_mm=.3,clearance_mm=.2,hole_clearance_mm=.25))
    return model


@unittest.skipUnless(GEOMETRY,'Requires pinned geometry dependencies')
class FixedViaBridges(unittest.TestCase):
    def test_zero_new_vias_fixed_bridge_crosses_wall(self):
        for four in [False,True]:
            model=bridge_model(four);before=copy.deepcopy(model);result=search(model)
            self.assertEqual(result['status'],'found')
            self.assertEqual(result['via_positions_mm'],[])
            self.assertEqual({x['uuid'] for x in result['reused_vias']},{'fixed-0','fixed-1'})
            self.assertEqual({x['layer'] for x in result['path']},{'F.Cu','B.Cu'})
            self.assertEqual(model,before)

    def test_absent_bridge_never_invents_hole(self):
        model=layered();model['task']['permissions']['max_new_vias']=0
        self.assertEqual(search(model)['status'],'no_path')

    def test_one_fixed_bridge_and_one_new_hole_share_resource_correctly(self):
        model=bridge_model();model['task']['permissions']['max_new_vias']=1
        for collection in [model['routing_models'],model['via_models']]:
            for m in collection.values():m['vias'].pop(1)
        result=search(model)
        self.assertEqual(result['status'],'found')
        self.assertEqual(len(result['via_positions_mm']),1)
        self.assertEqual([v['uuid'] for v in result['reused_vias']],['fixed-0'])

    def test_wrong_net_is_not_a_bridge(self):
        model=bridge_model()
        for collection in [model['routing_models'],model['via_models']]:
            for m in collection.values():
                for row in m['vias']:row['net']='foreign'
        self.assertEqual(search(model)['status'],'no_path')

    def test_unauthorized_layer_is_not_used(self):
        model=bridge_model();model['task']['permissions']['route_layers']=['F.Cu'];del model['routing_models']['B.Cu']
        self.assertEqual(search(model)['status'],'no_path')

    def test_inconsistent_or_missing_native_witness_rejected(self):
        for kind in ['missing-primary','missing-inner','center','drill','net','locked','span','copper-layers','duplicate','routing-witness']:
            model=bridge_model(True);row=model['via_models']['In1.Cu']['vias'][0]
            if kind=='missing-primary':model['via_models']['F.Cu']['vias'].pop(0)
            elif kind=='missing-inner':model['via_models']['In1.Cu']['vias'].pop(0)
            elif kind=='center':row['center']=[3.001,5]
            elif kind=='drill':row['drill_mm']=.35
            elif kind=='net':row['net']='other'
            elif kind=='locked':row['locked']=False
            elif kind=='span':row['layers']=['F.Cu','In1.Cu']
            elif kind=='copper-layers':row['copper_layers']=['F.Cu','B.Cu']
            elif kind=='duplicate':model['via_models']['In1.Cu']['vias'].append(copy.deepcopy(row))
            elif kind=='routing-witness':model['routing_models']['B.Cu']['vias'].pop(0)
            with self.subTest(kind=kind),self.assertRaises(ValueError):search(model)

    def test_nonstandard_vias_not_inferred(self):
        model=bridge_model()
        for collection in [model['routing_models'],model['via_models']]:
            for m in collection.values():
                for row in m['vias']:row['standard_through']=False
        self.assertEqual(search(model)['status'],'no_path')

    def test_bridge_outside_roi_cannot_enable_escape(self):
        model=bridge_model()
        for collection in [model['routing_models'],model['via_models']]:
            for m in collection.values():
                for row in m['vias']:row['center']=[row['center'][0],-.1]
        self.assertEqual(search(model)['status'],'no_path')

    def test_fractional_nm_and_coincident_ids_refused(self):
        for kind in ['fractional','duplicate-location']:
            model=bridge_model()
            for collection in [model['routing_models'],model['via_models']]:
                for m in collection.values():m['vias'][1]['center']=[7.0000001,5] if kind=='fractional' else [3,5]
            with self.subTest(kind=kind),self.assertRaises(ValueError):fixed_via_bridges(model)

if __name__=='__main__':unittest.main()
