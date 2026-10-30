"""Resource graph invariants, independent of stored PCB solution paths."""
import copy
import unittest
from unittest.mock import patch
from test_geometry_search import model, GEOMETRY
from pcb_local.multilayer_geometry import search
from pcb_local.geometry_search import SearchLimit


def layered():
    m=model(gap=.649);m['task'].update(allowed_layer='F.Cu',permissions=dict(schema_version=1,route_layers=['F.Cu','B.Cu'],max_new_vias=2,via_type='through'))
    m['primitives']=[dict(kind='segment',net='other',clearance_mm=.2,width_mm=.3,start=[5,0],end=[5,10])]
    m.update(copper_layers=['F.Cu','B.Cu'],via_rules=dict(diameter_mm=.6,drill_mm=.3),hole_to_hole_clearance_mm=.25)
    front=copy.deepcopy(m);back=copy.deepcopy(m);back['primitives']=[]
    m['routing_models']={'F.Cu':front,'B.Cu':back}
    m['via_models']=copy.deepcopy(m['routing_models'])
    return m


@unittest.skipUnless(GEOMETRY,'Requires pinned geometry dependencies')
class LayeredChecks(unittest.TestCase):
    def test_locked_wall_needs_two_vias(self):
        m=layered();r=search(m)
        self.assertEqual(r['status'],'found');self.assertEqual(len(r['via_positions_mm']),2)
        m['task']['permissions']['max_new_vias']=1
        self.assertEqual(search(m)['status'],'no_path')

    def test_input_is_immutable(self):
        m=layered();original=copy.deepcopy(m);search(m);self.assertEqual(m,original)

    def test_missing_layer_witness_fails(self):
        m=layered();del m['via_models']['B.Cu']
        with self.assertRaises(ValueError):search(m)

    def test_inner_layer_obstacle_blocks_drill(self):
        m=layered();m['copper_layers']=['F.Cu','In1.Cu','B.Cu'];m['task']['permissions']['route_layers']=m['copper_layers']
        inner=copy.deepcopy(m['routing_models']['B.Cu'])
        inner['primitives']=[dict(kind='keepout',net='',blocks_same_net=True,clearance_mm=0,polygons=[dict(shell=[[0,0],[10,0],[10,10],[0,10]],holes=[])])]
        m['routing_models']['In1.Cu']=inner;m['via_models']['In1.Cu']=copy.deepcopy(inner)
        self.assertEqual(search(m)['status'],'no_path')

    def test_via_only_keepout_not_dropped(self):
        m=layered();m['via_models']['B.Cu']['primitives']=[dict(kind='keepout',net='',blocks_same_net=True,clearance_mm=0,polygons=[dict(shell=[[0,0],[10,0],[10,10],[0,10]],holes=[])])]
        self.assertEqual(search(m)['status'],'no_path')

    def test_no_permission_growth(self):
        m=layered();m['task']['permissions']['route_layers']=['F.Cu']
        with self.assertRaises(ValueError):search(m)
        m=layered();m['task']['modification']={'mode':'ripup'}
        with self.assertRaises(ValueError):search(m)

    def test_resource_bounds(self):
        with self.assertRaises(SearchLimit):search(layered(),timeout=-1)
        with patch('pcb_local.multilayer_geometry.MAX_NODES',2):
            with self.assertRaises(SearchLimit):search(layered())

    def test_new_via_diameter_changes_feasible_sites(self):
        m=layered();m['via_rules']['diameter_mm']=15
        self.assertEqual(search(m)['status'],'no_path')

if __name__=='__main__':unittest.main()
