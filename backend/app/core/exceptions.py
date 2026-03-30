"""
VLCR custom exception hierarchy.

All exceptions carry:
  - status_code : HTTP status code to return
  - detail      : human-readable error message
  - code        : machine-readable error code string
"""


class VLCRException(Exception):
    """Base exception for all VLCR application errors."""

    status_code: int = 500
    detail: str = "An unexpected error occurred."
    code: str = "internal_error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        if detail is not None:
            self.detail = detail
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        super().__init__(self.detail)


class Unauthorized(VLCRException):
    """401 — missing or invalid JWT."""

    status_code = 401
    detail = "Authentication credentials are missing or invalid."
    code = "unauthorized"


class Forbidden(VLCRException):
    """403 — authenticated but insufficient role."""

    status_code = 403
    detail = "You do not have permission to perform this action."
    code = "forbidden"


class ComplaintNotFound(VLCRException):
    """404 — reference number not found."""

    status_code = 404
    detail = "Complaint not found."
    code = "complaint_not_found"


class RateLimitExceeded(VLCRException):
    """429 — IP rate limit or daily complaint limit hit."""

    status_code = 429
    detail = "Rate limit exceeded. Please try again later."
    code = "rate_limit_exceeded"


class ClassificationFailed(VLCRException):
    """502 — Claude API error or non-JSON response."""

    status_code = 502
    detail = "Complaint classification failed due to an upstream AI service error."
    code = "classification_failed"
