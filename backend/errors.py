"""Errors raised by the backend; the UI and the API turn them into messages."""


class AuthoringError(ValueError):
    """An invalid request: the message explains what to fix. Nothing was changed."""


class ConflictError(AuthoringError):
    """The data changed in a way the request did not expect (a name taken again, an entry gone, an action
    that is not the one asked for): nothing was changed, and nothing newer was overwritten."""
