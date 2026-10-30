"""File-level delivery tests; synthetic acceptance ledgers, no GUI automation."""
import copy
import json
import os
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_local import delivery
from pcb_local.jobs import write_json
from pcb_local.workspace import source_files, sha256


class DeliveryChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.original = self.root/'original'/'用户板.kicad_pcb'
        self.original.parent.mkdir()
        self.original.write_text('(kicad_pcb (version 20260101))')
        self.task = self.root/'task'; self.task.mkdir()
        self.candidate = self.task/'scoping/candidate/board.kicad_pcb'
        self.candidate.parent.mkdir(parents=True)
        self.candidate.write_text('(kicad_pcb (version 20260101) (segment))')
        for board in [self.original, self.candidate]:
            board.with_suffix('.kicad_pro').write_text('{"rules":"frozen"}')
            board.with_suffix('.kicad_dru').write_text('(version 1)')
            (board.parent/'fp-lib-table').write_text('(fp_lib_table (lib (uri "${KIPRJMOD}/parts.pretty")))')
            (board.parent/'parts.pretty').mkdir()
            (board.parent/'parts.pretty/P.kicad_mod').write_text('(footprint "P")')
        self.original.parent.joinpath('用户板.kicad_sch').write_text('untouched schematic')
        self.report = dict(schema_version=1, status='accepted', accepted=True, exit_code=0,
            command='repair', task_dir=str(self.task), source_board=str(self.original),
            candidate=str(self.candidate), source_hashes=source_files(self.original), candidate_hashes=source_files(self.candidate))
        self.save_report()
        write_json(self.task/'status.json', {'status':'accepted'})
        self.original_bytes = self.original.read_bytes()

    def save_report(self):write_json(self.task/'result.json',self.report)

    def test_pool_export_requires_selected_entry_and_exact_native_evidence(self):
        moved=self.task/'pns-candidates/3';moved.parent.mkdir()
        self.candidate.parent.rename(moved);self.candidate=moved/'board.kicad_pcb'
        hashes=source_files(self.candidate)
        verifier=self.task/'candidate-validations/1/verification';verifier.mkdir(parents=True)
        verified=dict(accepted=True,verification_complete=True,checks={'native':True},source_hashes_after=hashes)
        write_json(verifier/'verification.json',verified)
        self.report.update(candidate=str(self.candidate),candidate_hashes=hashes,verification=verified,
            verification_directory=str(verifier),candidate_pool=dict(status='accepted',selected_fingerprint='key',
            candidates=[dict(status='accepted',fingerprint='key',board=str(self.candidate),hashes=hashes,verification_directory=str(verifier))]))
        self.save_report()
        self.assertEqual(delivery.export_result(self.task,self.root/'pool-export')['status'],'exported')
        self.report['candidate_pool']['candidates'][0]['status']='pending';self.save_report()
        with self.assertRaises(ValueError):delivery.export_result(self.task,self.root/'bad-pool')
        self.report['candidate_pool']['candidates'][0]['status']='accepted';self.save_report()
        write_json(verifier/'verification.json',dict(verified,accepted=False))
        with self.assertRaises(ValueError):delivery.export_result(self.task,self.root/'bad-evidence')

    def test_unwitnessed_pool_path_is_not_a_deliverable_candidate(self):
        moved=self.task/'pns-candidates/0';moved.parent.mkdir()
        self.candidate.parent.rename(moved);self.candidate=moved/'board.kicad_pcb'
        self.report.update(candidate=str(self.candidate),candidate_hashes=source_files(self.candidate));self.save_report()
        with self.assertRaises(ValueError):delivery.export_result(self.task,self.root/'unwitnessed')

    def test_export_preserves_complete_context_and_source(self):
        dest=self.root/'export'
        result=delivery.export_result(self.task,dest)
        self.assertEqual(result['status'],'exported')
        self.assertEqual((dest/'board.kicad_pcb').read_bytes(),self.candidate.read_bytes())
        self.assertEqual(delivery.files_relative(dest/'board.kicad_pcb',source_files(dest/'board.kicad_pcb')),
                         delivery.files_relative(self.candidate,self.report['candidate_hashes']))
        self.assertEqual(self.original.read_bytes(),self.original_bytes)
        self.assertTrue((dest/'pcb-local-export.json').is_file())
        self.assertFalse((dest/'.pcb-local-incomplete').exists())
        self.assertFalse((dest/'用户板.kicad_sch').exists())

    def test_export_existing_directory_never_overwritten(self):
        dest=self.root/'export';dest.mkdir();(dest/'keep').write_text('keep')
        with self.assertRaises(ValueError):delivery.export_result(self.task,dest)
        self.assertEqual(list(dest.iterdir()),[dest/'keep'])

    def test_geometry_candidate_export_and_adopt_keep_same_checks(self):
        moved=self.task/'geometry-search/candidate'
        moved.parent.mkdir()
        self.candidate.parent.rename(moved)
        self.candidate=moved/'board.kicad_pcb'
        self.report.update(candidate=str(self.candidate),candidate_hashes=source_files(self.candidate))
        self.save_report()
        result=delivery.export_result(self.task,self.root/'geometry-export')
        self.assertEqual(result['status'],'exported')
        result=delivery.adopt_result(self.task,original_closed=True)
        self.assertEqual(result['status'],'adopted')
        self.assertEqual(Path(result['backup_board']).read_bytes(),self.original_bytes)
        self.assertEqual(self.original.read_bytes(),self.candidate.read_bytes())

    def test_auto_geometry_candidate_export_and_adopt_keep_same_checks(self):
        moved=self.task/'strategies/geometry/geometry-search/candidate'
        moved.parent.mkdir(parents=True)
        self.candidate.parent.rename(moved)
        self.candidate=moved/'board.kicad_pcb'
        self.report.update(candidate=str(self.candidate),candidate_hashes=source_files(self.candidate))
        self.save_report()
        result=delivery.export_result(self.task,self.root/'geometry-export')
        self.assertEqual(result['status'],'exported')
        result=delivery.adopt_result(self.task,original_closed=True)
        self.assertEqual(result['status'],'adopted')
        self.assertEqual(Path(result['backup_board']).read_bytes(),self.original_bytes)
        self.assertEqual(self.original.read_bytes(),self.candidate.read_bytes())

    def test_export_inside_task_rejected(self):
        with self.assertRaises(ValueError):delivery.export_result(self.task,self.task/'export')

    def test_local_refill_auto_exports_require_exact_path_acceptance_and_hashes(self):
        baseline=copy.deepcopy(self.report)
        for strategy in ('push-local-refill','pns-fixed-local-refill','push-local-refill-extra'):
            with self.subTest(strategy=strategy):
                folder=self.task/'strategies'/strategy/'scoping/candidate'
                shutil.copytree(self.candidate.parent,folder)
                candidate=folder/'board.kicad_pcb'
                self.report=dict(baseline,candidate=str(candidate),candidate_hashes=source_files(candidate))
                self.save_report()
                destination=self.root/(strategy+'-export')
                if strategy.endswith('-extra'):
                    with self.assertRaisesRegex(ValueError,'validated task output'):
                        delivery.export_result(self.task,destination)
                    self.assertFalse(destination.exists())
                    continue
                exported=delivery.export_result(self.task,destination)
                self.assertEqual(Path(exported['board']).read_bytes(),candidate.read_bytes())
                self.report['accepted']=False;self.save_report()
                with self.assertRaises(ValueError):delivery.accepted_result(self.task)
                self.report['accepted']=True;self.save_report()
                candidate.write_text('stale candidate')
                with self.assertRaises(ValueError):delivery.accepted_result(self.task)
        self.assertEqual(self.original.read_bytes(),self.original_bytes)

    def test_batch_local_refill_exports_recheck_final_child_hash(self):
        baseline=copy.deepcopy(self.report)
        for index,strategy in enumerate(('push-local-refill','pns-fixed-local-refill')):
            with self.subTest(strategy=strategy):
                child=self.task/('%02d-repair'%index)
                folder=child/'strategies'/strategy/'scoping/candidate'
                shutil.copytree(self.candidate.parent,folder)
                candidate=folder/'board.kicad_pcb'
                child_report=dict(baseline,task_dir=str(child),candidate=str(candidate),candidate_hashes=source_files(candidate))
                write_json(child/'result.json',child_report)
                self.report=dict(baseline,command='batch',candidate=str(candidate),candidate_hashes=source_files(candidate),
                    accepted_chain=[{'result':str(child/'result.json'),'sha256':sha256(child/'result.json')}])
                self.save_report()
                result=delivery.export_result(self.task,self.root/('batch-'+strategy))
                self.assertEqual(Path(result['board']).read_bytes(),candidate.read_bytes())
                (child/'result.json').write_text('stale child ledger')
                with self.assertRaises(ValueError):delivery.accepted_result(self.task)

    def test_export_stale_source_or_candidate_rejected(self):
        self.candidate.write_text('changed')
        with self.assertRaises(ValueError):delivery.export_result(self.task,self.root/'export')
        self.assertFalse((self.root/'export').exists())

    def test_nonaccepted_result_never_delivered(self):
        for status in ['no_progress','cancelled','verification_incomplete','running']:
            with self.subTest(status=status):
                self.report['status']=status;self.save_report()
                with self.assertRaises(ValueError):delivery.export_result(self.task,self.root/'export')
                with self.assertRaises(ValueError):delivery.adopt_result(self.task,original_closed=True)
        self.assertEqual(self.original.read_bytes(),self.original_bytes)

    def test_adopt_backups_and_replaces_only_pcb(self):
        old_context=source_files(self.original)
        result=delivery.adopt_result(self.task,original_closed=True)
        self.assertEqual(result['status'],'adopted')
        self.assertEqual(self.original.read_bytes(),self.candidate.read_bytes())
        self.assertEqual(Path(result['backup_board']).read_bytes(),self.original_bytes)
        after=source_files(self.original)
        self.assertEqual({k:v for k,v in old_context.items() if k!=str(self.original)},
                         {k:v for k,v in after.items() if k!=str(self.original)})
        self.assertEqual(self.original.with_suffix('.kicad_sch').read_text(),'untouched schematic')
        self.assertEqual(delivery.read(Path(result['backup_dir'])/'pcb-local-adoption.json')['status'],'adopted')
        with self.assertRaises(ValueError):delivery.adopt_result(self.task,original_closed=True)

    def test_adopt_requires_closed_confirmation(self):
        with self.assertRaises(ValueError):delivery.adopt_result(self.task)
        self.assertEqual(self.original.read_bytes(),self.original_bytes)

    def test_kicad_board_and_project_locks_refused(self):
        for suffix in ['.kicad_pcb','.kicad_pro']:
            lock=self.original.parent/('~'+self.original.with_suffix(suffix).name+'.lck');lock.touch()
            with self.assertRaisesRegex(ValueError,'锁文件'):delivery.adopt_result(self.task,original_closed=True)
            lock.unlink()
        self.assertFalse((self.original.parent/'.pcb-local-backups').exists())

    def test_rules_change_requires_export_not_adoption(self):
        self.candidate.with_suffix('.kicad_dru').write_text('(version 1)\n')
        self.report['candidate_hashes']=source_files(self.candidate);self.save_report()
        with self.assertRaisesRegex(ValueError,'context differs'):delivery.adopt_result(self.task,original_closed=True)
        self.assertEqual(self.original.read_bytes(),self.original_bytes)

    def test_concurrent_original_change_before_commit_is_preserved(self):
        original_copy=delivery.copy_exact
        def changed(*args):
            result=original_copy(*args);self.original.write_text('new user work');return result
        with patch.object(delivery,'copy_exact',side_effect=changed):
            with self.assertRaises(ValueError):delivery.adopt_result(self.task,original_closed=True)
        self.assertEqual(self.original.read_text(),'new user work')
        backups=list((self.original.parent/'.pcb-local-backups').glob('*/用户板.kicad_pcb'))
        self.assertEqual(backups[0].read_bytes(),self.original_bytes)

    def test_failed_atomic_replace_preserves_original_and_backup(self):
        replace=os.replace
        def fail_source(a,b):
            if Path(b)==self.original:raise OSError('test replace failure')
            return replace(a,b)
        with patch.object(delivery.os,'replace',side_effect=fail_source):
            with self.assertRaises(OSError):delivery.adopt_result(self.task,original_closed=True)
        self.assertEqual(self.original.read_bytes(),self.original_bytes)
        self.assertFalse(list(self.original.parent.glob('*.pcb-local-adopt.lock')))
        self.assertFalse(list(self.original.parent.glob('.*.pcb-local-*')))
        self.assertEqual(len(list((self.original.parent/'.pcb-local-backups').glob('*/用户板.kicad_pcb'))),1)

    def test_failed_receipt_after_commit_is_not_misreported_as_unchanged(self):
        writer=delivery.write_json;calls=[]
        def fail_receipt(*args):
            calls.append(args)
            if len(calls)==2:raise OSError('receipt failed')
            return writer(*args)
        with patch.object(delivery,'write_json',side_effect=fail_receipt):
            result=delivery.adopt_result(self.task,original_closed=True)
        self.assertEqual(result['status'],'adopted_needs_review')
        self.assertEqual(self.original.read_bytes(),self.candidate.read_bytes())
        self.assertTrue(Path(result['backup_board']).is_file())

    def test_export_candidate_change_during_copy_leaves_incomplete_marker(self):
        copier=delivery.copy_exact
        def changed(*args):
            result=copier(*args);self.candidate.write_text('changed later');return result
        with patch.object(delivery,'copy_exact',side_effect=changed):
            with self.assertRaises(ValueError):delivery.export_result(self.task,self.root/'export')
        self.assertTrue((self.root/'export/.pcb-local-incomplete').is_file())
        self.assertFalse((self.root/'export/pcb-local-export.json').exists())

    def test_linked_backup_directory_refused(self):
        elsewhere=self.root/'elsewhere';elsewhere.mkdir()
        (self.original.parent/'.pcb-local-backups').symlink_to(elsewhere,target_is_directory=True)
        with self.assertRaises(ValueError):delivery.adopt_result(self.task,original_closed=True)
        self.assertFalse(list(elsewhere.iterdir()))

    def test_batch_export_uses_final_accepted_child(self):
        child=self.task/'02-repair';child.mkdir()
        self.candidate.parent.parent.rename(child/'scoping')
        candidate=child/'scoping/candidate/board.kicad_pcb'
        child_report=dict(self.report,task_dir=str(child),candidate=str(candidate),candidate_hashes=source_files(candidate))
        write_json(child/'result.json',child_report)
        self.report.update(command='batch',candidate=str(candidate),candidate_hashes=source_files(candidate),
            accepted_chain=[{'result':str(child/'result.json'),'sha256':sha256(child/'result.json')}])
        self.save_report()
        result=delivery.export_result(self.task,self.root/'export')
        self.assertEqual(Path(result['board']).read_bytes(),candidate.read_bytes())
        (child/'result.json').write_text('changed')
        with self.assertRaises(ValueError):delivery.adopt_result(self.task,original_closed=True)

    def test_batch_auto_geometry_export_uses_final_accepted_child(self):
        child=self.task/'02-repair';child.mkdir()
        (child/'strategies/geometry').mkdir(parents=True)
        self.candidate.parent.parent.rename(child/'strategies/geometry/geometry-search')
        candidate=child/'strategies/geometry/geometry-search/candidate/board.kicad_pcb'
        child_report=dict(self.report,task_dir=str(child),candidate=str(candidate),candidate_hashes=source_files(candidate))
        write_json(child/'result.json',child_report)
        self.report.update(command='batch',candidate=str(candidate),candidate_hashes=source_files(candidate),
            accepted_chain=[{'result':str(child/'result.json'),'sha256':sha256(child/'result.json')}])
        self.save_report()
        result=delivery.export_result(self.task,self.root/'export')
        self.assertEqual(Path(result['board']).read_bytes(),candidate.read_bytes())
        (child/'result.json').write_text('changed')
        with self.assertRaises(ValueError):delivery.adopt_result(self.task,original_closed=True)

if __name__=='__main__': unittest.main()
