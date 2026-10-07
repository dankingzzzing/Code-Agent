"""Errors that interfaces can display without a Python traceback."""


class AgentError(Exception):
    """Base application error."""


class ConfigError(AgentError):
    """Invalid or missing configuration."""


class ProviderError(AgentError):
    """An upstream request or response failed."""

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class ToolError(AgentError):
    """A tool request failed validation or execution."""


class MemoryError(AgentError):
    """A persisted session is invalid or cannot be accessed."""
