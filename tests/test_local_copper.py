import copy
import math
import unittest
from pcb_local.pns_geometry import prepare,verify,MAX_FILL_VERTICES
from test_pns_geometry import source,projected


def large_model(net='OBSTACLE'):
    s=source();s['task'].update(target_net='TARGET',allowed_layer='F.Cu')
    p=s['primitives'][0];p.update(net=net,source_kind='fixed_graphic')
    p['polygons']=[dict(shell=[[round(9+2*math.cos(2*math.pi*i/14000),6),round(5+2*math.sin(2*math.pi*i/14000),6)] for i in range(14000)],holes=[])]
    return s

class LocalCopper(unittest.TestCase):
    def test_large_crossing_cover_has_explicit_conservative_proof(self):
        s=large_model();before=copy.deepcopy(s);p=prepare(s);r=verify(s,projected(s,p),p)
        self.assertEqual(s,before);self.assertLess(p['projected_source_vertices'],MAX_FILL_VERTICES)
        self.assertTrue(p['local_copper']['objects'])
        self.assertFalse(r['zones'][0]['exact_equal']);self.assertTrue(r['zones'][0]['required_copper_covered'])
        self.assertEqual(r['zones'][0]['proof'],'conservative_guard_coverage')

    def test_target_authorized_context_never_reduced(self):
        for variant in ('target','net','identity'):
            s=large_model()
            if variant=='target':s['primitives'][0]['net']='TARGET'
            elif variant=='net':s['task']['modification']={'nets':['OBSTACLE']}
            else:s['task']['modification']={'routing_uuids':['plane']}
            with self.subTest(variant=variant),self.assertRaisesRegex(ValueError,'vertex limit'):prepare(s)

    def test_coverage_witness_and_reread_attacks_refused(self):
        for attack in ('guard','hash','proof','source','shape','rule','tile'):
            s=large_model();p=prepare(s);r=projected(s,p)
            if attack=='guard':p['local_copper']['objects'][0]['guarded_region_mm'][0]+=1
            elif attack=='hash':p['local_copper']['objects'][0]['source_sha256']='changed'
            elif attack=='proof':p['local_copper']['objects']=[]
            elif attack=='source':s['primitives'][0]['polygons'][0]['shell'][0][0]+=.1
            else:
                uid=p['tiles'][0]['uuid'];item=next(x for x in r['primitives'] if x['uuid']==uid)
                if attack=='rule':item['clearance_mm']=0
                elif attack=='shape':item['authored_polygons'][0]['shell'][0][0]+=.01
                else:r['primitives'].remove(item)
            with self.subTest(attack=attack),self.assertRaises(ValueError):verify(s,r,p)

    def test_small_objects_preserve_exact_path(self):
        s=source();s['task'].update(target_net='TARGET',allowed_layer='F.Cu')
        p=prepare(s);r=verify(s,projected(s,p),p)
        self.assertEqual(p['local_copper']['objects'],[])
        self.assertTrue(all(z['exact_equal'] for z in r['zones']))

    def test_tile_pressure_below_vertex_limit(self):
        s=large_model();s['primitives'][0]['polygons'][0]['shell']=s['primitives'][0]['polygons'][0]['shell'][::2]
        p=prepare(s)
        self.assertLess(p['complexity_plan']['source_vertices'],12000)
        self.assertGreater(p['complexity_plan']['triangle_upper_bound'],4096)
        self.assertTrue(p['local_copper']['objects'])
        verify(s,projected(s,p),p)
        p['complexity_plan']['force_local_cover']=False
        with self.assertRaisesRegex(ValueError,'complexity plan'):verify(s,projected(s,p),p)

    def test_stack_pressure_triggers_both_small_layers(self):
        s=large_model();s['primitives'][0]['polygons'][0]['shell']=s['primitives'][0]['polygons'][0]['shell'][::4]
        other=copy.deepcopy(s);other['task']['allowed_layer']='B.Cu'
        s['auxiliary_layers']={'B.Cu':other}
        p=prepare(s)
        self.assertTrue(p['local_copper']['objects'])
        self.assertTrue(p['auxiliary_layers']['B.Cu']['local_copper']['objects'])
        r=projected(s,p);r['auxiliary_layers']={'B.Cu':projected(other,p['auxiliary_layers']['B.Cu'])}
        verify(s,r,p)

    def test_holes_contribute_to_tile_budget(self):
        from pcb_local.local_copper import projection_cost
        from pcb_local.local_relevance import fixed_copper_relevance
        s=source();s['task'].update(target_net='TARGET',allowed_layer='F.Cu')
        cost=projection_cost(s,fixed_copper_relevance(s))
        # Eight vertices with one hole triangulate to eight, not six.
        self.assertGreaterEqual(cost['triangle_upper_bound'],8)

    def test_tile_pressure_cannot_clip_target_context(self):
        s=large_model('TARGET');s['primitives'][0]['polygons'][0]['shell']=s['primitives'][0]['polygons'][0]['shell'][::2]
        with self.assertRaisesRegex(ValueError,'tile limit'):prepare(s)

if __name__=='__main__':unittest.main()
