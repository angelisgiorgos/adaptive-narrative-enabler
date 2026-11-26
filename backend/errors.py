"""Errors raised by the backend; the UI and the API turn them into messages."""


class AuthoringError(ValueError):
    """An invalid request: the message explains what to fix. Nothing was changed."""


class ConflictError(AuthoringError):
    """The data changed in a way the request did not expect (a name taken again, an entry gone, an action
    that is not the one asked for): nothing was changed, and nothing newer was overwritten."""


class StaleStateError(ConflictError):
    """A choice made from the options of an older state of the game (``state_version``): the game has
    moved since (another tab, a UNP update). Nothing was done; show the current options."""

    code = "stale_state"

    def __init__(self, message, state_version, offered=None):
        super().__init__(message)
        self.state_version = state_version
        self.offered = list(offered or [])


class ActionUnavailableError(ConflictError):
    """A choice (``action_id``) that is not among the options offered now. Nothing was done."""

    code = "action_unavailable"

    def __init__(self, message, state_version, offered=None):
        super().__init__(message)
        self.state_version = state_version
        self.offered = list(offered or [])


class SessionUnrecoverableError(ConflictError):
    """A play session that is saved but cannot be restored here (its save or its configuration is
    missing or damaged, or was made for something this server cannot rebuild). Nothing was changed."""

    code = "session_unrecoverable"

    def __init__(self, message, session_id=None):
        super().__init__(message)
        self.session_id = session_id
