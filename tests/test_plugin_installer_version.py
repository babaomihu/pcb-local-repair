"""Release installer must follow the packaged version and reject other CLIs."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess


class InstallerVersionTests(unittest.TestCase):
    def run_installer(self, cli_version):
        script = Path(__file__).resolve().parents[1] / 'scripts/install_kicad_plugin.py'
        spec = importlib.util.spec_from_file_location('release_installer_test', script)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'kicad_plugin/pcb_local_launcher'
            source.mkdir(parents=True)
            (source / '__init__.py').write_text('# test launcher\n')
            (root / 'LICENSE').write_text('test license')
            # A future version deliberately prevents hardcoding this release.
            (root / 'pyproject.toml').write_text('[project]\nversion = "12.3.4"\n')
            python = root / 'python'; python.write_text('#!/bin/sh\n'); python.chmod(0o755)
            app = root / 'pcbnew.app'; app.mkdir()
            config = root / 'runtime.json'
            config.write_text(json.dumps({key: 'test' for key in
                ['krt_root', 'router_python', 'kicad_cli', 'kicad_python', 'source_fingerprint']}))
            probe = subprocess.CompletedProcess([], 0, json.dumps({'python': [3, 12, 14], 'cli': cli_version}), '')
            with patch.object(installer, 'ROOT', root), patch.object(installer.subprocess, 'run', return_value=probe), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = installer.main(['--cli-python', str(python), '--runtime-config', str(config),
                    '--pcbnew-app', str(app), '--plugins-dir', str(root / 'plugins'), '--execute'])
            target = root / 'plugins/pcb_local_launcher'
            manifest = json.loads((target / 'INSTALL_MANIFEST.json').read_text()) if target.exists() else None
            return code, manifest

    def test_matching_package_version_installs(self):
        code, manifest = self.run_installer('12.3.4')
        self.assertEqual(code, 0)
        self.assertEqual(manifest['versions']['cli'], '12.3.4')

    def test_other_cli_version_refuses_before_writing(self):
        code, manifest = self.run_installer('0.8.9')
        self.assertEqual(code, 1)
        self.assertIsNone(manifest)
