import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from pcb_local.engine import local_pair_request
from pcb_local.krt_local import segment_path
from pcb_local.jobs import Halt
from test_engine_rules import native


class LocalKRT(unittest.TestCase):
    def test_pair_preserves_all_terminal_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            board=Path(tmp)/'board.kicad_pcb';board.write_text('unchanged')
            n=native();n['items']={str(i):dict(uuid=str(i),kind='pad',net='SIGNAL',copper_layers=['F.Cu'] if i<2 else ['B.Cu']) for i in range(3)}
            task=dict(target_net='SIGNAL',target_pad_uuids=['0','1'],allowed_layer='F.Cu')
            old=copy.deepcopy(n);r=local_pair_request(board,task,n)
            self.assertEqual(r['source_net_pad_count'],3);self.assertEqual([p['uuid'] for p in r['endpoints']],['0','1']);self.assertEqual(n,old)
            task['target_pad_uuids']=['0','2']
            with self.assertRaises(Halt):local_pair_request(board,task,n)
            task['target_pad_uuids']=['0','0'];self.assertIsNone(local_pair_request(board,task,n))

    def test_multi_operation_task_cannot_enter_pair_only_adapter(self):
        n=native();n['items']={}
        task=dict(target_net='SIGNAL',allowed_layer='F.Cu',modification={'mode':'ripup'})
        self.assertIsNone(local_pair_request(Path('unused'),task,n))
        task.pop('modification');task['permissions']=dict(schema_version=1,route_layers=['F.Cu','B.Cu'],max_new_vias=1,via_type='through')
        self.assertIsNone(local_pair_request(Path('unused'),task,n))

    def test_multilayer_pad_pair_uses_bounded_local_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            board=Path(tmp)/'board.kicad_pcb';board.write_text('unchanged')
            n=native();n['items']={str(i):dict(uuid=str(i),kind='pad',net='SIGNAL',copper_layers=['F.Cu']) for i in range(2)}
            task=dict(target_net='SIGNAL',target_pad_uuids=['0','1'],allowed_layer='F.Cu',
                      permissions=dict(schema_version=1,route_layers=['F.Cu','B.Cu'],max_new_vias=2,via_type='through'))
            result=local_pair_request(board,task,n)
            self.assertEqual(result['source_net_pad_count'],2)
            self.assertEqual([p['uuid'] for p in result['endpoints']],['0','1'])
            self.assertEqual(result['task']['permissions'],task['permissions'])
            task.pop('permissions')
            self.assertIsNone(local_pair_request(board,task,n))

    def test_segment_bridge_or_rule_change_refused(self):
        def s(a,b,**kw):return SimpleNamespace(start_x=a,start_y=0.,end_x=b,end_y=0.,width=.3,layer='F.Cu',net_id=1,**kw)
        self.assertEqual(segment_path([s(0.,1.),s(1.,2.)],1,'F.Cu',.3),[[0.,0.],[1.,0.],[2.,0.]])
        for mode in ['gap','net','width','layer','nan']:
            rows=[s(0.,1.),s(1.,2.)]
            if mode=='gap':rows[1].start_x=1.01
            elif mode=='net':rows[1].net_id=2
            elif mode=='width':rows[1].width=.2
            elif mode=='layer':rows[1].layer='B.Cu'
            else:rows[1].end_y=float('nan')
            with self.subTest(mode=mode),self.assertRaises(ValueError):segment_path(rows,1,'F.Cu',.3)

if __name__=='__main__':unittest.main()
