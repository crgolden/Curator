"""Exceptions raised by the ``curator.psn`` client, ported from ``psnpy``'s ``psn_api.py``/``safety.py``."""

from __future__ import annotations


class PsnAuthError(Exception):
    """Raised when PSN authentication fails: no usable token/npsso, or an expired/invalid npsso cookie."""


class MutationNotAllowedError(Exception):
    """Raised when a mutating PSN operation (send message, create/rename group, friend request, ...) is
    attempted against an account that is not the pinned test account (see :mod:`curator.psn.safety`)."""
