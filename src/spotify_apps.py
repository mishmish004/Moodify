"""Which of Moodify's Spotify developer apps each friend belongs to (SPOTIFY_EXTRA_CLIENT_IDS).

Spotify's Development Mode allows 5 people per app, so with more friends the owner spreads them over
several apps, and a login only works through the app whose allowlist has the friend's account. The
owner pastes each app's list here (/apps): the Spotify emails from the dashboard, plus the phone number
of any friend who logs in to Spotify with one. The login page asks for either, and the lookup sends the
friend to the right app first. Dev Mode never tells Moodify a friend's email or phone number, so it's
only ever what someone typed; it's used as a lookup key, never shown to anyone but the owner or sent anywhere.

A phone number is kept as its last 10 digits, so +91 98765 43210, 098765-43210 and 9876543210 are the
same friend however they were typed. Fewer than 10 digits, country code included, isn't taken as one.
"""
import re
from typing import Optional

from src.core.config import get_settings
from src.db import get_conn

_EMAIL = re.compile(r"[^\s,;<>]+@[^\s,;<>]+\.[^\s,;<>]+")
_PHONE = re.compile(r"\+?\(?\d[\d\s().-]*\d")
PHONE_DIGITS = 10


def normalize(account: str) -> Optional[str]:
    """The lookup key for an email or phone number as someone typed it, or None when it's neither."""
    account = account.strip()
    if "@" in account:
        return account.lower() if _EMAIL.fullmatch(account) else None
    if not re.fullmatch(r"\(?\+?[\d\s().-]+", account):
        return None
    digits = re.sub(r"\D", "", account)
    return digits[-PHONE_DIGITS:] if PHONE_DIGITS <= len(digits) <= 15 else None  # 15: the longest in the world


def parse(text: str) -> list[str]:
    """Every email and phone number in a pasted list, as lookup keys, without repeats. Emails are found in any
    layout; a phone number needs a line, comma or semicolon of its own, since spaces can sit inside one."""
    found = []
    for chunk in re.split(r"[\n,;]", text):
        found += [m.group() for m in _EMAIL.finditer(chunk)]
        found += [m.group() for m in _PHONE.finditer(_EMAIL.sub(" ", chunk))]
    keys = (normalize(f) for f in found)
    return list(dict.fromkeys(k for k in keys if k))


def app_for(account: str) -> Optional[str]:
    """The Client ID of the app whose list holds this email or phone number, if the owner listed it and the app
    still exists."""
    key = normalize(account)
    if not key:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT oauth_client_id FROM spotify_app_members WHERE account = ?", (key,)).fetchone()
    return row[0] if row and row[0] in get_settings().spotify_client_ids else None


def remember(account: str, client_id: str, user_id: str) -> None:
    """A login through `client_id` worked for `user_id`, who typed `account`: next time, go there first. The row
    is linked to the user, so deleting their data deletes it too."""
    key = normalize(account)
    if not key:
        return
    with get_conn() as conn:
        conn.execute("INSERT INTO spotify_app_members (account, oauth_client_id, user_id) VALUES (?, ?, ?)"
                     " ON CONFLICT (account) DO UPDATE SET oauth_client_id = excluded.oauth_client_id,"
                     " user_id = excluded.user_id", (key, client_id, user_id))


def members() -> dict[str, list[str]]:
    """Client ID -> the emails and phone numbers on its list, for the owner's page."""
    out: dict[str, list[str]] = {cid: [] for cid in get_settings().spotify_client_ids}
    with get_conn() as conn:
        for account, cid in conn.execute("SELECT account, oauth_client_id FROM spotify_app_members ORDER BY account"):
            out.setdefault(cid, []).append(account)
    return out


def set_members(client_id: str, text: str) -> list[str]:
    """Make app `client_id`'s list the emails and phone numbers in `text`. One moved here leaves its old app.
    Rows still listed are kept, not written again, so one a friend's own login linked to them stays linked."""
    accounts = parse(text)
    with get_conn() as conn:
        listed = {r[0] for r in conn.execute("SELECT account FROM spotify_app_members WHERE oauth_client_id = ?",
                                             (client_id,))}
        conn.executemany("DELETE FROM spotify_app_members WHERE account = ?",
                         [(a,) for a in listed - set(accounts)])
        conn.executemany("INSERT INTO spotify_app_members (account, oauth_client_id) VALUES (?, ?)"
                         " ON CONFLICT (account) DO UPDATE SET oauth_client_id = excluded.oauth_client_id",
                         [(a, client_id) for a in accounts])
    return accounts
