"""Exception hierarchy for the XingShu BCI SDK."""

from __future__ import annotations

from typing import Any, Optional


class XingShuError(Exception):
    """Base exception for all XingShu BCI SDK errors."""

    def __init__(self, message: str, *, code: str = "ERROR", status: Optional[int] = None,
                 details: Optional[Any] = None, request_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details
        self.request_id = request_id
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover
        return f"XingShuError(code={self.code!r}, status={self.status!r}, message={self.message!r})"


class XingShuAuthError(XingShuError):
    """Raised when a 401 Unauthorized is returned (missing/invalid token)."""


class XingShuForbiddenError(XingShuError):
    """Raised when a 403 Forbidden is returned (insufficient scope)."""


class XingShuNotFoundError(XingShuError):
    """Raised when a 404 Not Found is returned."""


class XingShuConnectionError(XingShuError):
    """Raised when the local API service cannot be reached."""


class XingShuTimeoutError(XingShuConnectionError):
    """Raised when a request times out (client-side) or returns 408.

    Subclasses :class:`XingShuConnectionError` so existing handlers that
    catch connection problems keep catching timeouts.
    """


class XingShuRateLimitError(XingShuError):
    """Raised when the API returns 429 Too Many Requests."""
