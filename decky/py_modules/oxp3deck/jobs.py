"""Background jobs: long work (a Limine rebuild, xess-xmx-linux's 300 MB of downloads) runs in a thread, one job at
a time, while the frontend polls its log. Each job is saved to RUNTIME_DIR/jobs/<id>.json when it starts, now and
then while it runs, and when it ends, so a job cut short by a plugin restart shows up as failed afterwards.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import traceback

from . import context, userexec

log = logging.getLogger("oxp3deck")

LOG_LINES = 500
SAVE_EVERY = 5.0      # seconds between saves of a running job's log
KEEP_JOBS = 30        # job files and jobs in memory
INTERRUPTED = "interrupted (the plugin restarted)"


class Job:
    def __init__(self, kind: str, title: str, job_id: str | None = None):
        self.id = job_id or time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        self.kind = kind
        self.title = title
        self.state = "running"
        self.started = time.time()
        self.ended: float | None = None
        self.log: list[str] = []
        self.result = None
        self.error: str | None = None
        self._lock = threading.Lock()
        self._on_change = None     # set by Jobs: saves the job now and then

    def say(self, line) -> None:
        """Add a line (or several, split at newlines) to the log; it keeps the last 500."""
        lines = [s.rstrip() for s in str(line).splitlines()] or [""]
        with self._lock:
            self.log.extend(lines)
            del self.log[:-LOG_LINES]
        for s in lines:
            log.info("[%s] %s", self.kind, s)
        if self._on_change is not None:
            self._on_change(self)

    def to_dict(self, tail: int | None = None) -> dict:
        with self._lock:
            lines = list(self.log) if tail is None else (self.log[-tail:] if tail > 0 else [])
            return {"id": self.id, "kind": self.kind, "title": self.title, "state": self.state,
                    "started": self.started, "ended": self.ended, "log": lines, "result": self.result,
                    "error": self.error}

    def _finish(self, state: str, result, error: str | None) -> None:
        with self._lock:
            self.state = state
            self.result = result
            self.error = error
            self.ended = time.time()

    @classmethod
    def _from_dict(cls, d: dict) -> Job | None:
        """A job read back from its file (which the user can edit: take only well-formed fields)."""
        jid = d.get("id")
        if not isinstance(jid, str) or not jid:
            return None
        job = cls(str(d.get("kind") or "?"), str(d.get("title") or "?"), jid[:64])
        job.state = d.get("state") if d.get("state") in ("running", "done", "failed") else "failed"
        job.started = _num(d.get("started")) or 0.0
        job.ended = _num(d.get("ended"))
        log_lines = d.get("log")
        job.log = [str(s) for s in log_lines[-LOG_LINES:]] if isinstance(log_lines, list) else []
        job.result = d.get("result")
        job.error = str(d["error"]) if d.get("error") is not None else None
        return job


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class Jobs:
    def __init__(self, runtime_dir: str):
        self.dir = os.path.join(runtime_dir, "jobs")
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._running: Job | None = None
        self._saved_at: dict[str, float] = {}
        self.interrupted: list[Job] = []   # the jobs _load found running: a plugin restart cut them short
        try:
            # Decky made RUNTIME_DIR and gives it to the user: no link the user planted above it is followed
            fd = userexec.open_dir(runtime_dir)
            try:
                os.mkdir("jobs", 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            finally:
                os.close(fd)
        except OSError as e:
            log.warning("jobs: cannot create %s: %s", self.dir, e)
        self._load()

    def _load(self) -> None:
        # The user owns RUNTIME_DIR: the folder is opened without following a link they planted (above it or at
        # jobs/ itself), and it is listed and its files read through that descriptor, never by path again.
        try:
            dfd = context._open_folder(self.dir)
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("jobs: cannot open %s (a symlink?): %s; old jobs are not shown", self.dir, e)
            return
        try:
            names = [n for n in os.listdir(dfd) if n.endswith(".json") and not n.startswith(".")]
            raws = {n: context.read_small(n, 4_000_000, dir_fd=dfd) for n in names}
        except OSError:
            return
        finally:
            os.close(dfd)
        loaded = []
        for n in names:
            raw = raws[n]
            try:
                d = json.loads(raw.decode("utf-8")) if raw is not None else None
            except ValueError:
                d = None
            job = Job._from_dict(d) if isinstance(d, dict) else None
            if job is None or n != job.id + ".json":
                continue
            if job.state == "running":
                job.state = "failed"
                job.error = INTERRUPTED
                job.ended = job.ended or time.time()
                self._save(job)
                self.interrupted.append(job)
            loaded.append(job)
        loaded.sort(key=lambda j: j.started)
        for job in loaded[-KEEP_JOBS:]:
            self._jobs[job.id] = job
        for job in loaded[:-KEEP_JOBS]:
            self._remove_file(job.id)

    def _save(self, job: Job) -> None:
        self._saved_at[job.id] = time.monotonic()
        try:
            data = json.dumps(job.to_dict(), default=str, indent=1).encode("utf-8")
            context.write_atomic(os.path.join(self.dir, job.id + ".json"), data)
        except (OSError, TypeError, ValueError) as e:
            log.warning("jobs: cannot save %s: %s", job.id, e)

    def _save_now_and_then(self, job: Job) -> None:
        if time.monotonic() - self._saved_at.get(job.id, 0.0) >= SAVE_EVERY:
            self._save(job)

    def _remove_file(self, job_id: str) -> None:
        # Relative to a folder opened without following a link: a jobs/ the user swapped for a symlink since the
        # load must not make root delete <id>.json somewhere else.
        try:
            dfd = context._open_folder(self.dir)
        except OSError:
            return
        try:
            os.unlink(job_id + ".json", dir_fd=dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)

    def set_error(self, job: Job, error: str) -> None:
        """Change an ended job's error text (and save it): main.py words an interrupted oxp3-tool job."""
        with job._lock:
            job.error = error
        self._save(job)

    def start(self, kind: str, title: str, fn) -> tuple[Job | None, str | None]:
        """Run fn(job) -> dict in a thread. Returns (job, None), or (None, why) when another job is running."""
        with self._lock:
            if self._running is not None:
                return None, f"another task is running: {self._running.title}"
            job = Job(kind, title)
            self._running = job
            self._jobs[job.id] = job
            for old in list(self._jobs)[:-KEEP_JOBS]:
                del self._jobs[old]
                self._remove_file(old)
        self._save(job)
        job._on_change = self._save_now_and_then
        try:
            threading.Thread(target=self._run, args=(job, fn), name=f"job-{kind}", daemon=True).start()
        except RuntimeError as e:
            self._end(job, "failed", None, f"{kind}: cannot start a thread: {e}")
            return None, f"{kind}: cannot start a thread: {e}"
        return job, None

    def _run(self, job: Job, fn) -> None:
        state, result, error = "failed", None, f"{job.kind}: stopped"
        try:
            result = fn(job)
            if not isinstance(result, dict):
                result = {"ok": False, "error": f"{job.kind}: returned {type(result).__name__}, not a dict"}
            result = json.loads(json.dumps(result, default=str))   # what the frontend will see
            state = "done" if result.get("ok") else "failed"
            error = None if state == "done" else str(result.get("error") or f"{job.kind}: failed")
        except Exception as e:
            result, error = None, f"{job.kind}: {type(e).__name__}: {e}"
            job.say(traceback.format_exc())
        finally:
            self._end(job, state, result, error)

    def _end(self, job: Job, state: str, result, error: str | None) -> None:
        job._on_change = None
        job._finish(state, result, error)
        try:
            self._save(job)
        finally:
            with self._lock:
                if self._running is job:
                    self._running = None
            self._saved_at.pop(job.id, None)

    def get(self, job_id: str | None = None) -> Job | None:
        """The job with that id; with None the running job, else the newest."""
        with self._lock:
            if job_id:
                return self._jobs.get(job_id)
            if self._running is not None:
                return self._running
            return max(self._jobs.values(), key=lambda j: j.started, default=None)

    def running(self) -> Job | None:
        with self._lock:
            return self._running

    def recent(self, n: int = 10) -> list[Job]:
        """The newest n jobs, newest first."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)
        return jobs[:n]
