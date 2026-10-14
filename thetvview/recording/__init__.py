"""Grabación manual (Fase 6)."""

from .backend import *  # noqa: F401,F403

__all__ = []
try:
    from .backend import __all__ as _a
    __all__ = list(_a)
except Exception:
    pass
