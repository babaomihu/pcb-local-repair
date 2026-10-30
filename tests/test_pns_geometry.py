"""Projection adversaries; topology, identities and rules are independently checked."""
import copy
import importlib.util
import unittest
from pcb_local.pns_geometry import prepare, verify


def source():
    return dict(task={'region_mm':[1,1,9,9]},outline_edges=[[[0,0],[10,0]],[[10,0],[10,10]],[[10,10],[0,10]],[[0,10],[0,0]]],
        minimum_clearance_mm=.2, minimum_track_width_mm=.25, edge_clearance_mm=.2,hole_clearance_mm=.25,
        primitives=[dict(uuid='plane',net='arbitrary',kind='polygon',source_kind='zone_fill',clearance_mm=.3,track_width_mm=.25,
            polygons=[dict(shell=[[2,2],[8,2],[8,8],[2,8]],holes=[[[3,3],[3,7],[7,7],[7,3]]]),
                      dict(shell=[[.1,.1],[.8,.1],[.8,.8],[.1,.8]],holes=[])])])


def projected(s, projection):
    rows=[dict(uuid=t['uuid'],net=t['net'],kind='polygon',clearance_mm=t['clearance_mm'],track_width_mm=t['track_width_mm'],
        polygons=[dict(shell=t['points'],holes=[])],authored_polygons=[dict(shell=t['points'],holes=[])]) for t in projection['tiles']]
    rows += [dict(uuid=p['uuid'],kind='keepout',blocks_same_net=True,polygons=p['polygons']) for p in projection['region_barriers']]
    return dict(s,primitives=rows)


