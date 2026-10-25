class MoodifyException(Exception):
    """Base exception for the application"""
    pass

class DatabaseConnectionError(MoodifyException):
    """"""
    pass

class ServiceError(MoodifyException):
    """A music service call failed. status is 0 when no response came back (timeout, DNS, ...).
    `service` names the service in messages; each client raises its own subclass."""
    service = "The music service"

    def __init__(self, status: int, body: str):
        super().__init__(f"{self.service} returned {status}: {body[:200]}")
        self.status = status
        self.body = body

class ReauthRequired(ServiceError):
    """The stored login is dead (revoked or expired); the user must log in again."""
    pass

class QuotaExceeded(ServiceError):
    """The service wants us to back off: its quota is used up, or a 429 persists or asks for a longer
    wait than a request can sit through. Show a message; don't retry."""
    pass

class SpotifyError(ServiceError):
    """A Spotify call failed."""
    service = "Spotify"

class SpotifyReauthRequired(SpotifyError, ReauthRequired):
    """The refresh token is dead (revoked, or past Spotify's 6-month limit)."""
    pass

class SpotifyQuotaExceeded(SpotifyError, QuotaExceeded):
    """The Dev Mode quota is used up, or a 429 persists or asks for a longer wait than a request can sit through."""
    pass
