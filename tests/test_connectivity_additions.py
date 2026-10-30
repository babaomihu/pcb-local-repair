"""Added closed loops cannot hide behind an improved target or airwire count."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pcb_local.validation import connectivity_delta


def snapshot(groups):
    items, components, component_of = {}, [], {}
    for index, group in enumerate(groups):
        for uid in group:
            items[uid] = {"uuid": uid, "net": "BUS", "kind": "pad" if uid.startswith("p") else "segment"}
            component_of[uid] = index
        components.append({"net": "BUS", "items": list(group), "pads": [u for u in group if u.startswith("p")]})
    return {"items": items, "components": components, "component_of": component_of}


class AddedCopperTests(unittest.TestCase):
    def setUp(self):
        self.before = snapshot([["p1"], ["p2"], ["old_remote"]])
        self.task = {"target_net": "BUS", "target_pad_uuids": ["p1", "p2"]}

    def test_native_component_containing_target_accepts_added_path(self):
        result = connectivity_delta(self.before, snapshot([["p1", "p2", "new_path"], ["old_remote"]]), self.task)
        self.assertTrue(result["target_connected"])
        self.assertTrue(result["added_copper_connected_to_target"])

    def test_closed_island_rejected_even_with_improved_target(self):
        after = snapshot([["p1", "p2", "new_path"], ["old_remote"], ["new_loop1", "new_loop2", "new_loop3"]])
        result = connectivity_delta(self.before, after, self.task)
        self.assertTrue(result["target_connected"])
        self.assertTrue(result["existing_components_preserved"])
        self.assertFalse(result["added_copper_connected_to_target"])
        self.assertEqual(result["detached_added_items"], ["new_loop1", "new_loop2", "new_loop3"])

    def test_unrelated_original_component_does_not_authorize_new_branch(self):
        result = connectivity_delta(self.before, snapshot([["p1", "p2", "new_path"], ["old_remote", "new_branch"]]), self.task)
        self.assertFalse(result["added_copper_connected_to_target"])
        self.assertEqual(result["detached_added_items"], ["new_branch"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
