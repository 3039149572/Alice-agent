class DecisionError(RuntimeError):
    """Safe base exception that must never include credentials or response bodies."""


class DecisionTimeoutError(DecisionError):
    pass


class DecisionAPIError(DecisionError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DecisionValidationError(DecisionError):
    pass


def failure_details(error: Exception) -> dict:
    """A finite diagnostic projection; never include exception text or bodies."""
    status = getattr(error, "status_code", None)
    status = status if isinstance(status, int) and 100 <= status <= 599 else None
    if isinstance(error, (DecisionTimeoutError, TimeoutError)):
        reason = "timeout"
    elif isinstance(error, DecisionValidationError):
        reason = "invalid_response"
    elif status in {401, 403}:
        reason = "authentication"
    elif status == 429:
        reason = "rate_limit"
    elif status is not None and status >= 500:
        reason = "upstream_error"
    elif status is not None:
        reason = "http_error"
    elif isinstance(error, DecisionAPIError):
        reason = "connection_error"
    else:
        reason = "internal_error"
    return {"error_kind": type(error).__name__, "failure_reason": reason, "http_status": status}
