class MoodifyException(Exception):
    """Base exception for the application"""
    pass

class DatabaseConnectionError(MoodifyException):
    """"""
    pass

class SpotifyError(MoodifyException):
    """A Spotify call failed. status is 0 when no response came back (timeout, DNS, ...)."""
    def __init__(self, status: int, body: str):
        super().__init__(f"Spotify returned {status}: {body[:200]}")
        self.status = status
        self.body = body

class ReauthRequired(SpotifyError):
    """The refresh token is dead (revoked, or past Spotify's 6-month limit); the user must log in again."""
    pass

class SpotifyQuotaExceeded(SpotifyError):
    """Spotify wants us to back off: the Dev Mode quota is used up, or a 429 persists or asks
    for a longer wait than a request can sit through. Show a message; don't retry."""
    pass

class GoogleSignInError(MoodifyException):
    """Google sign-in failed after the user came back: the code exchange failed, or the ID token didn't check out."""
    pass
