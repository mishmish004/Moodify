"""
Playlist builds run in a background thread, so the browser never waits minutes on one request.

The jobs table tracks each build: its status, the step the progress page shows, and how it ended.
job_events is the build's log, one row per step, LLM call (with token counts), search and failure.
That log is the "session" of phase-plan.md's agent structure; Phase 15 reflects on it.
"""
import json
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Callable, Optional

from src.db import get_conn

# Friends-only: a handful of builds at once is plenty, and each one mostly waits on the network
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="build")
_pending: set[Future] = set()
_pending_lock = threading.Lock()

# Why a build stopped, as stored in jobs.error. main.py turns each into a page.
NO_SUGGESTIONS = "no_suggestions"
NO_MATCHES = "no_matches"
QUOTA = "quota"
REAUTH = "reauth"
ERROR = "error"
INTERRUPTED = "interrupted"


def create(user_id: str, prompt: str) -> int:
    with get_conn() as conn:
        return conn.execute("INSERT INTO jobs (user_id, prompt) VALUES (?, ?)", (user_id, prompt)).lastrowid


def start(fn: Callable, *args) -> Future:
    future = _executor.submit(fn, *args)
    with _pending_lock:
        _pending.add(future)
    future.add_done_callback(_forget)
    return future


def _forget(future: Future) -> None:
    with _pending_lock:
        _pending.discard(future)


def wait_idle(timeout: float) -> bool:
    """Block until every started build has ended, or the timeout passes. True if idle.
    Tests use it so no build outlives the test (and its mocks and database) that started it."""
    with _pending_lock:
        pending = set(_pending)
    return not wait(pending, timeout=timeout).not_done


def get(job_id: int, user_id: str) -> Optional[sqlite3.Row]:
    """Only the owner's jobs: the id alone is not enough."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()


def running_for(user_id: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()


def finish(job_id: int, playlist_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'done', playlist_id = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (playlist_id, job_id),
        )


def fail(job_id: int, reason: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (reason, job_id),
        )
        _log(conn, job_id, "failed", {"reason": reason})


def fail_interrupted() -> int:
    """At startup: a build still marked running was cut off by a restart. Returns how many."""
    with get_conn() as conn:
        return conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = CURRENT_TIMESTAMP WHERE status = 'running'",
            (INTERRUPTED,),
        ).rowcount


def events(job_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT kind, detail_json FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
    return [{"kind": row["kind"], **json.loads(row["detail_json"])} for row in rows]


def _log(conn: sqlite3.Connection, job_id: int, kind: str, detail: dict) -> None:
    conn.execute("INSERT INTO job_events (job_id, kind, detail_json) VALUES (?, ?, ?)", (job_id, kind, json.dumps(detail)))


class Trace:
    """What a build reports to. Without a job (live tests, scripts) it records nothing."""

    def __init__(self, job_id: Optional[int] = None):
        self.job_id = job_id

    def step(self, text: str) -> None:
        """A new stage: shown on the progress page and logged."""
        if self.job_id is not None:
            with get_conn() as conn:
                conn.execute("UPDATE jobs SET step = ? WHERE id = ?", (text, self.job_id))
                _log(conn, self.job_id, "step", {"text": text})

    def event(self, kind: str, step: Optional[str] = None, **detail) -> None:
        """Log an event. `step` also updates the progress page (a counter like "12/30") in the same write."""
        if self.job_id is not None:
            with get_conn() as conn:
                if step is not None:
                    conn.execute("UPDATE jobs SET step = ? WHERE id = ?", (step, self.job_id))
                _log(conn, self.job_id, kind, detail)


NO_TRACE = Trace()
