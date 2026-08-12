"""Exception types with messages meant for end users, not stack traces."""


class GuardError(Exception):
    """Base class. The CLI prints str(err) and exits 2 rather than traceback."""


class GitError(GuardError):
    """A git subprocess failed."""


class NetworkError(GitError):
    """Clone or fetch could not reach github.com."""


class RefError(GuardError):
    """A ref does not exist in the local clone."""


class BadInputError(GuardError):
    """Caller supplied a path or symbol that cannot be used."""
