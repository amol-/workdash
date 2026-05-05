"""workdash package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("workdash")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

from .workdash import main

__all__ = ["main", "__version__"]
