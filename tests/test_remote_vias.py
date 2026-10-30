import copy
import unittest
from pcb_local.remote_vias import analyze, filtered
from pcb_local.pns_geometry import prepare, verify
from test_pns_geometry import source, projected


def model(x=20., net='foreign'):
    m=source(); m['copper_layers']=['F.Cu','B.Cu']
    m['task'].update(allowed_layer='F.Cu',target_net='target')
    m['hole_to_hole_clearance_mm']=.25
    m['endpoints']=[dict(uuid='endpoint',net='target',clearance_mm=.2)]
    via=dict(uuid='remote',net=net,center=[x,5.],drill_mm=.3,diameter_mm=.6,
             clearance_mm=.2,track_width_mm=.25,layers=['F.Cu','B.Cu'],
             copper_layers=['F.Cu','B.Cu'],standard_through=False,locked=False)
    m['vias']=[via]
    m['primitives'] += [dict(uuid='remote',kind='hole',net=net,center=[x,5.],
                            diameter_mm=.3,hole_clearance_mm=.25,clearance_mm=.2),
        dict(uuid='remote',kind='polygon',net=net,clearance_mm=.2,native_error_mm=.0001,
             polygons=[dict(shell=[[x-.3,4.7],[x+.3,4.7],[x+.3,5.3],[x-.3,5.3]],holes=[])])]
    aux=copy.deepcopy(m);aux['task']['allowed_layer']='B.Cu'
    m['auxiliary_layers']={'B.Cu':aux}
    return m


def layers(m):return [m,*m['auxiliary_layers'].values()]


def reread(m,p):
    f=filtered(m,p.get('remote_vias'));r=projected(f,p)
    r['primitives'] += [copy.deepcopy(x) for x in f['primitives'] if not x.get('source_kind')]
    r['vias']=[dict(v,locked=True) for v in f['vias']]
    r['auxiliary_layers']={}
    for l,a in f['auxiliary_layers'].items():
        n=projected(a,p['auxiliary_layers'][l])
        n['primitives'] += [copy.deepcopy(x) for x in a['primitives'] if not x.get('source_kind')]
        n['vias']=[dict(v,locked=True) for v in a['vias']]
        r['auxiliary_layers'][l]=n
    return r


class RemoteViaProof(unittest.TestCase):
    def test_foreign_remote_proof_and_native_verification_leave_source_unchanged(self):
        m=model();before=copy.deepcopy(m);p=prepare(m)
        self.assertEqual(p['remote_vias']['omitted_uuids'],['remote'])
        result=verify(m,reread(m,p),p)
        self.assertEqual(result['remote_vias'],p['remote_vias'])
        self.assertEqual(m,before)

    def test_near_copper_hole_and_native_error_are_never_omitted(self):
        for mode in ('copper','hole','error','touching','large_shape'):
            m=model(9.50005)
            for a in layers(m):
                if mode=='hole':a['primitives'][-2]['hole_clearance_mm']=2
                if mode=='error':a['primitives'][-1]['native_error_mm']=2
                if mode=='copper':a['primitives'][-1]['clearance_mm']=2
                if mode=='touching':a['primitives'][-1]['clearance_mm']=.20005
                if mode=='large_shape':a['primitives'][-1]['polygons'][0]['shell'][0][0]=8
            with self.subTest(mode=mode):self.assertEqual(analyze(m)['omitted_uuids'],[])

    def test_target_authorized_and_endpoint_vias_retained(self):
        for mode in ('target','modified','endpoint'):
            m=model(net='target' if mode=='target' else 'foreign')
            for a in layers(m):
                if mode=='modified':a['task']['modification']={'nets':['foreign']}
                if mode=='endpoint':a['endpoints'][0]['uuid']='remote'
            with self.subTest(mode=mode):self.assertEqual(analyze(m)['omitted_uuids'],[])

    def test_all_layers_and_unique_geometry_required(self):
        for mode in ('missing_layer','missing_via','missing_copper','extra_primitive','different_span','different_net','duplicate_via'):
            m=model();a=m['auxiliary_layers']['B.Cu']
            if mode=='missing_layer':m['auxiliary_layers']={}
            elif mode=='missing_via':a['vias']=[]
            elif mode=='missing_copper':a['primitives'].pop()
            elif mode=='extra_primitive':a['primitives'].append(copy.deepcopy(a['primitives'][-1]))
            elif mode=='different_span':a['vias'][0]['copper_layers']=['B.Cu']
            elif mode=='different_net':a['vias'][0]['net']='changed'
            else:a['vias'].append(copy.deepcopy(a['vias'][0]))
            with self.subTest(mode=mode),self.assertRaises((ValueError,KeyError)):analyze(m)

    def test_invalid_numeric_proof_rejected(self):
        for value in (float('nan'),float('inf'),-.1,True,'1'):
            for key in ('clearance_mm','native_error_mm'):
                m=model();m['primitives'][-1][key]=value
                with self.subTest(value=value,key=key),self.assertRaises(ValueError):analyze(m)

    def test_changed_source_or_forged_omission_rejected(self):
        m=model();p=prepare(m);r=reread(m,p)
        for mode in ('source','list','distance','missing_proof'):
            s,q=copy.deepcopy(m),copy.deepcopy(p)
            if mode=='source':s['task']['region_mm'][2]=25
            elif mode=='list':q['remote_vias']['omitted_uuids']=[]
            elif mode=='distance':q['remote_vias']['objects'][0]['layers'][0]['copper_distance_mm']=100
            else:q.pop('remote_vias')
            with self.subTest(mode=mode),self.assertRaises(ValueError):verify(s,r,q)
        s=copy.deepcopy(m);s['task']['target_net']='changed'
        with self.assertRaisesRegex(ValueError,'bound'):filtered(s,p['remote_vias'])

    def test_reintroduced_or_other_missing_native_geometry_rejected(self):
        m=model();p=prepare(m)
        for mode in ('via','hole','tile'):
            r=reread(m,p)
            if mode=='via':r['vias']=[dict(m['vias'][0],locked=True)]
            elif mode=='hole':r['primitives'].append(copy.deepcopy(m['primitives'][-2]))
            else:r['primitives'].pop(0)
            with self.subTest(mode=mode),self.assertRaises(ValueError):verify(m,r,p)

    def test_active_high_clearance_and_reversed_region_fail_closed(self):
        m=model(11)
        for a in layers(m):a['endpoints'][0]['clearance_mm']=3
        self.assertEqual(analyze(m)['omitted_uuids'],[])
        m['task']['region_mm']=[9,1,1,9]
        with self.assertRaises(ValueError):analyze(m)

    def test_no_unsupported_vias_preserves_existing_behavior(self):
        m=source();self.assertIsNone(analyze(m));self.assertIs(filtered(m,None),m)