@unittest.skipUnless(importlib.util.find_spec('shapely'),'Requires pinned router geometry runtime')
class FixedFillProjection(unittest.TestCase):
    def via_projection(self, locked=False):
        s=source();s['vias']=[dict(uuid='fixed-via',net='signal',center=[1,1],
            diameter_mm=.6,drill_mm=.3,layers=['F.Cu','B.Cu'],
            copper_layers=['F.Cu','B.Cu'],standard_through=True,locked=locked)]
        p=prepare(s);r=copy.deepcopy(projected(s,p));r['vias'][0]['locked']=True
        return s,p,r

    def test_solver_only_via_lock_strengthening_preserves_source(self):
        for locked in (False,True):
            s,p,r=self.via_projection(locked);before=copy.deepcopy(s)
            verify(s,r,p)
            self.assertEqual(s,before)

    def test_solver_fixed_via_cannot_be_unlocked_or_unwitnessed(self):
        for mode in ('unlocked','missing-source-lock','missing-projected-lock'):
            s,p,r=self.via_projection()
            if mode=='unlocked':r['vias'][0]['locked']=False
            elif mode=='missing-source-lock':s['vias'][0].pop('locked')
            else:r['vias'][0].pop('locked')
            with self.subTest(mode=mode),self.assertRaises(ValueError):verify(s,r,p)

    def test_solver_lock_strengthening_does_not_hide_via_geometry_changes(self):
        for key,value in [('center',[1.001,1]),('drill_mm',.2),('net','other'),
                          ('copper_layers',['F.Cu']),('layers',['F.Cu','In1.Cu'])]:
            s,p,r=self.via_projection();r['vias'][0][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'non-zone'):verify(s,r,p)

    def test_hole_and_disjoint_island_preserved_under_translation(self):
        for dx,dy in [(0,0),(23.317,-18.973)]:
            s=source()
            for p in s['primitives']:
                for poly in p['polygons']:
                    for ring in [poly['shell'],*poly['holes']]:
                        for xy in ring:xy[0]+=dx;xy[1]+=dy
            # Native snapshots are integer nanometres, not noisy float sums.
            for p in s['primitives']:
                for poly in p['polygons']:
                    for ring in [poly['shell'],*poly['holes']]:
                        for xy in ring:xy[:]=[round(v,6) for v in xy]
            projection=prepare(s);r=verify(s,projected(s,projection),projection)
            self.assertEqual(r['zones'][0]['holes'],1)
            self.assertTrue(r['zones'][0]['exact_equal'])

    def test_missing_tile_rejected(self):
        s=source();p=prepare(s);r=projected(s,p);r['primitives'].pop(0)
        with self.assertRaisesRegex(ValueError,'lost fixed-fill'):verify(s,r,p)

    def test_net_or_clearance_change_rejected(self):
        for key,value in [('net','different'),('clearance_mm',.1),('track_width_mm',.1)]:
            s=source();p=prepare(s);r=projected(s,p);r['primitives'][0][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'rules'):verify(s,r,p)

    def test_filled_hole_and_missing_sliver_rejected(self):
        for shell in [[[2,2],[8,2],[8,8],[2,8]],[[2.000001,2],[8,2],[8,8],[2.000001,8]]]:
            s=source();p=prepare(s);r=projected(s,p)
            r['primitives'][0]['polygons']=[dict(shell=shell,holes=[])]
            r['primitives'][0]['authored_polygons']=r['primitives'][0]['polygons']
            with self.assertRaisesRegex(ValueError,'topology'):verify(s,r,p)

    def test_roi_movement_or_omission_rejected(self):
        for remove in [True,False]:
            s=source();p=prepare(s);r=projected(s,p)
            if remove:r['primitives'].pop()
            else:r['primitives'][-1]['blocks_same_net']=False
            with self.assertRaisesRegex(ValueError,'boundary'):verify(s,r,p)

    def test_custom_pad_hole_islands_and_anchor_are_witnessed(self):
        s=source();pad=s['primitives'][0];pad.pop('source_kind')
        pad.update(custom_pad_projection=True,center=[2.5,2.5])
        p=prepare(s);r=projected(s,p)
        r['primitives'] += [dict(a,kind='circle',size=[.000002,.000002]) for a in p['anchors']]
        self.assertEqual(verify(s,r,p)['zones'][0]['holes'],1)
        r['primitives'][-1]['center']=[5,5]
        with self.assertRaisesRegex(ValueError,'anchor changed'):verify(s,r,p)
        pad['center']=[5,5]
        with self.assertRaisesRegex(ValueError,'interior connection'):prepare(s)

    def test_fixed_graphic_hole_and_identity_are_witnessed(self):
        s=source();s['primitives'][0]['source_kind']='fixed_graphic'
        p=prepare(s);r=projected(s,p)
        witness=verify(s,r,p)['zones'][0]
        self.assertEqual(witness['source_kind'],'fixed_graphic_envelope')
        self.assertEqual(witness['holes'],1);self.assertEqual(witness['components'],2)
        self.assertEqual(p['source_graphic_uuids'],['plane'])
        p['source_graphic_uuids']=[]
        with self.assertRaisesRegex(ValueError,'identities'):verify(s,r,p)

    def test_overlapping_graphic_parts_keep_integer_tile_vertices(self):
        s=source();g=s['primitives'][0];g['source_kind']='fixed_graphic'
        g['polygons']=[dict(shell=[[2,2],[8,2],[8,8.000001]],holes=[]),
                       dict(shell=[[2,8],[2,2.000001],[8,8]],holes=[])]
        p=prepare(s);r=projected(s,p)
        self.assertTrue(verify(s,r,p)['zones'][0]['exact_equal'])
        self.assertTrue(all(round(v,6)==v for t in p['tiles'] for xy in t['points'] for v in xy))

    def test_fixed_graphic_copper_and_rules_cannot_change(self):
        for mode in ('hole','net','clearance'):
            s=source();s['primitives'][0]['source_kind']='fixed_graphic'
            p=prepare(s);r=projected(s,p)
            if mode=='hole':
                r['primitives'][0]['authored_polygons']=[dict(shell=[[2,2],[8,2],[8,8],[2,8]],holes=[])]
                r['primitives'][0]['polygons']=r['primitives'][0]['authored_polygons']
            elif mode=='net':r['primitives'][0]['net']='other'
            else:r['primitives'][0]['clearance_mm']=.01
            with self.subTest(mode=mode),self.assertRaises(ValueError):verify(s,r,p)

    def test_fixed_arc_envelope_cannot_disappear_or_change(self):
        s=source();arc=s['primitives'][0];arc.pop('source_kind');arc['native_arc']={'mid':[2,3]}
        p=prepare(s);r=projected(s,p)
        self.assertEqual(verify(s,r,p)['zones'][0]['source_kind'],'fixed_arc_envelope')
        p['source_arc_uuids']=[]
        with self.assertRaisesRegex(ValueError,'identities'):verify(s,r,p)

    def test_inner_layer_omission_and_stack_reordering_refused(self):
        s=source();s['copper_layers']=['F.Cu','In1.Cu','In2.Cu','B.Cu']
        inner=source();inner['copper_layers']=s['copper_layers'];inner['task']['allowed_layer']='In1.Cu'
        s['auxiliary_layers']={'In1.Cu':inner};p=prepare(s)
        r=projected(s,p);r['auxiliary_layers']={'In1.Cu':projected(inner,p['auxiliary_layers']['In1.Cu'])}
        verify(s,r,p)
        r['auxiliary_layers']={}
        with self.assertRaisesRegex(ValueError,'layer was omitted'):verify(s,r,p)
        r['auxiliary_layers']={'In1.Cu':projected(inner,p['auxiliary_layers']['In1.Cu'])}
        r['copper_layers']=list(reversed(s['copper_layers']))
        with self.assertRaisesRegex(ValueError,'non-zone'):verify(s,r,p)

    def test_fractional_nanometre_roi_is_only_narrowed(self):
        from pcb_local.pns_geometry import region_barriers
        s=source();s['task']['region_mm']=[1.0000001,1.0000001,8.9999999,8.9999999]
        b=region_barriers(s)
        self.assertGreaterEqual(b[0]['polygons'][0]['shell'][1][0],s['task']['region_mm'][0])
        self.assertLessEqual(b[1]['polygons'][0]['shell'][0][0],s['task']['region_mm'][2])
        self.assertGreaterEqual(b[2]['polygons'][0]['shell'][2][1],s['task']['region_mm'][1])
        self.assertLessEqual(b[3]['polygons'][0]['shell'][0][1],s['task']['region_mm'][3])

    def test_bounded_complexity_rejected(self):
        from unittest.mock import patch
        with patch('pcb_local.pns_geometry.MAX_TILES',1),self.assertRaisesRegex(ValueError,'tile limit'):prepare(source())

    def test_sliver_is_merged_without_deleting_its_copper(self):
        import shapely
        from shapely.geometry import Polygon
        from pcb_local.pns_geometry import anchored_tiles
        pieces=[Polygon([(0,0),(1,.000001),(2,0)]),Polygon([(0,0),(2,0),(1,-1)])]
        tiles=anchored_tiles(pieces)
        self.assertEqual(len(tiles),1)
        self.assertTrue(shapely.union_all(pieces).equals(tiles[0][0]))
        self.assertEqual(tiles[0][0].bounds[3],.000001)
