#!/usr/bin/env python3
"""Run one bounded command, retaining its exact argv, output and exit status."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def stop_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            # macOS can return EPERM when the process group has just exited.
            # Ignore that only after reaping the child proves it is terminal.
            if process.poll() is not None:
                break
            raise
        if sig == signal.SIGTERM:
            time.sleep(0.5)
    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--cwd', type=Path, default=Path.cwd())
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--env', action='append', default=[])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.timeout <= 0:
        parser.error('a command and positive timeout are required')
    overrides = dict(item.split('=', 1) for item in args.env)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    record = {
        'argv': command, 'cwd': str(args.cwd.resolve()),
        'environment_overrides': overrides, 'timeout_seconds': args.timeout,
        'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'starting',
    }
    record_file = run_dir / 'command.json'
    def save():
        temporary = record_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(record_file)
    save()
    start = time.monotonic()
    print(str(run_dir), flush=True)
    process = None
    try:
        with (run_dir / 'output.log').open('wb') as log:
            process = subprocess.Popen(command, cwd=args.cwd, env={**os.environ, **overrides},
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            record.update(pid=process.pid, status='running')
            save()
            try:
                code = process.wait(timeout=args.timeout)
                status = 'succeeded' if code == 0 else 'failed'
            except subprocess.TimeoutExpired:
                stop_group(process)
                code, status = 124, 'timed_out'
            except KeyboardInterrupt:
                stop_group(process)
                code, status = 130, 'cancelled'
    except OSError as exc:
        record['error'] = str(exc)
        code, status = 127, 'launch_failed'
    finally:
        record.update(status=locals().get('status', 'interrupted'),
                      exit_code=locals().get('code', 130),
                      elapsed_seconds=round(time.monotonic() - start, 3),
                      finished_at=datetime.now(timezone.utc).isoformat())
        save()
    print(json.dumps({'status': status, 'exit_code': code, 'run_dir': str(run_dir)}), flush=True)
    return code if 0 <= code <= 255 else 1


if __name__ == '__main__':
    sys.exit(main())
