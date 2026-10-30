import copy
from pcb_local.partial_segments import split_segment
from pcb_local.pns_adapter import compose
from pcb_local.validation import read_board,parse_sexpr,field,name,Quoted
from pcb_local.scope_candidate import _serialize
from test_pns_adapter import PNSAdapter
from test_scope_candidate import TASK


class PartialCompose(PNSAdapter):
    def setup_partial(self):
        original=read_board(self.before);old=original['routing']['old']
        row=dict(uuid='old',net=original['identities']['old']['net'],kind='segment',
            start=list(map(float,field(old,'start')[1:3])),end=list(map(float,field(old,'end')[1:3])),
            width_mm=float(field(old,'width')[1]),problem_layer='F.Cu')
        self.plan=split_segment(row,[0,0,4,4],.4)
        self.task=dict(TASK,region_mm=[0,0,4,4],partial_segments=[self.plan],modification=dict(schema_version=1,mode='shove',routing_uuids=['old'],nets=[row['net']],max_modified_tracks=1))
        root=parse_sexpr(self.before.read_text())[0]
        root=[n for n in root if name(n)!='segment']
        for child in self.plan['children']:
            n=copy.deepcopy(old);field(n,'uuid')[1]=Quoted(child['uuid'])
            for key in ('start','end'):field(n,key)[1:]=list(map(str,child[key]))
            if child['locked']:n.append(['locked','yes'])
            root.append(n)
        self.problem.write_text(_serialize(root));self.loaded.write_text(_serialize(root))
        return root

    def test_zero_motion_preserves_original_parent_identity(self):
        self.setup_partial()
        candidate=compose(self.before,self.problem,self.loaded,self.loaded,self.task,self.root/'no-motion',partial_plans=[self.plan])
        self.assertEqual(read_board(candidate),read_board(self.before))

    def test_middle_delta_replaces_parent_but_keeps_tail_metadata(self):
        root=self.setup_partial();mid=self.plan['middle_uuid']
        node=next(n for n in root if name(n)=='segment' and str(field(n,'uuid')[1])==mid)
        field(node,'start')[2]='1.1';field(node,'end')[2]='1.1'
        proposal=self.write('middle-moved',_serialize(root))
        candidate=compose(self.before,self.problem,self.loaded,proposal,self.task,self.root/'moved',partial_plans=[self.plan])
        result=read_board(candidate)
        self.assertNotIn('old',result['routing'])
        for child in self.plan['children']:
            node=result['routing'][child['uuid']]
            self.assertIsNone(field(node,'locked'))
            if child['locked']:
                for key in ('start','end'):self.assertEqual(list(map(float,field(node,key)[1:3])),child[key])

    def test_fixed_tail_motion_or_deletion_denied(self):
        for action in ('move','delete'):
            root=self.setup_partial();tail=self.plan['children'][0]['uuid']
            n=next(n for n in root if name(n)=='segment' and str(field(n,'uuid')[1])==tail)
            if action=='move':field(n,'start')[2]='1.1'
            else:root.remove(n)
            proposal=self.write(action,_serialize(root))
            with self.subTest(action=action),self.assertRaisesRegex(ValueError,'unauthorized'):
                compose(self.before,self.problem,self.loaded,proposal,self.task,self.root/action,partial_plans=[self.plan])

    def test_missing_plan_and_modified_parent_denied(self):
        self.setup_partial()
        with self.assertRaisesRegex(ValueError,'independently validated'):
            compose(self.before,self.problem,self.loaded,self.loaded,self.task,self.root/'missing')
        self.before.write_text(self.before.read_text().replace('(width 0.2)','(width 0.3)'))
        with self.assertRaisesRegex(ValueError,'original serialized'):
            compose(self.before,self.problem,self.loaded,self.loaded,self.task,self.root/'parent',partial_plans=[self.plan])

    def test_final_gate_independently_rejects_tail_tamper(self):
        from pcb_local.partial_protection import check
        root=self.setup_partial();before=read_board(self.before)
        proposal=self.write('split-only',_serialize(root));after=read_board(proposal)
        self.assertEqual(check(before,after,self.task),[])
        for kind in ('delete','move','width','net','parent'):
            damaged=copy.deepcopy(after);tail=self.plan['children'][0]['uuid']
            if kind=='delete':del damaged['routing'][tail]
            elif kind=='move':field(damaged['routing'][tail],'start')[1]='1.000001'
            elif kind=='width':field(damaged['routing'][tail],'width')[1]='.199999'
            elif kind=='net':damaged['identities'][tail]['net']='OTHER'
            elif kind=='parent':damaged['routing']['old']=before['routing']['old']
            with self.subTest(kind=kind):self.assertTrue(check(before,damaged,self.task))
