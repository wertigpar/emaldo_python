"""Emaldo battery system API client.

Python library for interacting with Emaldo home battery systems.
Provides both a programmatic API and a command-line interface.
"""

from emaldo.client import EmaldoClient
from emaldo.e2e import PersistentE2ESession
from emaldo.exceptions import EmaldoError, EmaldoAuthError, EmaldoAPIError, EmaldoConnectionError

__version__ = "0.1.0"
__all__ = [
    "EmaldoClient",
    "PersistentE2ESession",
    "EmaldoError",
    "EmaldoAuthError",
    "EmaldoAPIError",
    "EmaldoConnectionError",
]
