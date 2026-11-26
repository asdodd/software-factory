"""Errors every adapter raises in place of its native exceptions."""


class PortError(Exception):
    """Base for all adapter errors. Messages must never contain secrets."""


class NotFound(PortError):
    pass


class Conflict(PortError):
    """Optimistic-concurrency or state conflict, e.g. a lost claim race."""


class RateLimited(PortError):
    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class Unavailable(PortError):
    """Timeouts, connection failures, 5xx. Safe to retry with backoff."""


class InvalidRequest(PortError):
    """Malformed input or a request the adapter cannot express."""


class DivergedClone(PortError):
    """Existing clone cannot fast-forward. Caller re-clones or fails."""


class SubmoduleFailure(PortError):
    def __init__(self, message: str, path: str) -> None:
        super().__init__(message)
        self.path = path


class AccessDenied(PortError):
    """Raised by tools when the AccessGuard denies a read. Carries paths only."""


class BudgetExceeded(PortError):
    pass
