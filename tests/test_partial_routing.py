import copy
import unittest
from pcb_local.refill_strategy import select
from pcb_local.partial_routing import validated_plans, expand
from test_refill_strategy import case


def model_case():
    snapshot,task=case();task,report=select(snapshot,task,partial=True)
    primitives=[dict(uuid=u,kind='segment',net=r['net'],start=r['start_mm'],end=r['end_mm'],
        width_mm=r['width_mm'],locked=r['locked'],clearance_mm=.2)
        for u,r in snapshot['items'].items() if r['kind']=='segment']
    primitives.append(dict(uuid='plane',kind='polygon',source_kind='zone_fill',net='PLANE',
        locked=False,source_owner_uuid=None,clearance_mm=.2,min_thickness_mm=.1))
    return dict(task=task,primitives=primitives,minimum_clearance_mm=.2),report


class PartialRouting(unittest.TestCase):
    def test_native_independent_reconstruction_and_exact_expansion(self):
        model,report=model_case();plans=validated_plans(model)
        self.assertEqual(len(plans),1);self.assertEqual(report['partial_uuids'],['n1'])
        self.assertEqual(report['frozen_uuids'],[])
        rows=expand(model)['primitives'];children=[p for p in rows if p['uuid'] in {c['uuid'] for c in plans[0]['children']}]
        self.assertEqual([p['locked'] for p in children],[True,False,True])
        self.assertEqual(children[0]['start'],[0,0]);self.assertEqual(children[-1]['end'],[1,0])
        self.assertEqual(children[0]['end'],children[1]['start']);self.assertEqual(children[1]['end'],children[2]['start'])

    def test_independent_native_input_changes_are_not_self_certifying(self):
        for key,value in [('min_thickness_mm',.11),('clearance_mm',.3),('locked',True),('source_owner_uuid','footprint')]:
            model,_=model_case();model['primitives'][-1][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):validated_plans(model)
        for key,value in [('net','OTHER'),('locked',True),('start',[.000001,0])]:
            model,_=model_case();model['primitives'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):validated_plans(model)

    def test_forgery_collision_and_permission_denied(self):
        for kind in ('margin','tail','uuid','permission','duplicate','collision'):
            model,_=model_case();plan=model['task']['partial_segments'][0]
            if kind=='margin':plan['margin_nm']='1'
            if kind=='tail':plan['children'][0]['locked']=False
            if kind=='uuid':plan['children'][0]['uuid']='forged'
            if kind=='permission':model['task']['modification']['routing_uuids']=['n2']
            if kind=='duplicate':model['task']['partial_segments'].append(copy.deepcopy(plan))
            if kind=='collision':model['primitives'].append(dict(model['primitives'][1],uuid=plan['middle_uuid']))
            with self.subTest(kind=kind),self.assertRaises(ValueError):validated_plans(model)

    def test_non_shove_children_do_not_receive_partial_permission(self):
        from pcb_local.auto_strategy import plans
        model,_=model_case()
        for _,strategy,task in plans(model['task'],True):
            self.assertEqual('partial_segments' in task,strategy=='pns')

if __name__=='__main__':unittest.main()
