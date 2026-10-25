"""
Phase 27: the YouTube Data API's daily quota, counted by Moodify.

The quota belongs to the Google Cloud project, so every YouTube user of Moodify shares it. Google resets it at
midnight Pacific time. Each call adds its cost here before it's sent, since Google charges failed calls too.
A build checks it can afford its writes before making any (src/youtube/client.py check_room), so a used-up
quota never leaves a half-made playlist. Google's own count can differ; its 403 quotaExceeded ends a build
the same way.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.core.config import get_settings
from src.db import get_conn

SERVICE = "youtube"
PACIFIC = ZoneInfo("America/Los_Angeles")
SHOWN_IN = ZoneInfo("Asia/Kolkata")  # where Moodify's friends are
KEEP_DAYS = 7


def day(now: datetime | None = None) -> str:
    """The quota day: the date in Pacific time."""
    return (now or datetime.now(timezone.utc)).astimezone(PACIFIC).strftime("%Y-%m-%d")


def spent(now: datetime | None = None) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT units FROM service_quota WHERE service = ? AND day = ?", (SERVICE, day(now))).fetchone()
    return row["units"] if row else 0


def left(now: datetime | None = None) -> int:
    return max(0, get_settings().YOUTUBE_DAILY_QUOTA - spent(now))


def spend(units: int, now: datetime | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO service_quota (service, day, units) VALUES (?, ?, ?)"
            " ON CONFLICT (service, day) DO UPDATE SET units = units + excluded.units",
            (SERVICE, day(now), units),
        )


def resets_at(now: datetime | None = None) -> datetime:
    """The next Pacific midnight."""
    local = (now or datetime.now(timezone.utc)).astimezone(PACIFIC)
    midnight = datetime(local.year, local.month, local.day, tzinfo=PACIFIC) + timedelta(days=1)
    return midnight.astimezone(timezone.utc)


def words(now: datetime | None = None) -> tuple[str, str]:
    """(title, sentence) for a build or page stopped by the quota."""
    at = resets_at(now).astimezone(SHOWN_IN)
    clock = at.strftime("%I:%M %p").lstrip("0")
    return ("YouTube's daily limit is used up",
            "YouTube gives Moodify a daily allowance that everyone on it shares, and today's is used up. Nothing was"
            f" made. It resets at {clock} India time.")


def prune() -> int:
    """Forget days older than KEEP_DAYS. Run at startup."""
    cutoff = (datetime.now(PACIFIC) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        return conn.execute("DELETE FROM service_quota WHERE day < ?", (cutoff,)).rowcount
