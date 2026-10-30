"""Independent-engine proposals may never overwrite original board semantics."""
import json
from pathlib import Path
import tempfile
import unittest
from pcb_local.pns_adapter import compose
from pcb_local.validation import read_board, canonical
from test_scope_candidate import BASE, RULES, TASK, segment


class PNSAdapter(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root = Path(tmp.name)
        self.before = self.write('original', BASE)
        self.problem = self.write('problem', BASE.replace('(uuid "old")','(uuid "old") (locked yes)'))
        self.loaded = self.write('loaded', self.problem.read_text())

    def write(self, label, text):
        path = self.root / (label+'.kicad_pcb'); path.write_text(text)
        path.with_suffix('.kicad_pro').write_text(json.dumps(RULES)); return path

    def test_only_routing_delta_is_imported(self):
        proposed = self.write('proposed',self.loaded.read_text()[:-1]+segment('new')+')')
        result = compose(self.before,self.problem,self.loaded,proposed,TASK,self.root/'candidate')
        original, after = read_board(self.before),read_board(result)
        self.assertEqual(original['static'],after['static'])
        self.assertEqual(original['routing']['old'],after['routing']['old'])
        self.assertEqual(self.before.with_suffix('.kicad_pro').read_bytes(),result.with_suffix('.kicad_pro').read_bytes())

    def test_loaded_geometry_change_refuses_before_import(self):
        self.loaded.write_text(self.loaded.read_text().replace('(width 0.2)','(width 0.1)'))
        with self.assertRaisesRegex(ValueError,'loaded geometry'):
            compose(self.before,self.problem,self.loaded,self.loaded,TASK,self.root/'candidate')

    def test_protected_motion_and_deletion_rejected(self):
        for label, text in [('motion',self.loaded.read_text().replace('(end 2 1)','(end 3 1)')),
                            ('deletion',BASE[:BASE.index('  (segment')]+')')]:
            proposed = self.write(label,text)
            with self.subTest(label=label),self.assertRaisesRegex(ValueError,'unauthorized'):
                compose(self.before,self.problem,self.loaded,proposed,TASK,self.root/label)

    def test_explicit_selected_motion_preserves_original_static(self):
        task=dict(TASK,modification=dict(schema_version=1,mode='shove',routing_uuids=['old'],nets=['N'],max_modified_tracks=1))
        proposed=self.write('proposed',self.loaded.read_text().replace('(end 2 1)','(end 3 1)'))
        result=compose(self.before,self.problem,self.loaded,proposed,task,self.root/'candidate')
        self.assertEqual(read_board(result)['static'],read_board(self.before)['static'])
        self.assertNotEqual(read_board(result)['routing']['old'],read_board(self.before)['routing']['old'])

    def test_fixed_via_move_resize_delete_rejected(self):
        from test_task_policy import via
        original=BASE[:-1]+via()+')'
        for path in [self.before,self.problem,self.loaded]:path.write_text(original)
        for label,text in [('move',original.replace('(at 5 1)','(at 6 1)')),
                           ('resize',original.replace('(drill 0.3)','(drill 0.2)')),
                           ('delete',BASE)]:
            proposal=self.write(label,text)
            with self.subTest(label=label),self.assertRaisesRegex(ValueError,'unauthorized'):
                compose(self.before,self.problem,self.loaded,proposal,TASK,self.root/label)

    def test_new_via_refused_even_if_other_task_authorizes_vias(self):
        from test_task_policy import via
        proposal=self.write('newvia',BASE[:-1]+via()+')')
        with self.assertRaisesRegex(ValueError,'must not create'):
            compose(self.before,self.problem,self.loaded,proposal,TASK,self.root/'candidate')

    def test_engine_obstacle_mutation_rejected(self):
        proposal=self.write('badpad',self.loaded.read_text().replace('(size 1 1)','(size .5 .5)'))
        # An independent extra copper footprint, even if no original routing
        # changes, cannot alter the problem used to justify a proposed route.
        proposal.write_text(proposal.read_text()[:-1]+'(gr_line (start 0 0)(end 1 1)(layer "F.Cu"))'+')')
        with self.assertRaisesRegex(ValueError,'fixed obstacle'):
            compose(self.before,self.problem,self.loaded,proposal,TASK,self.root/'candidate')
