#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnose or explicitly install only the PCB Local KiCad 10.0.4 launcher.

Default is read-only. --execute creates a new plugin directory; existing
directories are never overwritten. This does not enable IPC or change KiCad
settings, install Python packages, open a board, or start the GUI.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "pcb_local_launcher"
APP = "/Applications/KiCad/KiCad.app/Contents/Applications/pcbnew.app"


def absolute(value):
    # Preserve the venv Python executable rather than resolving its symlink.
    return Path(value).expanduser().absolute()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli-python", required=True, type=Path,
                        help="Python 3.12 environment containing installed pcb-local-repair")
    parser.add_argument("--runtime-config", required=True, type=Path,
                        help="Existing pcb-local configure output JSON")
    parser.add_argument("--pcbnew-app", type=Path, default=Path(APP))
    parser.add_argument("--plugins-dir", type=Path,
                        default=Path.home() / "Library/Preferences/kicad/10.0/scripting/plugins")
    parser.add_argument("--tasks-root", type=Path,
                        default=Path.home() / "Documents/PCB Local Repair/tasks")
    parser.add_argument("--execute", action="store_true", help="Create the new plugin directory after diagnostics pass")
    args = parser.parse_args(argv)
    config = {key: str(absolute(getattr(args, key)))
              for key in ("cli_python", "runtime_config", "pcbnew_app", "tasks_root")}
    config["schema_version"] = 1
    parent = absolute(args.plugins_dir)
    target = parent / PLUGIN
    try:
        source = ROOT / "kicad_plugin" / PLUGIN
        files = sorted(source.glob("*.py"))
        if not files or not (source / "__init__.py").is_file():
            raise ValueError("Release package is missing the plugin source")
        if not (ROOT / "LICENSE").is_file():
            raise ValueError("Release package is missing its LICENSE")
        if target.exists() or target.is_symlink():
            raise ValueError("Existing directory is preserved; remove only a verified prior managed install first: " + str(target))
        for key in ("cli_python", "runtime_config", "pcbnew_app"):
            if not Path(config[key]).exists():
                raise ValueError("Missing " + key + ": " + config[key])
        if not os.access(config["cli_python"], os.X_OK):
            raise ValueError("CLI Python is not executable")
        if not config["pcbnew_app"].endswith(".app"):
            raise ValueError("--pcbnew-app must be the macOS pcbnew.app bundle")
        runtime = json.loads(Path(config["runtime_config"]).read_text(encoding="utf-8"))
        for key in ("krt_root", "router_python", "kicad_cli", "kicad_python", "source_fingerprint"):
            if not runtime.get(key):
                raise ValueError("Runtime configuration is missing " + key)
        env = dict(os.environ)
        for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
            env.pop(key, None)
        probe = subprocess.run([config["cli_python"], "-B", "-c",
            "import json,sys,pcb_local; print(json.dumps({'python':list(sys.version_info[:3]),'cli':pcb_local.__version__}))"],
            capture_output=True, text=True, timeout=15, env=env, cwd=tempfile.gettempdir())
        if probe.returncode:
            raise ValueError("CLI package import failed; install the CLI into this environment first: " + probe.stderr[-2000:])
        versions = json.loads(probe.stdout)
        expected_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        if versions["python"][:2] != [3, 12] or versions["cli"] != expected_version:
            raise ValueError("This launcher requires Python 3.12 / pcb-local-repair " + expected_version + ": " + repr(versions))
        manifest = {"managed_plugin": PLUGIN, "schema_version": 1, "versions": versions,
            "source_directory": str(source), "installation_directory": str(target),
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            "config": config,
            "scope": "Only this newly created plugin directory; no KiCad settings, other plugins, active boards or IPC changes"}
        if args.execute:
            parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation never replaces even an empty existing folder.
            # Publish __init__.py last so KiCad cannot register a partial copy.
            target.mkdir(exist_ok=False)
            try:
                for path in files:
                    if path.name != "__init__.py":
                        shutil.copy2(path, target / path.name)
                shutil.copy2(ROOT / "LICENSE", target / "LICENSE")
                (target / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                (target / "INSTALL_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                shutil.copy2(source / "__init__.py", target / "__init__.py")
            except BaseException:
                shutil.rmtree(target)
                raise
        print(json.dumps({"status": "installed" if args.execute else "diagnostic_only",
                          "target": str(target), "config": config, "versions": versions,
                          "next": "Reload KiCad action plugins or restart KiCad after saving your own work; confirm the actual plugin search path."},
                         ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc), "target": str(target)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
