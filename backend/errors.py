"""Errors raised by the backend; the UI and the API turn them into messages."""


class AuthoringError(ValueError):
    """An invalid request: the message explains what to fix. Nothing was changed."""
