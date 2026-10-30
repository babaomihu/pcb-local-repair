"""Plugin opens only managed PNS outputs with CLI acceptance evidence.

Synthetic acceptance ledgers test the bridge policy; no GUI or KiCad routing
is performed. The actual delivery checker runs in an isolated CLI subprocess.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('pool_plugin_bridge',ROOT/'kicad_plugin/pcb_local_launcher/process.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)


def hashes(board):
    return {str(p):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (board,board.with_suffix('.kicad_pro'))}


class PluginCandidatePoolTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
        self.real_run=subprocess.run
        cli=ROOT/'.venv-krt/bin/python'
        self.config={'cli_python':str(cli if cli.is_file() else Path(sys.executable)), 'pcbnew_app':'test-pcbnew'}
        self.opened=[];self.checked=[]

    def fixture(self,label,relative='pns-candidates/0/board.kicad_pcb',batch=False):
        folder=self.root/label;folder.mkdir();owner=folder/'03-repair' if batch else folder
        owner.mkdir(exist_ok=True)
        source=folder/'source/board.kicad_pcb';source.parent.mkdir()
        source.write_text('(kicad_pcb)');source.with_suffix('.kicad_pro').write_text('{}')
        candidate=owner/relative;candidate.parent.mkdir(parents=True)
        candidate.write_text('(kicad_pcb (segment))');candidate.with_suffix('.kicad_pro').write_text('{}')
        prefix=candidate.parent.parent.parent
        verifier=prefix/'candidate-validations/1/verification';verifier.mkdir(parents=True)
        verified=dict(verification_complete=True,accepted=True,
            checks={'full_drc_no_new_or_changed':True,'existing_components_preserved':True,
                    'source_files_untouched':True},source_hashes_after=hashes(candidate))
        (verifier/'verification.json').write_text(json.dumps(verified))
        report=dict(task_dir=str(owner),source_board=str(source),source_hashes=hashes(source),
            candidate=str(candidate),candidate_hashes=hashes(candidate),command='repair',
            accepted=True,status='accepted',exit_code=0,verification=verified,verification_directory=str(verifier),
            candidate_pool=dict(status='accepted',selected_fingerprint='selected',candidates=[dict(
                status='accepted',fingerprint='selected',board=str(candidate),hashes=hashes(candidate),
                verification_directory=str(verifier))]))
        (owner/'result.json').write_text(json.dumps(report));(owner/'status.json').write_text('{"status":"accepted"}')
        if batch:
            top=dict(report,task_dir=str(folder),command='batch',accepted_chain=[dict(
                result=str(owner/'result.json'),sha256=hashlib.sha256((owner/'result.json').read_bytes()).hexdigest())])
            (folder/'result.json').write_text(json.dumps(top));(folder/'status.json').write_text('{"status":"accepted"}')
        process=Mock();process.poll.return_value=0
        task=dict(folder=folder,kind='batch' if batch else 'repair',process=process)
        state=dict(status='accepted',accepted=True,candidate=str(candidate))
        return task,state,report,owner,verifier

    def run_bridge(self,task,state):
        def run(argv,**kwargs):
            if argv[0]=='/usr/bin/open':
                self.opened.append(argv);return subprocess.CompletedProcess(argv,0,'','')
            # The normal bridge uses an installed CLI. This source-level test
            # injects only the checkout import path into that same isolated
            # checker process, retaining its real gate and exit status.
            self.checked.append(list(argv));argv=list(argv)
            index=argv.index('-c')+1
            argv[index]='import sys;sys.path.insert(0,'+repr(str(ROOT/'src'))+');'+argv[index]
            return self.real_run(argv,**kwargs)
        with patch.object(bridge,'query',return_value=state),patch.object(bridge.subprocess,'run',side_effect=run):
            return bridge.open_candidate(self.config,task)

    def test_direct_auto_and_batch_pool_paths_use_authoritative_evidence(self):
        for label,relative,batch in [('direct','pns-candidates/0/board.kicad_pcb',False),
            ('last','pns-candidates/7/board.kicad_pcb',False),
            ('auto','strategies/push-local-refill/pns-candidates/3/board.kicad_pcb',False),
            ('batch','strategies/pns-fixed/pns-candidates/2/board.kicad_pcb',True)]:
            with self.subTest(label=label):
                task,state,*_=self.fixture(label,relative,batch)
                self.assertEqual(self.run_bridge(task,state),state)
                self.assertEqual(self.opened[-1][-1],state['candidate'])
        self.assertEqual(len(self.checked),4);self.assertEqual(len(self.opened),4)

    def test_selected_pool_status_fingerprint_path_and_hash_must_match(self):
        for mode in ('pending','fingerprint','path','hash'):
            task,state,report,owner,_=self.fixture(mode)
            row=report['candidate_pool']['candidates'][0]
            if mode=='pending':row['status']='pending'
            elif mode=='fingerprint':row['fingerprint']='different'
            elif mode=='path':row['board']=str(owner/'pns-candidates/1/board.kicad_pcb')
            else:row['hashes']={}
            (owner/'result.json').write_text(json.dumps(report))
            with self.subTest(mode=mode),self.assertRaises(ValueError):self.run_bridge(task,state)
        self.assertEqual(self.opened,[])

    def test_native_report_incomplete_changed_or_wrong_location_is_refused(self):
        for mode in ('incomplete','changed','location'):
            task,state,report,owner,verifier=self.fixture(mode)
            if mode=='incomplete':
                report['verification']['verification_complete']=False
                (verifier/'verification.json').write_text(json.dumps(report['verification']))
            elif mode=='changed':(verifier/'verification.json').write_text('{}')
            else:
                report['verification_directory']=str(owner/'untrusted/verification')
                report['candidate_pool']['candidates'][0]['verification_directory']=report['verification_directory']
            (owner/'result.json').write_text(json.dumps(report))
            with self.subTest(mode=mode),self.assertRaises(ValueError):self.run_bridge(task,state)
        self.assertEqual(self.opened,[])

    def test_candidate_tamper_or_batch_link_tamper_after_status_is_refused(self):
        for mode in ('candidate','batch'):
            task,state,report,owner,_=self.fixture(mode,batch=mode=='batch')
            if mode=='candidate':Path(state['candidate']).write_text('modified')
            else:(owner/'result.json').write_text(json.dumps(report)+' ')
            with self.subTest(mode=mode),self.assertRaises(ValueError):self.run_bridge(task,state)
        self.assertEqual(self.opened,[])

    def test_pool_number_or_strategy_is_not_an_arbitrary_directory_allowance(self):
        for label,relative in [('outside-range','pns-candidates/8/board.kicad_pcb'),
            ('negative','pns-candidates/-1/board.kicad_pcb'),
            ('strategy','strategies/unknown/pns-candidates/0/board.kicad_pcb')]:
            task,state,*_=self.fixture(label,relative)
            with self.subTest(label=label),self.assertRaises(ValueError):self.run_bridge(task,state)
        self.assertEqual(self.opened,[]);self.assertEqual(self.checked,[])


if __name__=='__main__':unittest.main()
