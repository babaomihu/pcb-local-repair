"""A capacity string cannot bypass native runtime identity or completion."""
import json
from pathlib import Path
import tempfile
import unittest
from pcb_local import drc_complete as d


class CompleteDRCChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cli = self.root/'MacOS/kicad-cli'; self.cli.parent.mkdir(); self.cli.write_text('test cli')
        self.native = self.root/'PlugIns/_pcbnew.kiface'; self.native.parent.mkdir(); self.native.write_text('test native')
        self.path = self.cli.with_name(d.MANIFEST_NAME)
        self.record = dict(schema_version=1, source_commit=d.SOURCE_COMMIT, patch_sha256=d.PATCH_SHA256,
                           kicad_version='10.0.4', per_type_capacity=d.REPORT_CAPACITY,
                           extended_capacity=d.REPORT_CAPACITY, runtime_root=str(self.root),
                           cli_relative='MacOS/kicad-cli', files={str(p.relative_to(self.root)):d.sha256(p) for p in (self.cli,self.native)})
        self.path.write_text(json.dumps(self.record))

    def test_bound_runtime_and_exact_trace(self):
        runtime=d.load_runtime(self.cli)
        clean,evidence=d.capacity_trace('12:00:00: Trace: (KICAD_DRC_PROFILE) '+d.TRACE_TEXT+'\nother native diagnostic',runtime)
        self.assertEqual(clean,'other native diagnostic')
        self.assertEqual(evidence['per_type'],d.REPORT_CAPACITY)
        self.assertEqual(d.environment(runtime),{'KICAD_RUN_FROM_BUILD_DIR':'1'})

    def test_release_trace_requires_the_same_pinned_runtime(self):
        line='12:00:00: Trace: '+d.TRACE_TEXT
        clean,evidence=d.capacity_trace(line,d.load_runtime(self.cli))
        self.assertEqual(clean,'')
        self.assertEqual(evidence['per_type'],d.REPORT_CAPACITY)
        with self.assertRaises(ValueError):d.capacity_trace(line,None)

    def test_changed_native_provider_rejected(self):
        self.native.write_text('changed')
        with self.assertRaises(ValueError):d.load_runtime(self.cli)

    def test_changed_linked_runtime_dependency_rejected(self):
        library=self.root/'linked-lib.dylib';library.write_text('external-library')
        self.record['external_files']={str(library):d.sha256(library)}
        self.path.write_text(json.dumps(self.record));self.assertTrue(d.load_runtime(self.cli)['verified'])
        library.write_text('changed-library')
        with self.assertRaises(ValueError):d.load_runtime(self.cli)

    def test_missing_native_module_and_unknown_patch_rejected(self):
        for key,value in [('patch_sha256','a'*64),('files',{'MacOS/kicad-cli':d.sha256(self.cli)})]:
            self.path.write_text(json.dumps(dict(self.record,**{key:value})))
            with self.assertRaises(ValueError):d.load_runtime(self.cli)

    def test_missing_duplicate_unknown_or_forged_marker_rejected(self):
        runtime=d.load_runtime(self.cli);line='Trace: (KICAD_DRC_PROFILE) '+d.TRACE_TEXT
        for text in ['',line+'\n'+line,line.replace('2147483647','999999')]:
            with self.assertRaises(ValueError):d.capacity_trace(text,runtime)
        with self.assertRaises(ValueError):d.capacity_trace(line,None)

    def test_stock_runtime_keeps_original_gate(self):
        self.path.unlink()
        self.assertIsNone(d.load_runtime(self.cli))
        self.assertEqual(d.capacity_trace('ordinary native trace',None),('ordinary native trace',None))
        self.assertEqual(d.environment(None),{})
