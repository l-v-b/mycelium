"""mycelium — a unified memory system for AI agents (drawers, notes, links) over MCP."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mycelium-palace")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0+unknown"
