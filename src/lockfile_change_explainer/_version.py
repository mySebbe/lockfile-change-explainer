"""Version helpers for lockfile-change-explainer."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

_DISTRIBUTION_NAME = "lockfile-change-explainer"

try:
    __version__ = _distribution_version(_DISTRIBUTION_NAME)
except PackageNotFoundError:
    __version__ = "0.1.0"
