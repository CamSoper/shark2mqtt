"""Exception classes for shark2mqtt."""


class SharkAuthError(Exception):
    """Authentication failure."""


class SharkAuthLockedError(SharkAuthError):
    """Account locked or rate-limited — do not retry."""


class AylaApiError(Exception):
    """Ayla API request failure."""
