"""Cache identity tests; mocked comparisons are not native PCB evidence."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pcb_local.jobs import Halt, write_json
from pcb_local.preparation_cache import NetclassPreparationCache, _KRT_FILES, _source_hashes
from pcb_local.workspace import sha256


class PreparationCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        krt = self.root / 'krt' / 'py_router'; krt.mkdir(parents=True)
        for name in _KRT_FILES: (krt / name).write_text(name)
        native = self.root / 'native'; (native / 'bin').mkdir(parents=True)
        (native / 'bin/python3.9').write_bytes(b'native interpreter identity')
        modules = native / 'lib/python3.9/site-packages'; modules.mkdir(parents=True)
        (modules / 'pcbnew.py').write_bytes(b'native wrapper identity')
        (modules / '_pcbnew.so').write_bytes(b'native binary identity')
        self.config = dict(krt_root=str(krt.parent), router_python=sys.executable,
                           kicad_python=str(native / 'bin/python3.9'), kicad_cli=sys.executable,
                           source_fingerprint={'engine': 'v1'}, verified_kicad_version='10.0.4')
        self.cache = NetclassPreparationCache()
        self.calls = 0

    def inputs(self, name, board_text='saved-board', rule_text='original-rules'):
        folder = self.root / name; folder.mkdir()
        directory = folder / 'input'; directory.mkdir()
        board = directory / 'board.kicad_pcb'; board.write_text(board_text)
        board.with_suffix('.kicad_pro').write_text(rule_text)
        return SimpleNamespace(folder=folder, check=lambda: None), board, folder / 'netclasses.json'

    def producer(self, board, probe, *, success=True, mutate=False):
        def run():
            self.calls += 1
            native = dict(version=self.config['verified_kicad_version'], nets={'N': {'width': .25}})
            report = dict(board=str(board), source_hashes=_source_hashes(board, lambda: None),
                          native=native, native_exit_code=0, passed=success,
                          inputs_unchanged=True, krt={'passed': success},
                          native_command=['worker', str(board), str(probe.with_suffix('.native.json'))],
                          krt_source_hashes={name: sha256(Path(self.config['krt_root']) / 'py_router' / name)
                                             for name in _KRT_FILES})
            write_json(probe, report); write_json(probe.with_suffix('.native.json'), native)
            probe.with_suffix('.native.stdout.log').write_text('native stdout\n')
            probe.with_suffix('.native.stderr.log').write_text('')
            if mutate: board.write_text('changed during computation')
            return 0 if success else 1
        return run

    def run_probe(self, name, **kwargs):
        job, board, probe = self.inputs(name, **kwargs)
        code = self.cache.run(job, self.config, board, probe, self.producer(board, probe))
        return code, json.loads((job.folder / 'preparation-cache.json').read_text()), json.loads(probe.read_text())

    def test_identical_snapshot_reuses_and_rebinds_only_input_paths(self):
        _, first, original = self.run_probe('one')
        _, second, reused = self.run_probe('two')
        self.assertEqual((first['status'], second['status'], self.calls), ('stored', 'hit', 1))
        self.assertEqual(first['identity_sha256'], second['identity_sha256'])
        self.assertEqual(reused['board'], str(self.root / 'two/input/board.kicad_pcb'))
        self.assertEqual(reused['native_command'], original['native_command'])
        self.assertTrue(reused['preparation_cache']['native_command_is_historical'])
        self.assertEqual(reused['native'], original['native'])
        self.assertFalse(second['candidate_validation_reused'])
        self.assertFalse(second['native_preflight_reused'])

    def test_changed_board_and_rules_miss(self):
        self.run_probe('one')
        self.assertEqual(self.run_probe('two', board_text='changed')[1]['status'], 'stored')
        self.assertEqual(self.run_probe('three', rule_text='stricter')[1]['status'], 'stored')
        self.assertEqual(self.calls, 3)

    def test_added_rule_sidecar_misses(self):
        self.run_probe('one')
        job, board, probe = self.inputs('two'); board.with_suffix('.kicad_dru').write_text('(rule changed)')
        self.cache.run(job, self.config, board, probe, self.producer(board, probe))
        self.assertEqual(self.calls, 2)

    def test_runtime_version_and_actual_parser_source_invalidate(self):
        self.run_probe('one')
        self.config['verified_kicad_version'] = '10.0.5'
        self.run_probe('two')
        (Path(self.config['krt_root']) / 'py_router' / _KRT_FILES[0]).write_text('new parser')
        self.run_probe('three')
        self.config['source_fingerprint'] = {'engine': 'v2'}
        self.run_probe('four')
        self.assertEqual(self.calls, 4)

    def test_project_library_content_invalidates(self):
        for index, content in enumerate(('old', 'new')):
            job, board, probe = self.inputs(str(index))
            (board.parent / 'fp-lib-table').write_text('(uri "${KIPRJMOD}/library.pretty")')
            library = board.parent / 'library.pretty'; library.mkdir()
            (library / 'P.kicad_mod').write_text(content)
            self.cache.run(job, self.config, board, probe, self.producer(board, probe))
        self.assertEqual(self.calls, 2)

    def test_environment_change_invalidates(self):
        self.run_probe('one')
        with patch.dict(os.environ, {'PCB_LOCAL_TEST_CONTEXT': 'different'}): self.run_probe('two')
        self.assertEqual(self.calls, 2)

    def test_native_binary_changed_under_same_version_invalidates(self):
        self.run_probe('one')
        (self.root / 'native/lib/python3.9/site-packages/_pcbnew.so').write_bytes(b'changed binary')
        self.run_probe('two')
        self.assertEqual(self.calls, 2)

    def test_unknown_native_layout_runs_without_cache(self):
        self.config['kicad_python'] = sys.executable
        self.assertEqual(self.run_probe('one')[1]['status'], 'bypass')
        self.assertEqual(self.run_probe('two')[1]['status'], 'bypass')
        self.assertEqual(self.calls, 2)
        self.assertFalse(self.cache._entries)

    def test_corrupt_memory_entry_is_evicted_then_recomputed(self):
        self.run_probe('one')
        key, entry = next(iter(self.cache._entries.items()))
        outputs = list(entry.outputs); suffix, data, digest = outputs[0]
        outputs[0] = (suffix, data + b'garbage', digest)
        self.cache._entries[key] = replace(entry, outputs=tuple(outputs))
        _, receipt, _ = self.run_probe('two')
        self.assertEqual(self.calls, 2)
        self.assertIn('identity mismatch', receipt['invalidated_reason'])

    def test_failed_comparison_never_stored(self):
        job, board, probe = self.inputs('one')
        self.assertEqual(self.cache.run(job, self.config, board, probe,
                                       self.producer(board, probe, success=False)), 1)
        self.assertFalse(self.cache._entries)
        self.run_probe('two'); self.assertEqual(self.calls, 2)

    def test_cancelled_hit_does_not_materialize_or_start_process(self):
        self.run_probe('one')
        job, board, probe = self.inputs('two')
        def cancel(): raise Halt('cancelled', 'test request')
        job.check = cancel
        with self.assertRaises(Halt) as caught:
            self.cache.run(job, self.config, board, probe, self.producer(board, probe))
        self.assertEqual(caught.exception.status, 'cancelled')
        self.assertFalse(probe.exists()); self.assertEqual(self.calls, 1)

    def test_mutation_during_producer_is_protection_violation(self):
        job, board, probe = self.inputs('one')
        with self.assertRaises(Halt) as caught:
            self.cache.run(job, self.config, board, probe, self.producer(board, probe, mutate=True))
        self.assertEqual(caught.exception.status, 'protection_violation')
        self.assertFalse(self.cache._entries)

    def test_bounded_lru_and_oversized_entries(self):
        self.cache = NetclassPreparationCache(max_entries=1)
        self.run_probe('one'); self.run_probe('two', board_text='different'); self.run_probe('three')
        self.assertEqual(self.calls, 3); self.assertEqual(len(self.cache._entries), 1)
        self.cache = NetclassPreparationCache(max_bytes=1)
        self.assertEqual(self.run_probe('four')[1]['status'], 'not_cached')
        self.assertFalse(self.cache._entries)

    def test_new_batch_has_no_previous_entry(self):
        self.run_probe('one'); self.cache = NetclassPreparationCache(); self.run_probe('two')
        self.assertEqual(self.calls, 2)

    def test_existing_output_is_not_overwritten_by_hit(self):
        self.run_probe('one'); job, board, probe = self.inputs('two')
        probe.write_text('keep me')
        with self.assertRaises(ValueError):
            self.cache.run(job, self.config, board, probe, self.producer(board, probe))
        self.assertEqual(probe.read_text(), 'keep me')


if __name__ == '__main__': unittest.main()
