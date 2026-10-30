"""
Phase 31: recall lessons by meaning, as well as by their tags.

Google's ReasoningBank retrieves memory items "using embedding-based similarity search". Moodify does the same
next to Phase 16's tag overlap: every active lesson gets an embedding of its title and description (what it's
about and when it applies), and each recall embeds what the build is about. A lesson fits a query when it's
close in absolute terms (MIN_SIMILARITY), or much closer than the user's typical lesson (ABOVE_MEDIAN, once there
are MEDIAN_OF lessons to compare). Both numbers were measured on text-embedding-3-large at 512 dimensions on
2026-10-27, with 12 made-up lessons and 17 requests: together they found 14 of the 17 lessons that fit, with one
that didn't. Embedding the content too, or using text-embedding-3-small, did worse. Tags still count, so
"homework" finds a focus lesson through memory.TAG_GROUPS even where the embeddings miss it.

One embeddings call per recall: lessons still missing their vector go in the same request as the queries.
Nothing here raises. A failed call, or OPENAI_EMBEDDING_MODEL left empty, leaves recall to the tags.
"""
import time
from array import array
from math import sqrt
from statistics import median
from typing import Iterable, Optional

from openai import APIConnectionError, InternalServerError, OpenAI, OpenAIError

from src.core.config import get_settings
from src.core.logger import MoodifyLogger
from src.db import get_conn
from src.jobs import NO_TRACE, Trace

DIMENSIONS = 512
TIMEOUT = 15.0
ATTEMPTS = 2           # a timeout, 5xx or dropped connection gets one more try
MIN_SIMILARITY = 0.36  # this close to a query: the lesson fits
ABOVE_MEDIAN = 0.15    # or this much closer than the median of the user's lessons
MEDIAN_OF = 5          # lessons needed before the median means anything
MERGE_SIMILARITY = 0.45  # consolidation also looks at existing lessons this close to a new one
QUERY_CHARS = 2000

log = MoodifyLogger.get_logger("embeddings")


def model() -> str:
    return get_settings().OPENAI_EMBEDDING_MODEL.strip()


def made_with() -> str:
    """What lessons.embedded_with holds for a vector that's current. Another value means embed again."""
    return f"{model()}/{DIMENSIONS}"


def lesson_text(lesson) -> str:
    """What a lesson is embedded as (a row or a memory.Draft): its title and when it applies."""
    title, description = (lesson["title"], lesson["description"]) if hasattr(lesson, "keys") else (
        lesson.title, lesson.description)
    return f"{' '.join(title.split())}. {' '.join(description.split())}"


def embed(texts: list[str], trace: Trace = NO_TRACE) -> Optional[list[list[float]]]:
    """Unit vectors for `texts`, in order, or None when embeddings are off or the call failed."""
    name = model()
    if not name:
        return None
    if not texts:
        return []
    client = OpenAI(api_key=get_settings().OPENAI_API_KEY.get_secret_value(), timeout=TIMEOUT, max_retries=0)
    inputs = [text[:QUERY_CHARS] or "-" for text in texts]  # the API refuses empty strings
    for attempt in range(1, ATTEMPTS + 1):
        started = time.monotonic()
        try:
            response = client.embeddings.create(model=name, input=inputs, dimensions=DIMENSIONS)
            vectors = [_unit(item.embedding) for item in sorted(response.data, key=lambda item: item.index)]
            if len(vectors) != len(inputs):
                raise ValueError(f"{len(vectors)} embeddings for {len(inputs)} texts")
        except (OpenAIError, ValueError, TypeError) as exc:
            retry = isinstance(exc, (APIConnectionError, InternalServerError))
            log.warning("embeddings call failed (attempt %d): %s: %s", attempt, type(exc).__name__, exc)
            trace.event("embed", model=name, attempt=attempt, ok=False, error=type(exc).__name__,
                        seconds=round(time.monotonic() - started, 2))
            if not retry:
                return None
            continue
        usage = getattr(response, "usage", None)
        trace.event("embed", model=name, attempt=attempt, ok=True, texts=len(inputs),
                    tokens=getattr(usage, "prompt_tokens", None), seconds=round(time.monotonic() - started, 2))
        return vectors
    return None


