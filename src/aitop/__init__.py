from importlib.metadata import PackageNotFoundError, version

# Single source of truth is pyproject.toml's `version`; this reads it back
# from the installed distribution so it can't drift from the package metadata.
# The "0.0.0" fallback is a sentinel for "unknown", only hit when running
# straight from a source tree that was never installed -- not a second copy.
try:
    __version__ = version("aitop")
except PackageNotFoundError:
    __version__ = "0.0.0"
