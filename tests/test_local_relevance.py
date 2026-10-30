import copy
import importlib.util
import unittest
from unittest.mock import patch
from pcb_local.local_relevance import fixed_copper_relevance
from pcb_local.pns_geometry import prepare,verify
from test_pns_geometry import source,projected


def model():
    s=source();s['task'].update(target_net='TARGET',allowed_layer='F.Cu')
    s['primitives'][0]['source_kind']='fixed_graphic'
    s['primitives'][0]['polygons']=[dict(shell=[[20,20],[25,20],[25,25],[20,25]],holes=[])]
    return s


@unittest.skipUnless(importlib.util.find_spec('shapely'),'Requires pinned router geometry runtime')
class LocalRelevance(unittest.TestCase):
    def test_far_fixed_copper_excluded_only_from_solver_with_witness(self):
        for kind in ('fixed_graphic','zone_fill'):
            s=model();s['primitives'][0]['source_kind']=kind;before=copy.deepcopy(s)
            p=prepare(s);r=verify(s,projected(s,p),p)
            self.assertEqual(p['tiles'],[]);self.assertEqual(s,before)
            self.assertEqual(p['source_vertices'],4);self.assertEqual(p['projected_source_vertices'],0)
            self.assertEqual(r['zones'],[])
            self.assertEqual(r['omitted_fixed_copper'][0]['uuid'],'plane')
            self.assertTrue(r['omitted_fixed_copper'][0]['omitted'])

    def test_crossing_object_is_kept_even_with_center_outside_roi(self):
        s=model();s['primitives'][0]['polygons'][0]['shell']=[[8,4],[40,4],[40,5],[8,5]]
        self.assertEqual(fixed_copper_relevance(s)['omitted_uuids'],[])
        self.assertTrue(prepare(s)['tiles'])

    def test_full_outline_holes_and_remote_islands_are_never_clipped(self):
        s=model();s['primitives'][0]['polygons']+=source()['primitives'][0]['polygons']
        p=prepare(s);r=verify(s,projected(s,p),p)
        self.assertEqual(p['local_relevance']['omitted_uuids'],[])
        self.assertEqual(r['zones'][0]['holes'],1);self.assertEqual(r['zones'][0]['components'],3)
        self.assertTrue(any(t['center'][0]>20 for t in p['tiles']))

    def test_target_and_authorized_nets_keep_global_context(self):
        for mode in ('target','authorized','identity','unknown'):
            s=model()
            if mode=='target':s['primitives'][0]['net']='TARGET'
            elif mode=='authorized':s['task']['modification']={'nets':['arbitrary']}
            elif mode=='identity':s['task']['modification']={'routing_uuids':['plane']}
            else:s['task'].pop('target_net')
            self.assertEqual(fixed_copper_relevance(s)['omitted_uuids'],[])

    def test_touch_and_large_rule_keep_entire_object(self):
        s=model();guard=fixed_copper_relevance(s)['guarded_region_mm'][2]
        s['primitives'][0]['polygons'][0]['shell']=[[guard,4],[20,4],[20,5],[guard,5]]
        self.assertEqual(fixed_copper_relevance(s)['omitted_uuids'],[])
        s=model();s['primitives'][0]['clearance_mm']=12
        self.assertEqual(fixed_copper_relevance(s)['omitted_uuids'],[])

    def test_no_unknown_rule_or_invalid_shape_is_hidden(self):
        for mode in ('rule','shape'):
            s=model()
            if mode=='rule':s['primitives'][0]['clearance_mm']=float('nan')
            else:s['primitives'][0]['polygons'][0]['shell']=[[20,20],[25,25],[25,20],[20,25]]
            with self.assertRaises(ValueError):fixed_copper_relevance(s)

    def test_no_complexity_increase_crossing_still_rejected(self):
        with patch('pcb_local.pns_geometry.MAX_FILL_VERTICES',3):
            prepare(model())
            s=model();s['primitives'][0]['polygons'][0]['shell']=[[8,4],[40,4],[40,5],[8,5]]
            with self.assertRaisesRegex(ValueError,'vertex limit'):prepare(s)

    def test_relevance_proof_tampering_and_changed_source_refused(self):
        for attack in ('list','bounds','guard','source','unexpected_copper'):
            s=model();p=prepare(s);r=projected(s,p)
            if attack=='list':p['local_relevance']['omitted_uuids']=[]
            elif attack=='bounds':p['local_relevance']['objects'][0]['bounds_mm']=[1,1,2,2]
            elif attack=='guard':p['local_relevance']['guard_mm']=0
            elif attack=='source':s['primitives'][0]['polygons'][0]['shell'][0][0]=1
            else:r['primitives'].append(copy.deepcopy(s['primitives'][0]))
            with self.subTest(attack=attack),self.assertRaises(ValueError):verify(s,r,p)

    def test_no_omission_for_native_arc_or_custom_pad(self):
        s=model();p=s['primitives'][0];p.pop('source_kind');p['native_arc']={'mid':[20,20]}
        self.assertEqual(fixed_copper_relevance(s)['objects'],[])
        self.assertTrue(prepare(s)['tiles'])

    def test_far_source_movement_requires_new_proof_even_if_still_far(self):
        s=model();p=prepare(s);r=projected(s,p)
        s['primitives'][0]['polygons'][0]['shell'][0][0]+=1
        with self.assertRaisesRegex(ValueError,'proof changed'):verify(s,r,p)

if __name__=='__main__':unittest.main()