def _unit(vector) -> list[float]:
    length = sqrt(sum(x * x for x in vector)) or 1.0
    return [x / length for x in vector]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack(blob: bytes) -> list[float]:
    vector = array("f")
    vector.frombytes(blob)
    return list(vector)


# --- lesson vectors ---------------------------------------------------------------------------------------

def _active(user_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, title, description, embedding, embedded_with FROM lessons"
            " WHERE user_id = ? AND status = 'active' AND scope = 'lasting' ORDER BY id",
            (user_id,),
        ).fetchall()


def _vectors(user_id: str, extra: list[str], trace: Trace) -> Optional[tuple[dict[int, list[float]], list[list[float]]]]:
    """({lesson id: vector} for every active lasting lesson, vectors of `extra`), embedding the lessons that
    lack a current vector in the same call. None when embeddings are off or the call failed."""
    if not model():
        return None
    rows = _active(user_id)
    current = made_with()
    missing = [r for r in rows if r["embedding"] is None or r["embedded_with"] != current]
    vectors = embed([lesson_text(r) for r in missing] + extra, trace) if missing or extra else []
    if vectors is None:
        return None
    if missing:
        _save(user_id, missing, vectors[:len(missing)])
    fresh = {r["id"]: v for r, v in zip(missing, vectors)}
    lessons = {r["id"]: fresh.get(r["id"]) or unpack(r["embedding"]) for r in rows}
    return lessons, vectors[len(missing):]


def _save(user_id: str, rows, vectors: list[list[float]]) -> None:
    """Store each lesson's vector, unless its title or description changed while it was being embedded
    (consolidation rewrote it): then it stays missing and is embedded again next time."""
    with get_conn() as conn:
        conn.executemany(
            "UPDATE lessons SET embedding = ?, embedded_with = ?"
            " WHERE id = ? AND user_id = ? AND title = ? AND description = ?",
            [(pack(v), made_with(), r["id"], user_id, r["title"], r["description"]) for r, v in zip(rows, vectors)],
        )


def fill(user_id: str, trace: Trace = NO_TRACE) -> None:
    """Embed the user's active lessons that lack a current vector, so the next build doesn't wait on it.
    Called after a review's lessons are stored, in the background."""
    _vectors(user_id, [], trace)


def relevant(user_id: str, queries: Iterable[str], trace: Trace = NO_TRACE) -> dict[int, float]:
    """{lesson id: similarity} for the active lessons that fit at least one query, with the best similarity
    among the queries they fit. Empty when nothing fits, embeddings are off, or the call failed."""
    queries = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
    if not queries or not _active(user_id):
        return {}
    got = _vectors(user_id, queries, trace)
    if got is None:
        return {}
    lessons, asked = got
    fits: dict[int, float] = {}
    for query in asked:
        scores = {lesson_id: _dot(query, vector) for lesson_id, vector in lessons.items()}
        typical = median(scores.values()) if len(scores) >= MEDIAN_OF else None
        for lesson_id, score in scores.items():
            if score >= MIN_SIMILARITY or (typical is not None and score - typical >= ABOVE_MEDIAN):
                fits[lesson_id] = max(fits.get(lesson_id, score), score)
    return fits


def close_to(user_id: str, drafts: list, trace: Trace = NO_TRACE) -> set[int]:
    """Ids of active lessons within MERGE_SIMILARITY of one of the new lessons (memory.Draft), so consolidation
    sees a lesson that says the same thing, or the opposite, in other words and under other tags."""
    if not drafts or not _active(user_id):
        return set()
    got = _vectors(user_id, [lesson_text(d) for d in drafts], trace)
    if got is None:
        return set()
    lessons, new = got
    return {lesson_id for lesson_id, vector in lessons.items()
            if any(_dot(vector, draft) >= MERGE_SIMILARITY for draft in new)}
