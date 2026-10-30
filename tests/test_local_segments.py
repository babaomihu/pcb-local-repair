import copy
import unittest
from pcb_local.local_segments import select
from pcb_local.modification import witness
from pcb_local.partial_routing import validated_plans
from pcb_local.partial_segments import split_segment,verify_plan
from pcb_local.validation import protection,read_board,parse_sexpr,field,name,Quoted
from pcb_local.scope_candidate import _serialize
from pcb_local.pns_adapter import compose
from test_refill_strategy import case
from test_pns_adapter import PNSAdapter
from test_scope_candidate import TASK


def selection_case(refill=False):
    s,t=case();t.pop('modification');t.pop('requested_modification_nets',None)
    if not refill:t.pop('zone_refill')
    t['region_mm']=[.1,-1,.9,1]
    s['routing_rules']['board_minima_mm']['clearance']=.02
    s['routing_rules']['netclasses_mm']['PLANE']['clearance']=.02
    s['zone_refill_choices'][0].update(local_clearance_mm=.02,min_thickness_mm=.05)
    for r in s['items'].values():r['netclass']['clearance_mm']=.02
    return s,t


class LocalSelection(unittest.TestCase):
    def test_explicit_crossing_selection_and_native_reconstruction(self):
        for refill in (False,True):
            s,t=selection_case(refill);original=copy.deepcopy(s)
            proof=select(s,t,['n1']);self.assertEqual(proof['local_middle_uuids'],['n1'])
            p=t['partial_segments'][0];self.assertEqual(p['schema_version'],2)
            rows=[dict(uuid=u,kind='segment',net=r['net'],start=r['start_mm'],end=r['end_mm'],
                       width_mm=r['width_mm'],locked=r['locked'],clearance_mm=.02)
                  for u,r in s['items'].items() if r['kind']=='segment']
            if refill:rows.append(dict(uuid='plane',source_kind='zone_fill',net='PLANE',locked=False,
                source_owner_uuid=None,clearance_mm=.02,min_thickness_mm=.05))
            model=dict(task=t,minimum_clearance_mm=.02,primitives=rows)
            self.assertEqual(validated_plans(model),[p]);self.assertEqual(s,original)
            with self.assertRaisesRegex(ValueError,'differs'):
                verify_plan(dict(rows[0],problem_layer='F.Cu'),t['region_mm'],.12,p)

    def test_unchosen_neighbour_does_not_gain_permission(self):
        s,t=selection_case();select(s,t,['n1'])
        self.assertEqual(t['modification']['routing_uuids'],['n1'])
        self.assertEqual(t['modification']['max_modified_tracks'],1)
        t['modification']['routing_uuids'].append('n2')
        with self.assertRaises(ValueError):witness(s,t)

    def test_native_authority_or_anchor_changes_refuse(self):
        for mode in ('locked','clearance','endpoint','target','layer','ripup','tail'):
            s,t=selection_case();select(s,t,['n1'])
            if mode=='locked':s['items']['n1']['locked']=True
            elif mode=='clearance':s['items']['n1']['own_clearance_mm']=.03
            elif mode=='endpoint':s['items']['p1']['position_mm']=[-.001,0]
            elif mode=='target':t['target_item_ids']=['n1']
            elif mode=='layer':s['items']['n1']['copper_layers']=['B.Cu']
            elif mode=='ripup':t['modification']['mode']='ripup'
            else:t['partial_segments'][0]['children'][0]['end'][0]+=.000001
            with self.subTest(mode=mode),self.assertRaises(ValueError):witness(s,t)

    def test_rotation_reversal_and_coprime_lattice(self):
        row=dict(uuid='cross',net='N',kind='segment',start=[-5,0],end=[5,0],width_mm=.2,layer='F.Cu')
        self.assertIsNone(split_segment(row,[-1,-1,1,1],.2))
        p=split_segment(row,[-1,-1,1,1],.2,allow_crossing=True)
        self.assertEqual([p['children'][1][k] for k in ('start','end')],[[-.8,0],[.8,0]])
        for a,b in [([0,-5],[0,5]),([5,0],[-5,0])]:
            self.assertIsNotNone(split_segment(dict(row,start=a,end=b),[-1,-1,1,1],.2,allow_crossing=True))
        self.assertIsNone(split_segment(dict(row,start=[-.000003,0],end=[0,.000002],width_mm=.000001),
            [-.000002,0,-.000001,.000002],.0000005,allow_crossing=True))


class CrossingProtection(PNSAdapter):
    def setup_crossing(self):
        before=read_board(self.before);old=before['routing']['old'];roi=[1.2,0,1.8,2]
        row=dict(uuid='old',net=before['identities']['old']['net'],kind='segment',
            start=[1,1],end=[2,1],width_mm=.2,problem_layer='F.Cu')
        plan=split_segment(row,roi,.15,allow_crossing=True)
        task=dict(TASK,region_mm=roi,partial_segments=[plan],modification=dict(schema_version=1,
            mode='shove',routing_uuids=['old'],nets=['N'],max_modified_tracks=1))
        root=parse_sexpr(self.before.read_text())[0];root=[n for n in root if name(n)!='segment']
        for child in plan['children']:
            n=copy.deepcopy(old);field(n,'uuid')[1]=Quoted(child['uuid'])
            for k in ('start','end'):field(n,k)[1:]=list(map(str,child[k]))
            root.append(n)
        candidate=self.write('crossing',_serialize(root))
        return before,plan,task,candidate

    def test_only_exact_tails_exempt_from_whole_new_segment_roi_check(self):
        before,p,t,c=self.setup_crossing()
        check=protection(before,read_board(c),self.before,c,t)
        self.assertTrue(all(check['checks'].values()),check)
        for mode in ('tail_move','tail_metadata','middle_outside','extra_outside','keep_parent','forged_plan'):
            after=copy.deepcopy(read_board(c));task=copy.deepcopy(t)
            tail=p['children'][0]['uuid'];mid=p['middle_uuid']
            if mode=='tail_move':field(after['routing'][tail],'start')[1]='.999999'
            elif mode=='tail_metadata':after['routing'][tail].append(['locked','yes'])
            elif mode=='middle_outside':field(after['routing'][mid],'start')[1]='1.199'
            elif mode=='extra_outside':
                after['routing']['extra']=copy.deepcopy(after['routing'][tail])
                after['identities']['extra']=copy.deepcopy(after['identities'][tail])
            elif mode=='keep_parent':
                after['routing']['old']=before['routing']['old'];after['identities']['old']=before['identities']['old']
            else:task['partial_segments'][0]['children'][0]['end'][0]+=.000001
            with self.subTest(mode=mode):
                self.assertFalse(all(protection(before,after,self.before,c,task)['checks'].values()))

    def test_actual_composition_keeps_crossing_tails_and_source(self):
        before,p,t,c=self.setup_crossing();root=parse_sexpr(c.read_text())[0]
        self.problem.write_text(_serialize(root));self.loaded.write_text(_serialize(root))
        n=next(n for n in root if name(n)=='segment' and str(field(n,'uuid')[1])==p['middle_uuid'])
        field(n,'start')[2]='1.1';field(n,'end')[2]='1.1'
        proposed=self.write('crossing-moved',_serialize(root))
        candidate=compose(self.before,self.problem,self.loaded,proposed,t,self.root/'composed',partial_plans=[p])
        result=protection(before,read_board(candidate),self.before,candidate,t)
        self.assertTrue(all(result['checks'].values()),result)
        self.assertEqual(before,read_board(self.before))
        # Connectivity is deliberately a different gate: this synthetic shifted
        # middle is disconnected and is NOT asserted to be an accepted repair.
