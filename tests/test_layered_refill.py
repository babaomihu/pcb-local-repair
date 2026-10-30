import copy
import unittest
from unittest.mock import patch
from pcb_local.zone_refill import contract, layer_contract, search_model, bind_selection
from pcb_local.zone_fill_witness import compare
from test_zone_fill_witness import snapshot, task, rectangle
from test_zone_refill import model


def layered():
    value=task()
    value['permissions']=dict(schema_version=1,route_layers=['F.Cu','B.Cu'],max_new_vias=2,via_type='through')
    value['zone_refill']=dict(schema_version=2,zones=[dict(uuid='plane',layers=['F.Cu','B.Cu'])])
    return value


class LayeredRefill(unittest.TestCase):
    def test_unselected_inner_layers_remain_exactly_fixed(self):
        from pcb_local.multilayer_geometry import search
        source=model();source['task'].update(layered())
        source['task']['permissions']['max_new_vias']=0
        source['task']['zone_refill']['zones'][0]['layers']=['F.Cu']
        stack=['F.Cu','In1.Cu','In2.Cu','B.Cu']
        source['copper_layers']=stack
        source['routing_models']={l:dict(copy.deepcopy(source),task=dict(source['task'],allowed_layer=l)) for l in ['F.Cu','B.Cu']}
        source['via_models']={l:dict(copy.deepcopy(model()),task=dict(source['task'],allowed_layer=l)) for l in stack}
        original=copy.deepcopy(source)
        with patch('pcb_local.multilayer_geometry._search_core',return_value={'status':'no_path'}) as core:
            search(source)
        proposal=core.call_args.args[0]
        for l in ['In1.Cu','In2.Cu','B.Cu']:
            self.assertEqual(proposal['via_models'][l],original['via_models'][l])
        self.assertEqual(source,original)

    def test_binding_uses_only_explicit_native_zone_layer_intersections(self):
        value=layered()
        selected=bind_selection(value,['front','back'],[dict(uuid='front',layers=['F.Cu']),dict(uuid='back',layers=['B.Cu'])],True)
        self.assertEqual(selected['zones'],[dict(uuid='front',layers=['F.Cu']),dict(uuid='back',layers=['B.Cu'])])
        with self.assertRaises(ValueError):bind_selection(value,['missing'],[],True)
        with self.assertRaises(ValueError):bind_selection(value,['front','front'],[dict(uuid='front',layers=['F.Cu'])],True)

    def test_auto_preserves_refill_witness_for_every_candidate(self):
        from pcb_local.auto_strategy import plans
        value=layered();frozen=copy.deepcopy(value)
        rows=plans(value,True)
        self.assertEqual([r[1] for r in rows],['geometry-multilayer','krt'])
        self.assertTrue(all(r[2]['zone_refill']==value['zone_refill'] for r in rows))
        self.assertTrue(all(r[2]['permissions']==value['permissions'] for r in rows))
        self.assertEqual(value,frozen)

    def test_two_layers_allowed_without_changing_original_contract(self):
        value=layered();old=copy.deepcopy(value)
        self.assertEqual(contract(value),['plane'])
        before=snapshot();after=copy.deepcopy(before)
        for layer in ['F.Cu','B.Cu']:
            after['zones'][0]['fills'][layer][0]['holes']=[rectangle(3,3,4,4)['shell']]
        self.assertTrue(compare(before,after,value)['checks']['zone_refill_scope_preserved'])
        self.assertEqual(value,old)

    def test_one_nm_outside_on_either_layer_remains_violation(self):
        for layer in ['F.Cu','B.Cu']:
            before=snapshot();after=copy.deepcopy(before)
            after['zones'][0]['fills'][layer][0]['holes']=[rectangle(1.999999,3,4,4)['shell']]
            self.assertFalse(compare(before,after,layered())['checks']['zone_refill_scope_preserved'])

    def test_layer_pair_selection_not_blanket_zone_permission(self):
        value=layered();value['zone_refill']['zones'][0]['layers']=['F.Cu']
        before=snapshot();after=copy.deepcopy(before)
        after['zones'][0]['fills']['B.Cu'][0]['holes']=[rectangle(3,3,4,4)['shell']]
        self.assertFalse(compare(before,after,value)['checks']['zone_refill_scope_preserved'])

    def test_unselected_zone_and_metadata_protected(self):
        for variant in ['other','definition']:
            before=snapshot();after=copy.deepcopy(before)
            if variant=='other':after['zones'][1]['fills']['F.Cu']=[]
            else:after['zones'][0]['net']='changed'
            self.assertFalse(compare(before,after,layered())['checks']['zone_refill_scope_preserved'])

    def test_invalid_or_unapproved_layers_rejected(self):
        for zones in [[],[dict(uuid='plane',layers=[])], [dict(uuid='plane',layers=['In1.Cu'])],
                      [dict(uuid='plane',layers=['F.Cu','F.Cu'])],
                      [dict(uuid='plane',layers=['F.Cu']),dict(uuid='plane',layers=['B.Cu'])]]:
            value=layered();value['zone_refill']['zones']=zones
            with self.assertRaises(ValueError):layer_contract(value)

    def test_absent_native_zone_layer_cannot_be_authorized(self):
        before=snapshot();before['zones'][0]['layers']=['F.Cu'];before['zones'][0]['fills'].pop('B.Cu')
        with self.assertRaises(ValueError):compare(before,copy.deepcopy(before),layered())

    def test_old_contract_still_single_layer_and_no_new_holes(self):
        value=layered();value['zone_refill']=dict(schema_version=1,zone_uuids=['plane'])
        with self.assertRaises(ValueError):contract(value)

    def test_clipping_only_selected_layer_and_native_inputs_immutable(self):
        source=model();source['task'].update(layered())
        source['task']['zone_refill']['zones'][0]['layers']=['B.Cu']
        frozen=copy.deepcopy(source)
        self.assertEqual(search_model(source),(source,None))
        source['task']['allowed_layer']='B.Cu'
        result,proof=search_model(source)
        self.assertTrue(proof['zones'][0]['outside_region_copper_preserved'])
        self.assertNotEqual(result['primitives'],source['primitives'])
        source['task']['allowed_layer']='F.Cu'
        self.assertEqual(source,frozen)


if __name__=='__main__':unittest.main()
