"""project-sandbox package."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("project-sandbox")
except PackageNotFoundError:
    __version__ = "unknown"
