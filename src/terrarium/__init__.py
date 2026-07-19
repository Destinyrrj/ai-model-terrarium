"""AI Model Terrarium.

The package keeps model inference outside world mechanics.  Agent output is
untrusted input and may only cross into the world through strict domain models.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ai-model-terrarium")
except PackageNotFoundError:  # source checkout
    __version__ = "0.1.0"

__all__ = ["__version__"]
