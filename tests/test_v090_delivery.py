"""Delivery boundary checks for v0.9.0 paths, using synthetic proof ledgers.

Native workflow/save proof is separately exercised by check_trace_quality_cli_native.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from pcb_local.delivery import accepted_result, export_result, adopt_result
from pcb_local.jobs import write_json
from pcb_local.workspace import source_files


class NewDeliveryChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.folder=self.root/'task';self.folder.mkdir()
        self.source=self.root/'original/board.kicad_pcb';self.source.parent.mkdir()
        self.source.write_text('(kicad_pcb (version 20241229))')
        self.source.with_suffix('.kicad_pro').write_text('{}')
        self.candidate=self.folder/'trace-quality/proposal/board.kicad_pcb';self.candidate.parent.mkdir(parents=True)
        self.candidate.write_text('(kicad_pcb (version 20241229) (segment))');self.candidate.with_suffix('.kicad_pro').write_text('{}')
        self.verifier=self.folder/'trace-quality/verification';self.verifier.mkdir()
        self.verified=dict(accepted=True,verification_complete=True,checks={'native':True},source_hashes_after=source_files(self.candidate))
        self.report=dict(status='accepted',accepted=True,exit_code=0,command='repair',task_dir=str(self.folder),
            source_board=str(self.source),source_hashes=source_files(self.source),candidate=str(self.candidate),candidate_hashes=source_files(self.candidate),
            verification=self.verified,verification_directory=str(self.verifier),trace_quality=dict(status='accepted',candidate_board=str(self.candidate),
                candidate_hashes=source_files(self.candidate),verification_directory=str(self.verifier)))
        write_json(self.folder/'status.json',dict(status='accepted'));self.save()
    def save(self):
        write_json(self.folder/'result.json',self.report);write_json(self.verifier/'verification.json',self.verified)

    def test_quality_export_and_adoption_require_real_path_and_backup(self):
        original=self.source.read_bytes()
        self.assertEqual(export_result(self.folder,self.root/'export')['status'],'exported')
        lock=self.source.parent/('~'+self.source.name+'.lck');lock.write_text('open')
        with self.assertRaises(ValueError):adopt_result(self.folder,original_closed=True)
        lock.unlink()
        adopted=adopt_result(self.folder,original_closed=True)
        self.assertEqual(adopted['status'],'adopted');self.assertEqual(Path(adopted['backup_board']).read_bytes(),original)
        self.assertEqual(self.source.read_bytes(),self.candidate.read_bytes())

    def test_missing_quality_native_evidence_is_rejected(self):
        self.report.pop('trace_quality');self.save()
        with self.assertRaises(ValueError):accepted_result(self.folder)

    def test_quality_native_report_tampering_is_rejected(self):
        write_json(self.verifier/'verification.json',dict(self.verified,accepted=False))
        with self.assertRaises(ValueError):accepted_result(self.folder)

    def test_candidate_context_binding_is_required(self):
        self.verified['source_hashes_after']={};self.save()
        with self.assertRaises(ValueError):accepted_result(self.folder)

    def test_unmanaged_nested_quality_paths_are_rejected(self):
        for strategy in ['push','add-only','pns-fixed','multilayer','ripup']:
            path=self.folder/'strategies'/strategy/'trace-quality/proposal/board.kicad_pcb'
            path.parent.mkdir(parents=True);path.write_bytes(self.candidate.read_bytes());path.with_suffix('.kicad_pro').write_text('{}')
            self.report.update(candidate=str(path),candidate_hashes=source_files(path));self.save()
            with self.assertRaises(ValueError,msg=strategy):accepted_result(self.folder)

    def test_focused_branch_must_be_selected(self):
        label='ripup-focused-0123456789ab';path=self.folder/'strategies'/label/'scoping/candidate/board.kicad_pcb'
        path.parent.mkdir(parents=True);path.write_bytes(self.candidate.read_bytes());path.with_suffix('.kicad_pro').write_text('{}')
        self.report.update(candidate=str(path),candidate_hashes=source_files(path),automatic_strategy={'selected':'ripup'})
        self.save()
        with self.assertRaises(ValueError):accepted_result(self.folder)
        self.report['automatic_strategy']['selected']=label;self.save()
        self.assertEqual(accepted_result(self.folder)['candidate'],str(path))

    def test_malformed_focused_hash_path_is_rejected(self):
        label='ripup-focused-not-a-bound-hash';path=self.folder/'strategies'/label/'scoping/candidate/board.kicad_pcb'
        path.parent.mkdir(parents=True);path.write_bytes(self.candidate.read_bytes());path.with_suffix('.kicad_pro').write_text('{}')
        self.report.update(candidate=str(path),candidate_hashes=source_files(path),automatic_strategy={'selected':label});self.save()
        with self.assertRaises(ValueError):accepted_result(self.folder)

if __name__=='__main__':unittest.main()
