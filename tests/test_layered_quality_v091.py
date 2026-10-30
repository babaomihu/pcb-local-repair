"""Layered quality preserves immutable anchors, contacts, scope and inputs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from pcb_local.trace_quality import propose,eligibility
from pcb_local.layered_quality import partition,reposition_new_vias
from pcb_local.multilayer_geometry import candidate
from pcb_local.validation import parse_sexpr,name,field,read_board,canonical
from pcb_local.scope_candidate import _serialize
from pcb_local.workspace import source_files
from geometry_fixture import create
from test_trace_quality import model,GEOMETRY


@unittest.skipUnless(GEOMETRY,'Pinned geometry runtime required')
class LayeredQualityTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.before,self.task=create(self.root/'input',gates=())
        self.task['permissions']=dict(schema_version=1,route_layers=['F.Cu','B.Cu'],max_new_vias=2,via_type='through')
        self.model=model(self.task);self.model['copper_layers']=['F.Cu','B.Cu']
        self.model['routing_models']={l:copy.deepcopy(self.model) for l in ['F.Cu','B.Cu']}
        for l,m in self.model['routing_models'].items():m['task']=dict(self.task,allowed_layer=l)
        raw=[('F.Cu',[6,15]),('F.Cu',[8,15]),('B.Cu',[8,15]),('B.Cu',[10,17]),('B.Cu',[20,17]),('B.Cu',[22,15]),('F.Cu',[22,15]),('F.Cu',[24,15])]
        self.after=candidate(self.before,self.task,dict(path=[dict(layer=l,point_mm=p) for l,p in raw],via_positions_mm=[[8,15],[22,15]],width_mm=.25,via_diameter_mm=.6,via_drill_mm=.3),self.root/'raw')
    def mutate(self,fn):
        tree=parse_sexpr(self.after.read_text())[0];fn(tree);self.after.write_text(_serialize(tree)+'\n')
    def proposal(self):return propose(self.before,self.after,self.task,self.model,self.root/'out',timeout=3)
    def test_layer_chords_keep_exact_vias_and_originals(self):
        hashes=source_files(self.before),source_files(self.after);r=self.proposal()
        self.assertEqual(r['status'],'proposed');self.assertEqual(r['before_segments'],5);self.assertEqual(r['after_segments'],3)
        self.assertEqual(r['after_length_mm'],18)
        old,new=read_board(self.after),read_board(Path(r['candidate_board']))
        for uid,n in old['routing'].items():
            if name(n)=='via':self.assertEqual(canonical(n),canonical(new['routing'][uid]))
        self.assertEqual(hashes,(source_files(self.before),source_files(self.after)))
    def test_only_authorized_layers_eligible(self):
        self.task['permissions']['route_layers']=['F.Cu']
        self.assertIsNotNone(eligibility(self.before,self.after,self.task))
    def test_via_budget_still_checked(self):
        self.task['permissions']['max_new_vias']=1
        self.assertIn('via count',eligibility(self.before,self.after,self.task))
    def test_missing_layer_model_refused(self):
        del self.model['routing_models']['B.Cu']
        with self.assertRaisesRegex(ValueError,'per-layer'):self.proposal()
    def test_original_object_change_refused(self):
        self.mutate(lambda tree:tree.append(['property','unexpected','change']))
        with self.assertRaisesRegex(ValueError,'Original definitions'):self.proposal()
    def test_added_other_net_refused(self):
        self.mutate(lambda tree:field(next(x for x in tree if name(x)=='segment'),'net').__setitem__(1,'0'))
        with self.assertRaisesRegex(ValueError,'target-net'):self.proposal()
    def test_side_contact_freezes_only_affected_chain(self):
        self.model['routing_models']['B.Cu']['primitives']=[dict(uuid='contact',kind='circle',center=[10,17],size=[1,1],net=self.task['target_net'],clearance_mm=.2)]
        r=self.proposal();self.assertEqual(r['status'],'unchanged');self.assertEqual(r['before_segments'],r['after_segments'])
        self.assertTrue(any('same-net contact' in x.get('kept_reason','') for x in r['chain_results']))
    def test_obstacle_cannot_be_chorded_through(self):
        self.model['routing_models']['B.Cu']['primitives']=[dict(uuid='obstacle',kind='circle',center=[15,15],size=[1,1],net='other',clearance_mm=.2)]
        r=self.proposal();self.assertGreater(r['after_segments'],3)
    def test_region_not_expanded(self):
        self.task['region_mm']=[4,14,26,16]
        for m in self.model['routing_models'].values():m['task']['region_mm']=self.task['region_mm']
        r=self.proposal();self.assertEqual(r['status'],'proposed')
        # The direct chord may replace an outside raw path, but it must itself
        # fit the existing region. Production calls require prior acceptance.
        new=read_board(Path(r['candidate_board']))
        for n in new['routing'].values():
            if name(n)=='segment':
                for key in ('start','end'):
                    x,y=map(float,field(n,key)[1:3]);self.assertTrue(4<=x<=26 and 14<=y<=16)
    def test_new_via_at_intermediate_vertex_remains_anchor(self):
        self.mutate(lambda tree:tree.extend(parse_sexpr('(via (at 10 17) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (net 1) (uuid "76f06ad2-9089-43f3-9a2b-aed50dbfb052"))')))
        self.task['permissions']['max_new_vias']=3
        chains,_,_,_=partition(self.before,self.after,self.task,self.model)
        self.assertTrue(all([10,17] not in c['points'][1:-1] for c in chains))
    def test_incidental_new_via_contact_is_preserved(self):
        self.mutate(lambda tree:tree.extend(parse_sexpr('(via (at 15 17.1) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (net 1) (uuid "76f06ad2-9089-43f3-9a2b-aed50dbfb052"))')))
        self.task['permissions']['max_new_vias']=3
        r=self.proposal();self.assertEqual(r['status'],'unchanged')
    def test_budget_exhaustion_creates_no_candidate(self):
        with self.assertRaises(TimeoutError):propose(self.before,self.after,self.task,self.model,self.root/'out',timeout=-1)
        self.assertFalse((self.root/'out').exists())
    def test_closed_island_not_discarded(self):
        extra=''.join(f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width .25) (layer "B.Cu") (net 1) (uuid "00000000-0000-4000-8000-{i:012d}"))' for i,(a,b) in enumerate([((10,10),(11,10)),((11,10),(11,11)),((11,11),(10,10))]))
        self.mutate(lambda tree:tree.extend(parse_sexpr(extra)))
        with self.assertRaisesRegex(ValueError,'Closed'):self.proposal()

    def via_problem(self):
        m=copy.deepcopy(self.model);m['hole_to_hole_clearance_mm']=.25
        m['via_models']=copy.deepcopy(m['routing_models'])
        via=parse_sexpr('(via (at 10 18) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v"))')[0]
        plans=[dict(layer='F.Cu',result=[[6.,15.],[10.,18.]]),dict(layer='B.Cu',result=[[10.,18.],[24.,15.]])]
        return plans,{'v':via},m
    def test_new_via_moves_only_to_fully_witnessed_free_position(self):
        plans,vias,m=self.via_problem();frozen=copy.deepcopy((vias,m))
        moves,r=reposition_new_vias(plans,vias,m,.25)
        self.assertEqual(moves,{'v':[10.,15.]});self.assertEqual(r['status'],'proposed')
        self.assertEqual((vias,m),frozen);self.assertEqual(plans[0]['result'][-1],plans[1]['result'][0])
    def test_locked_new_via_is_not_moved(self):
        plans,vias,m=self.via_problem();vias['v'].append(['locked','yes'])
        self.assertEqual(reposition_new_vias(plans,vias,m,.25)[0],{})
    def test_missing_physical_layer_via_witness_prevents_move(self):
        plans,vias,m=self.via_problem();del m['via_models']['B.Cu']
        self.assertEqual(reposition_new_vias(plans,vias,m,.25)[0],{})
    def test_inner_layer_block_prevents_new_via_move(self):
        plans,vias,m=self.via_problem();m['copper_layers'].insert(1,'In1.Cu')
        inner=copy.deepcopy(m['via_models']['B.Cu']);inner['primitives']=[dict(kind='keepout',net='',blocks_same_net=True,clearance_mm=0,polygons=[dict(shell=[[0,0],[30,0],[30,30],[0,30]],holes=[])])]
        m['via_models']['In1.Cu']=inner
        self.assertEqual(reposition_new_vias(plans,vias,m,.25)[0],{})
    def test_original_same_net_contact_freezes_new_via(self):
        plans,vias,m=self.via_problem()
        m['routing_models']['B.Cu']['primitives']=[dict(uuid='fixed',kind='circle',net=self.task['target_net'],center=[10,18],size=[.6,.6],clearance_mm=.2)]
        self.assertEqual(reposition_new_vias(plans,vias,m,.25)[0],{})
    def test_other_new_hole_clearance_is_enforced(self):
        plans,vias,m=self.via_problem()
        vias['other']=parse_sexpr('(via (at 10 15) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (net 1) (uuid "other"))')[0]
        moves,_=reposition_new_vias(plans,vias,m,.25)
        import math
        if 'v' in moves:self.assertGreaterEqual(math.dist(moves['v'],[10,15]),.55+.00002)

if __name__=='__main__':unittest.main()
