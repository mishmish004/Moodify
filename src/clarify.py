"""
Phase 30: ask one question before building when a request pulls against what the listener said before.

This is step 1 of Meta's PAHF loop, "seeking pre-action clarification to resolve ambiguity", which Phase 16
left out. The brief call (src/brief.py) spots the clash and writes the question with three answers. The build
puts it on the job page and waits up to WAIT_SECONDS:
  - keep: build as read, following the lesson. Also what happens when no answer comes, so a build never stalls
  - this_time: read the prompt again without that lesson, for this playlist only
  - from_now_on: the same, and the lesson is retired (memory.retire), since preferences drift
Only a build of one's own playlist asks: Blends and "hear it like a friend" carry no memory, and "Fix it" and
"More like this" already carry the listener's newest feedback.
"""
import json
import threading
import time
from dataclasses import dataclass, field
from math import ceil
from typing import Optional

from src import jobs, memory
from src.core.logger import MoodifyLogger
from src.db import get_conn
from src.jobs import Trace

WAIT_SECONDS = 20
STEP = "Checking one thing with you"
KEEP, THIS_TIME, FROM_NOW_ON = "keep", "this_time", "from_now_on"
ANSWERS = (KEEP, THIS_TIME, FROM_NOW_ON)
NO_ANSWER = "none"  # the wait ran out: the lesson is followed, as with KEEP
LABEL_CHARS = 60
QUESTION_CHARS = 240
# When the model leaves an answer's words empty
DEFAULT_LABELS = {KEEP: "Yes, keep to that", THIS_TIME: "Not this time", FROM_NOW_ON: "Not any more"}

log = MoodifyLogger.get_logger("clarify")

# Builds waiting for an answer, by job id: the answer route wakes its build straight away
_waiting: dict[int, threading.Event] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Question:
    lesson_id: int
    lesson: str                                      # the lesson's title, as the Memory page shows it
    text: str
    choices: dict[str, str] = field(hash=False)      # answer -> the words on its button, in ANSWERS order


def from_brief(brief, user_id: str) -> Optional[Question]:
    """The question the brief call wrote, if it names one of the user's active lessons. A question about
    anything else isn't asked: the listener couldn't tell what it refers to, and from_now_on would have nothing
    to retire."""
    asked = brief.clarify
    if asked is None:
        return None
    row = memory.find(user_id, asked.lesson)
    if row is None:
        log.info("%s: the brief asked about a lesson that isn't in memory: %r", user_id, asked.lesson)
        return None
    words = {KEEP: asked.keep, THIS_TIME: asked.this_time, FROM_NOW_ON: asked.from_now_on}
    choices = {answer: _line(words[answer], LABEL_CHARS) or DEFAULT_LABELS[answer] for answer in ANSWERS}
    text = _line(asked.question, QUESTION_CHARS) or f"Last time you said: {row['title']}. Keep to that today?"
    return Question(row["id"], row["title"], text, choices)


def _line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ask(trace: Trace, question: Question) -> str:
    """Show the question on the build's job page and wait up to WAIT_SECONDS. Returns the answer, or NO_ANSWER.
    Once this returns the question is closed, so an answer posted later changes nothing."""
    job_id = trace.job_id
    event = threading.Event()
    with _lock:
        _waiting[job_id] = event
    try:
        jobs.ask(job_id, json.dumps({"lesson": question.lesson, "text": question.text, "choices": question.choices,
                                     "asked_at": time.time()}), STEP)
        trace.event("question", lesson=question.lesson_id, text=question.text)
        event.wait(WAIT_SECONDS)
    finally:
        with _lock:
            _waiting.pop(job_id, None)
    with get_conn() as conn:
        conn.execute("UPDATE jobs SET answer = ? WHERE id = ? AND answer IS NULL", (NO_ANSWER, job_id))
        answer = conn.execute("SELECT answer FROM jobs WHERE id = ?", (job_id,)).fetchone()["answer"]
    trace.event("answer", answer=answer, **({"said": question.choices[answer]} if answer in ANSWERS else {}))
    return answer


def answer(job_id: int, user_id: str, choice: str) -> bool:
    """The listener's answer, from the job page. False when there's no open question on their running build
    (already answered, timed out, or never asked)."""
    if choice not in ANSWERS:
        return False
    with get_conn() as conn:
        taken = conn.execute(
            "UPDATE jobs SET answer = ? WHERE id = ? AND user_id = ? AND status = 'running'"
            " AND question_json IS NOT NULL AND answer IS NULL",
            (choice, job_id, user_id),
        ).rowcount == 1
    if taken:
        with _lock:
            waiting = _waiting.get(job_id)
        if waiting is not None:
            waiting.set()
    return taken


def for_page(job) -> Optional[dict]:
    """What the job page shows: the open question with its choices and the seconds left, or what was answered."""
    if not job["question_json"]:
        return None
    asked = json.loads(job["question_json"])
    left = ceil(WAIT_SECONDS - (time.time() - asked["asked_at"]))
    return {
        "text": asked["text"], "lesson": asked["lesson"], "choices": list(asked["choices"].items()),
        "open": job["answer"] is None, "seconds": max(0, min(WAIT_SECONDS, left)),
        "said": asked["choices"].get(job["answer"]), "no_answer": job["answer"] == NO_ANSWER,
    }


def note(question: Question, answer: str) -> str:
    """The listener's answer as a correction for the brief call and a note for the song picker."""
    text = (f"Before building, Moodify asked the listener: \"{question.text}\" They answered: "
            f"\"{question.choices[answer]}\". So this playlist goes with the request as written, not with their "
            f"earlier lesson \"{question.lesson}\".")
    return text + (" They want it that way from now on." if answer == FROM_NOW_ON else "")
