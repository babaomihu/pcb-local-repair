"""Bounded subprocesses and cooperative cancellation for one isolated task."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Halt(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def stop_group(process):
    # Children inherit this fresh process group, including KiCad spawned by
    # the validation worker. Never signal a PID read from a persisted file.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            if process.poll() is not None:
                break
            raise
        if sig == signal.SIGTERM:
            time.sleep(0.5)
    process.wait()


class Job:
    def __init__(self, folder, timeout=240, parent_check=None):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=False)
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self.cancelled = False
        self.parent_check = parent_check
        self.record = {"schema_version": 1, "pid": os.getpid(), "status": "running",
                       "phase": "preflight", "started_at": datetime.now(timezone.utc).isoformat(),
                       "timeout_seconds": timeout, "steps": []}
        self.handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self.handlers[sig] = signal.signal(sig, self._interrupt)
        self.save()

    def _interrupt(self, _signal, _frame):
        self.cancelled = True

    def save(self):
        self.record["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        write_json(self.folder / "status.json", self.record)

    def check(self):
        if self.parent_check is not None:
            self.parent_check()
        if self.cancelled or (self.folder / "cancel.request").exists():
            raise Halt("cancelled", "Cancellation requested; candidate cannot be accepted")
        if time.monotonic() >= self.deadline:
            raise Halt("timed_out", "Whole-task deadline exhausted")

    def run(self, label, argv, *, cwd=None, env=None, timeout=60):
        self.check()
        step_dir = self.folder / "logs" / label
        step_dir.mkdir(parents=True, exist_ok=False)
        start = time.monotonic()
        deadline = min(self.deadline, start + timeout)
        step = {"label": label, "argv": [str(x) for x in argv],
                "cwd": str(Path(cwd or Path.cwd()).resolve()),
                "environment_overrides": env or {}, "timeout_seconds": deadline - start,
                "status": "starting", "started_at": datetime.now(timezone.utc).isoformat()}
        self.record.update(phase=label)
        self.record["steps"].append(step)
        process = None
        try:
            with (step_dir / "output.log").open("wb") as log:
                process = subprocess.Popen(step["argv"], cwd=cwd,
                    env={**os.environ, **(env or {})}, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                step.update(pid=process.pid, status="running")
                write_json(step_dir / "command.json", step)
                self.save()
                while process.poll() is None:
                    self.check()
                    if time.monotonic() >= deadline:
                        raise Halt("timed_out", label + " exceeded its stage budget")
                    time.sleep(0.1)
                self.check()
                step.update(exit_code=process.returncode,
                            status="succeeded" if process.returncode == 0 else "failed")
                # A command can exit while a helper is still alive. Its
                # remaining process group has no work to keep doing here.
                stop_group(process)
        except Halt as exc:
            if process is not None:
                stop_group(process)
            step.update(status=exc.status, exit_code=130 if exc.status == "cancelled" else 124)
            raise
        except BaseException:
            if process is not None:
                stop_group(process)
            step.update(status="launch_or_execution_error", exit_code=127)
            raise
        finally:
            step["elapsed_seconds"] = round(time.monotonic() - start, 3)
            write_json(step_dir / "command.json", step)
            self.save()
        return step["exit_code"]

    def finish(self, status):
        self.record.update(status=status, finished_at=datetime.now(timezone.utc).isoformat())
        self.save()
        for sig, handler in self.handlers.items():
            signal.signal(sig, handler)
