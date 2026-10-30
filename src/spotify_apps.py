"""Which of Moodify's Spotify developer apps each friend belongs to (SPOTIFY_EXTRA_CLIENT_IDS).

Spotify's Development Mode allows 5 people per app, so with more friends the owner spreads them over
several apps, and a login only works through the app whose allowlist has the friend's Spotify email.
The owner pastes each app's allowlist here (/apps), the login page asks for the email, and the lookup
sends the friend to the right app first. Dev Mode doesn't return emails, so a friend's email is only
ever what they typed; it's used as a lookup key, never shown or sent anywhere.
"""
import re
from typing import Optional

from src.core.config import get_settings
from src.db import get_conn

_EMAIL = re.compile(r"[^\s,;<>]+@[^\s,;<>]+\.[^\s,;<>]+")


def normalize(email: str) -> str:
    return email.strip().lower()


def parse(text: str) -> list[str]:
    """Every email in a pasted list, however it's separated, lowercased and without repeats."""
    return list(dict.fromkeys(normalize(m.group()) for m in _EMAIL.finditer(text)))


def app_for(email: str) -> Optional[str]:
    """The Client ID of the app whose allowlist holds this email, if the owner listed it and the app still exists."""
    with get_conn() as conn:
        row = conn.execute("SELECT oauth_client_id FROM spotify_app_members WHERE email = ?",
                           (normalize(email),)).fetchone()
    return row[0] if row and row[0] in get_settings().spotify_client_ids else None


def remember(email: str, client_id: str) -> None:
    """A login through `client_id` worked for someone who typed `email`: next time, go there first."""
    with get_conn() as conn:
        conn.execute("INSERT INTO spotify_app_members (email, oauth_client_id) VALUES (?, ?)"
                     " ON CONFLICT (email) DO UPDATE SET oauth_client_id = excluded.oauth_client_id",
                     (normalize(email), client_id))


def members() -> dict[str, list[str]]:
    """Client ID -> the emails on its list, for the owner's page."""
    out: dict[str, list[str]] = {cid: [] for cid in get_settings().spotify_client_ids}
    with get_conn() as conn:
        for email, cid in conn.execute("SELECT email, oauth_client_id FROM spotify_app_members ORDER BY email"):
            out.setdefault(cid, []).append(email)
    return out


def set_members(client_id: str, text: str) -> list[str]:
    """Replace app `client_id`'s list with the emails in `text`. An email moved here leaves its old app."""
    emails = parse(text)
    with get_conn() as conn:
        conn.execute("DELETE FROM spotify_app_members WHERE oauth_client_id = ?", (client_id,))
        conn.executemany("INSERT INTO spotify_app_members (email, oauth_client_id) VALUES (?, ?)"
                         " ON CONFLICT (email) DO UPDATE SET oauth_client_id = excluded.oauth_client_id",
                         [(e, client_id) for e in emails])
    return emails
