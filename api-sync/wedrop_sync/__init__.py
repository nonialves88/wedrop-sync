"""Sincronizador de estoque WeDrop (API v3, sem navegador).

Uso rápido:
    python -m wedrop_sync check       # login + integrações
    python -m wedrop_sync --dry-run   # monta a fila sem enviar
    python -m wedrop_sync             # rodada normal (Shopee → ML1 → ML2)

A senha nunca é impressa: `Credentials` mascara no `repr`, e nada aqui grava
token ou credencial em disco.
"""

__version__ = "2.0.0"

__all__ = [
    "__version__",
    "ApiClient",
    "AuthError",
    "Channel",
    "Credentials",
    "HttpTransport",
    "RunLock",
    "Runner",
    "Settings",
    "load_credentials",
    "load_settings",
]

from .auth import AuthError  # noqa: E402
from .client import ApiClient, HttpTransport  # noqa: E402
from .config import Channel, Credentials, Settings, load_credentials, load_settings  # noqa: E402
from .lock import RunLock  # noqa: E402
from .runner import Runner  # noqa: E402
