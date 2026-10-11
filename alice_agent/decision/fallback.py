class DecisionError(RuntimeError):
    """Safe base exception that must never include credentials or response bodies."""


class DecisionTimeoutError(DecisionError):
    pass


class DecisionAPIError(DecisionError):
    pass


class DecisionValidationError(DecisionError):
    pass
