"""Permission adversaries: no implicit via/layer authorization or projection."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from pcb_local.task_policy import permissions
from pcb_local.validation import protection, read_board
from pcb_local.scope_candidate import build_scoped_candidate
from test_scope_candidate import BASE, RULES, TASK, segment


def via(uid="via", x=5, kind="", layer="B.Cu", size=.6, drill=.3):
    return f'(via {kind} (at {x} 1) (size {size}) (drill {drill}) (layers "F.Cu" "{layer}") (net 1) (uuid "{uid}"))'


class TaskPolicy(unittest.TestCase):
    def task(self, budget=2):
        return dict(TASK, permissions=dict(schema_version=1, route_layers=["F.Cu", "B.Cu"], max_new_vias=budget, via_type="through"))

    def test_default_and_strict_schema(self):
        self.assertEqual(permissions(TASK)["max_new_vias"], 0)
        for mutate in (lambda p:p.update(max_new_vias=True), lambda p:p.update(max_new_vias=-1),
                       lambda p:p.update(route_layers=["F.Cu", "F.Cu"]),
                       lambda p:p.update(move_existing=True), lambda p:p.update(via_type="micro")):
            task=self.task();mutate(task["permissions"])
            with self.assertRaises(ValueError):permissions(task)

    def test_every_copper_layer_requires_explicit_permission(self):
        with self.assertRaises(ValueError):permissions(self.task(), ["F.Cu", "In1.Cu", "B.Cu"])
        self.assertTrue(permissions(self.task(), ["F.Cu", "B.Cu"]))

    def check(self, additions, task):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);before=root/'a.kicad_pcb';after=root/'b.kicad_pcb'
            before.write_text(BASE);after.write_text(BASE[:-1]+'\n'+'\n'.join(additions)+')')
            for path in (before,after):path.with_suffix('.kicad_pro').write_text(json.dumps(RULES))
            result=protection(read_board(before),read_board(after),before,after,task)
            scope=build_scoped_candidate(before,after,task,root/'scope')
            return result,scope

    def test_explicit_via_and_second_layer_allowed(self):
        result,scope=self.check([via(),segment('back',layer='B.Cu')],self.task())
        self.assertTrue(all(result['checks'].values()),result)
        self.assertEqual(scope['status'],'ready')

    def test_default_rejects_via_and_second_layer(self):
        result,scope=self.check([via(),segment('back',layer='B.Cu')],TASK)
        self.assertFalse(result['checks']['additions_within_permission'])
        self.assertEqual(scope['status'],'rejected')

    def test_budget_applies_before_projection(self):
        result,scope=self.check([via('a'),via('b',x=6)],self.task(1))
        self.assertFalse(result['checks']['new_via_budget_respected'])
        self.assertEqual(scope['status'],'rejected')

    def test_bad_vias_never_discarded_to_hide_violation(self):
        for bad in [via(x=20),via(kind='blind'),via(kind='micro'),via(layer='In1.Cu'),via(size=.2,drill=.3)]:
            with self.subTest(bad=bad):
                result,scope=self.check([bad],self.task())
                self.assertFalse(result['checks']['additions_within_permission'])
                self.assertEqual(scope['status'],'rejected')
                self.assertEqual(scope['discarded_segments'],[])
