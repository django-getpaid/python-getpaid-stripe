"""Stripe simulator plugin for python-getpaid-simulator.

``get_plugin`` is exported lazily so that dependency-light modules
(e.g. :mod:`getpaid_stripe.simulator.signing`) stay importable without
the ``simulator`` extra (litestar, python-getpaid-simulator).
"""

from typing import Any


__all__ = ["get_plugin"]


def __getattr__(name: str) -> Any:
    if name == "get_plugin":
        from getpaid_stripe.simulator.plugin import get_plugin

        return get_plugin
    raise AttributeError(name)
