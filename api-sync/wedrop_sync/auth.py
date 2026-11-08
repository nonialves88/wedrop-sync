"""Autenticação: login, leitura do `exp` do JWT e renovação proativa.

Diferente da versão anterior (que assumia "vale ~4h e renova quando expira"),
aqui a expiração é lida do próprio token e a renovação acontece com margem —
assim nunca perdemos uma requisição no meio de uma rodada longa (as rodadas
reais passam de 9h, e o token tem validade curta).
"""
from __future__ import annotations

import base64
import binascii
import json
import threading
import time
from typing import Any, Optional

from .config import Credentials
from .classify import normalize


class AuthError(RuntimeError):
    """Falha de autenticação. `kind` guia a política de alerta."""

    def __init__(self, message: str, kind: str = "auth", status: int = 0) -> None:
        super().__init__(message)
        self.kind = kind  # invalid_credentials | email_code | network | bad_response | auth
        self.status = status

    @property
    def needs_human(self) -> bool:
        """Precisa de gente: senha trocada, código por e-mail, conta bloqueada."""
        return self.kind in ("invalid_credentials", "email_code", "bad_response")


_EMAIL_CODE_MARKERS = (
    "codigo",
    "code",
    "verificacao",
    "verification",
    "2fa",
    "two-factor",
    "autenticacao em duas etapas",
    "email",
    "confirmar",
)

_BAD_CREDENTIAL_MARKERS = (
    "invalid",
    "invalida",
    "invalido",
    "incorreta",
    "incorreto",
    "nao encontrado",
    "unauthorized",
    "forbidden",
    "senha",
    "password",
    "credenciais",
)


def jwt_expiry(token: str) -> Optional[int]:
    """Lê `exp` (epoch) do payload do JWT, sem validar assinatura."""
    try:
        payload_b64 = token.split(".")[1]
    except (AttributeError, IndexError):
        return None
    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        return int(exp)
    return None


def _extract_token(body: Any) -> Optional[str]:
    if not isinstance(body, dict):
        return None
    for key in ("access_token", "accessToken", "token", "jwt"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested_key in ("data", "result", "auth", "payload"):
        nested = body.get(nested_key)
        token = _extract_token(nested)
        if token:
            return token
    return None


class TokenProvider:
    """Guarda o token em memória (nunca em disco) e renova sozinho.

    - Renova `margin` segundos antes do `exp` do JWT.
    - Se o `exp` não for legível, usa uma validade conservadora configurável.
    - `invalidate()` força nova autenticação no próximo uso (após um 401).
    """

    def __init__(
        self,
        transport,
        credentials: Credentials,
        *,
        margin: float = 300.0,
        assume_ttl: float = 3600.0,
        max_extra: Optional[int] = None,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._margin = margin
        self._assume_ttl = assume_ttl
        self._max_extra = max_extra
        self._token: Optional[str] = None
        self._expires_at: Optional[float] = None
        self._lock = threading.RLock()
        self.logins = 0  # telemetria: quantos logins nesta rodada

    # ------------------------------------------------------------------ #
    def token(self) -> str:
        with self._lock:
            if self._token and not self._is_expiring():
                return self._token
            return self._login()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = None

    @property
    def expiration(self) -> Optional[float]:
        with self._lock:
            return self._expires_at

    # ------------------------------------------------------------------ #
    def _is_expiring(self) -> bool:
        if not self._token:
            return True
        if self._expires_at is None:
            return False
        return time.time() >= (self._expires_at - self._margin)

    def _login(self) -> str:
        body = {"email": self._credentials.email, "password": self._credentials.password}
        try:
            response = self._transport.request(
                "POST",
                "/auth",
                json_body=body,
                attempts=2,
                retry_5xx=True,
            )
        except Exception as exc:  # rede, DNS, TLS, timeout
            raise AuthError(f"não consegui falar com a API no login: {type(exc).__name__}: {exc}", "network") from exc

        payload = response.data if response.data is not None else {}
        token = _extract_token(payload)
        if response.status == 200 and token:
            self._token = token
            exp = jwt_expiry(token)
            self._expires_at = float(exp) if exp else (time.time() + self._assume_ttl)
            self.logins += 1
            return token

        raise self._diagnose(response.status, payload)

    def _diagnose(self, status: int, payload: Any) -> AuthError:
        text = normalize(json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else payload)
        if any(marker in text for marker in ("codigo", "verification", "2fa", "two-factor")):
            return AuthError(
                "o WeDrop pediu código de verificação por e-mail — precisa de login manual",
                "email_code",
                status,
            )
        if status in (401, 403) or any(marker in text for marker in _BAD_CREDENTIAL_MARKERS):
            return AuthError(f"login recusado (HTTP {status}): e-mail ou senha inválidos", "invalid_credentials", status)
        if status == 429:
            return AuthError("limite de requisições no login (HTTP 429)", "rate_limit", status)
        return AuthError(f"resposta inesperada no login (HTTP {status})", "bad_response", status)
