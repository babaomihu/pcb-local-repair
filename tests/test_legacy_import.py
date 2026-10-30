"""Semantic-boundary tests for independently audited legacy import."""
import json
import copy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from pcb_local.legacy_import import prepare, normalized_uuid, native_semantics, check_imported
from pcb_local.legacy_worker import compare_drc
from legacy_fixture import create


class LegacyTests(unittest.TestCase):
    def source(self):
        with tempfile.TemporaryDirectory() as d:
            return create(Path(d)/'input').read_text()

    def test_deterministic_identity_only_enrichment(self):
        source=self.source();a,proof=prepare(source);b,other=prepare(source)
        self.assertEqual(a,b);self.assertEqual(proof,other)
        self.assertTrue(proof['authored_content_except_identity_equal'])
        self.assertTrue(all(x['new_uuid'] for x in proof['identity_map']))
        self.assertEqual(normalized_uuid('12345670'),'00000000-0000-0000-0000-000012345670')

    def test_legacy_zone_requires_independent_proof(self):
        with self.assertRaisesRegex(ValueError,'Legacy zones'):
            prepare(self.source().rstrip()[:-1]+'(zone (net 1))\n)')

    def test_unknown_object_refused(self):
        with self.assertRaisesRegex(ValueError,'Unsupported legacy board'):
            prepare(self.source().rstrip()[:-1]+'(group "ambiguous")\n)')

    def test_duplicate_existing_identity_refused(self):
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            prepare(self.source().replace('12345671','12345670'))

    def test_invalid_identity_refused(self):
        with self.assertRaisesRegex(ValueError,'Invalid stored'):
            prepare(self.source().replace('12345670','not-a-uuid'))

    def test_opaque_legacy_status_flags_refused(self):
        with self.assertRaisesRegex(ValueError,'status flags'):
            prepare(self.source().replace('(segment (start','(segment (status C00000) (start',1))

    def test_native_comparison_cannot_hide_geometry_or_rule_changes(self):
        a='(kicad_pcb (version 20241229) (setup (x 1)) (segment (start 1 2) (end 3 4) (uuid "a")))'
        self.assertEqual(native_semantics(a),native_semantics(a.replace('"a"','"b"')))
        self.assertNotEqual(native_semantics(a),native_semantics(a.replace('(start 1 2)','(start 2 2)')))
        self.assertNotEqual(native_semantics(a),native_semantics(a.replace('(x 1)','(x 2)')))

    def test_import_does_not_accept_repair_receipt(self):
        with self.assertRaises(ValueError):check_imported({'command':'repair','status':'accepted'})

    def test_only_builtin_silkscreen_display_alias_is_normalized(self):
        a='(kicad_pcb (layers (5 "F.SilkS" user)))'
        b='(kicad_pcb (layers (5 "F.SilkS" user "F.Silkscreen")))'
        self.assertEqual(native_semantics(a),native_semantics(b))
        self.assertNotEqual(native_semantics(a),native_semantics(b.replace('F.Silkscreen','Custom')))
        self.assertNotEqual(native_semantics(a),native_semantics(b.replace('(5 ', '(7 ')))

    def test_supplemental_findings_cannot_disappear_or_worsen(self):
        drc={'violations':[{'type':'clearance','description':'gap .1 mm'}], 'unconnected_items':[], 'ignored_checks':[], 'included_severities':['error']}
        measurement={'drc':drc,'supplemental_drc':{'drc':copy.deepcopy(drc),'checks_enabled':['clearance'],'coverage':{'complete':True}}}
        self.assertTrue(all(compare_drc(measurement,copy.deepcopy(measurement),{'stored_uuids':set()})[0].values()))
        for change in ('remove','worsen','missing'):
            other=copy.deepcopy(measurement)
            if change=='remove':other['supplemental_drc']['drc']['violations']=[]
            elif change=='worsen':other['supplemental_drc']['drc']['violations'][0]['description']='gap .05 mm'
            else:other['supplemental_drc']=None
            self.assertFalse(compare_drc(measurement,other,{'stored_uuids':set()})[0]['supplemental_drc_equal'])


if __name__=='__main__':unittest.main()
