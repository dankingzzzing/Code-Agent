"""Errors that interfaces can display without a Python traceback."""


class AgentError(Exception):
    """Base application error."""


class ConfigError(AgentError):
    """Invalid or missing configuration."""


class ProviderError(AgentError):
    """An upstream request or response failed."""


class ToolError(AgentError):
    """A tool request failed validation or execution."""


class MemoryError(AgentError):
    """A persisted session is invalid or cannot be accessed."""
