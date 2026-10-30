import copy
import unittest
from pcb_local.modification import witness, select
from pcb_local.validation import connectivity_delta


def snapshot(replaced=False, broken=False):
    def pad(uid, net, x, y):
        return dict(kind="pad", uuid=uid, net=net, position_mm=[x,y], copper_layers=["F.Cu"], locked=False)
    def track(uid, net, a, b):
        return dict(kind="segment", uuid=uid, net=net, start_mm=a, end_mm=b, position_mm=a, width_mm=.2, copper_layers=["F.Cu"], locked=False)
    items={"p1":pad("p1","N",0,0), "p2":pad("p2","N",2,0), "t1":pad("t1","T",0,2), "t2":pad("t2","T",2,2)}
    if replaced:
        items["newt"]=track("newt","T",[0,2],[2,2])
        comps=[["p1"],["p2"],["t1","t2","newt"]] if broken else [["p1","p2"],["t1","t2","newt"]]
        if not broken:
            items["newn"]=track("newn","N",[0,0],[2,0]);comps[0].append("newn")
    else:
        items.update(n1=track("n1","N",[0,0],[1,0]),n2=track("n2","N",[1,0],[2,0]))
        comps=[["p1","p2","n1","n2"],["t1"],["t2"]]
    return dict(items=items,components=[dict(items=c,net=items[c[0]]["net"]) for c in comps],component_of={uid:i for i,c in enumerate(comps) for uid in c})


class Modification(unittest.TestCase):
    def task(self):
        t=dict(target_net="T",target_pad_uuids=["t1","t2"],allowed_layer="F.Cu",region_mm=[-1,-1,3,3])
        select(snapshot(),t,["N"],2)
        return t

    def test_replacement_preserves_terminal_relation(self):
        r=connectivity_delta(snapshot(),snapshot(True),self.task())
        self.assertTrue(r["existing_components_preserved"])
        self.assertTrue(r["target_connected"])
        self.assertTrue(r["added_copper_connected_to_target"])
        self.assertEqual(r["modification_witness"]["anchors"],["p1","p2"])

    def test_target_success_never_hides_neighbour_break(self):
        r=connectivity_delta(snapshot(),snapshot(True,True),self.task())
        self.assertTrue(r["target_connected"])
        self.assertFalse(r["existing_components_preserved"])

    def test_default_cannot_remove_original_tracks(self):
        t=self.task();t.pop("modification")
        self.assertFalse(connectivity_delta(snapshot(),snapshot(True),t)["existing_components_preserved"])

    def test_detached_replacement_not_accepted(self):
        out=snapshot(True);out["items"]["stray"]=dict(out["items"]["newn"],uuid="stray")
        out["component_of"]["stray"]=len(out["components"]);out["components"].append(dict(net="N",items=["stray"]))
        self.assertFalse(connectivity_delta(snapshot(),out,self.task())["added_copper_connected_to_target"])

    def test_locked_outside_and_open_branch_fail_witness(self):
        for case in ["locked","outside","open","non-centre"]:
            before=snapshot()
            if case=="locked":before["items"]["n1"]["locked"]=True
            if case=="outside":before["items"]["n1"]["start_mm"]=[-2,0]
            if case=="open":before["items"].pop("p1")
            if case=="non-centre":before["items"]["p1"]["position_mm"]=[.05,0]
            with self.subTest(case=case), self.assertRaises(ValueError):witness(before,self.task())

    def test_budget_and_ambiguous_permissions_fail(self):
        for change in [dict(max_modified_tracks=1),dict(nets=["N","OTHER"]),dict(routing_uuids=["n1","n1"])]:
            t=self.task();t["modification"].update(change)
            with self.assertRaises(ValueError):witness(snapshot(),t)
