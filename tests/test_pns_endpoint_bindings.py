"""Engine bindings reject substitution after native geometry projection."""
import copy
from types import SimpleNamespace as NS
import unittest
from pcb_local.pns_worker import endpoint_net_codes


class PnsEndpointBindings(unittest.TestCase):
    def setUp(self):
        self.pad=NS(footprint_ref='pad',net_name='DATA',net_code=1,x_mm=1.,y_mm=2.,layer=0)
        self.track=NS(uuid='track',net_name='DATA',net_code=1,x1_mm=3.,y1_mm=2.,x2_mm=5.,y2_mm=2.,layer=0)
        self.via=NS(uuid='via',net_name='DATA',net_code=1,x_mm=8.,y_mm=2.,top_layer=0,bottom_layer=2)
        self.router=NS(get_pads=lambda:[self.pad],get_tracks=lambda:[self.track],get_vias=lambda:[self.via])
        self.pad_witness=[dict(uuid='pad',reference='pad',net='DATA',anchor=[1.,2.],shape_center=[1.,2.],layer='F.Cu')]
        self.request=dict(target_net='DATA',allowed_layer='F.Cu',endpoints=[[5.,2.],[8.,2.]],
            endpoint_anchors=[[5.,2.],[8.,2.]],endpoint_bindings=[
                dict(kind='segment',uuid='track',selector='end',position_mm=[5.,2.],net='DATA',layer='F.Cu'),
                dict(kind='via',uuid='via',selector='center',position_mm=[8.,2.],net='DATA',layer='F.Cu')])

    def test_line_and_via_use_real_objects(self):
        self.assertEqual(endpoint_net_codes(self.router,self.request,0),[1,1])

    def test_each_identity_field_is_bound(self):
        for index,key,value in [(0,'uuid','missing'),(0,'selector','start'),(0,'kind','pad'),
                                (0,'position_mm',[5.000001,2]),(0,'net','OTHER'),
                                (0,'layer','B.Cu'),(1,'selector','start'),(1,'uuid','track')]:
            with self.subTest(index=index,key=key):
                request=copy.deepcopy(self.request);request['endpoint_bindings'][index][key]=value
                with self.assertRaises(ValueError):endpoint_net_codes(self.router,request,0)

    def test_loaded_geometry_cannot_be_swapped(self):
        for obj,key,value in [(self.track,'x2_mm',5.000001),(self.track,'net_name','OTHER'),
                              (self.track,'layer',2),(self.via,'bottom_layer',4),
                              (self.via,'x_mm',8.000001),(self.via,'net_code',2)]:
            with self.subTest(key=key,value=value):
                old=getattr(obj,key);setattr(obj,key,value)
                with self.assertRaises(ValueError):endpoint_net_codes(self.router,self.request,0)
                setattr(obj,key,old)

    def test_route_coordinate_cannot_differ_from_fixed_endpoint(self):
        request=copy.deepcopy(self.request);request['endpoints'][0][0]+=.1
        with self.assertRaises(ValueError):endpoint_net_codes(self.router,request,0)

    def test_duplicate_identity_is_not_arbitrarily_selected(self):
        self.router.get_tracks=lambda:[self.track,copy.copy(self.track)]
        with self.assertRaises(ValueError):endpoint_net_codes(self.router,self.request,0)

    def test_pad_and_via_and_legacy_pads(self):
        request=copy.deepcopy(self.request)
        request['endpoint_bindings'][0].update(kind='pad',uuid='pad',selector='center',position_mm=[1.,2.])
        request['endpoints'][0]=request['endpoint_anchors'][0]=[1.,2.]
        self.assertEqual(endpoint_net_codes(self.router,request,0,self.pad_witness),[1,1])
        second=copy.copy(self.pad);second.footprint_ref='pad2';second.x_mm=8.
        self.router.get_pads=lambda:[self.pad,second]
        self.pad_witness.append(dict(uuid='pad2',reference='pad2',net='DATA',anchor=[8.,2.],shape_center=[8.,2.],layer='F.Cu'))
        request.pop('endpoint_bindings');request['endpoint_refs']=['pad','pad2']
        self.assertEqual(endpoint_net_codes(self.router,request,0,self.pad_witness),[1,1])

    def test_offset_pad_uses_independent_loaded_shape_center(self):
        request=copy.deepcopy(self.request)
        request['endpoint_bindings'][0].update(kind='pad',uuid='pad',selector='center',position_mm=[1.,2.])
        request['endpoint_anchors'][0]=[1.,2.]
        self.pad_witness[0]['shape_center']=[1.05,2.]
        request['endpoints'][0]=[1.05,2.]
        self.assertEqual(endpoint_net_codes(self.router,request,0,self.pad_witness),[1,1])
        for point in ([1.,2.],[1.050001,2.],[100.,200.],[float('nan'),2.],[float('inf'),2.]):
            with self.subTest(point=point):
                request['endpoints'][0]=point
                # A matching extra request field cannot authorize itself.
                request['pad_shape_center']=point
                with self.assertRaises(ValueError):endpoint_net_codes(self.router,request,0,self.pad_witness)

    def test_pad_requires_unique_native_witness(self):
        request=copy.deepcopy(self.request)
        request['endpoint_bindings'][0].update(kind='pad',uuid='pad',selector='center',position_mm=[1.,2.])
        request['endpoints'][0]=request['endpoint_anchors'][0]=[1.,2.]
        for witness in (None, [], self.pad_witness*2):
            with self.subTest(witness=witness):
                with self.assertRaises(ValueError):endpoint_net_codes(self.router,request,0,witness)
        for key,value in [('uuid','other'),('reference','other'),('net','OTHER'),('layer','B.Cu'),('anchor',[1.000001,2.])]:
            witness=copy.deepcopy(self.pad_witness);witness[0][key]=value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):endpoint_net_codes(self.router,request,0,witness)

    def test_same_object_cannot_supply_both_endpoints(self):
        request=copy.deepcopy(self.request)
        request['endpoint_bindings'][1]=copy.deepcopy(request['endpoint_bindings'][0])
        request['endpoint_bindings'][1].update(selector='start',position_mm=[3.,2.])
        request['endpoint_anchors'][1]=request['endpoints'][1]=[3.,2.]
        with self.assertRaises(ValueError):endpoint_net_codes(self.router,request,0)


if __name__=='__main__':unittest.main()
