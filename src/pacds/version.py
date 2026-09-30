"""The installed PACDS version (one source: the distribution metadata)."""

from importlib.metadata import PackageNotFoundError, version


def package_version() -> str:
    """The version of the `pacds` distribution, or "unknown" when it is not installed (a bare source tree)."""
    try:
        return version("pacds")
    except PackageNotFoundError:
        return "unknown"
