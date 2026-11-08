"""Transporte HTTP e cliente da API WeDrop.

Sem dependência externa: `http.client` + `ssl` + `socket` da stdlib, com
conexão reutilizada por thread (keep-alive), timeouts separados de conexão e
leitura (10s / 30s, como a versão anterior já usava) e política de retry:

* leituras (GET): repete até `read_attempts` vezes, inclusive em 5xx;
* escritas (POST update-stock): repete em erro de rede/timeout até
  `update_attempts` vezes, mas **não** repete 5xx (decisão do histórico: se o
  servidor quebrou, seguir para o próximo anúncio é mais eficiente);
* 429: respeita o `Retry-After` (padrão 60s) e tenta de novo, com teto.
"""
from __future__ import annotations

import http.client
import json
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Optional
from urllib.parse import urlencode, urlsplit

from .auth import AuthError, TokenProvider

USER_AGENT = "wedrop-sync/2.0 (api; +https://dash.wedrop.com.br)"


class TransportError(RuntimeError):
    """Falha de rede/timeout que esgotou as tentativas."""


class RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"429: aguardar {retry_after:.0f}s")
        self.retry_after = retry_after


@dataclass
class Response:
    status: int
    data: Any = None
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


def _parse_retry_after(headers: Mapping[str, str], default: float) -> float:
    raw = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value
            break
    if not raw:
        return default
    raw = str(raw).strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        return max(1.0, when.timestamp() - time.time())
    except (TypeError, ValueError):
        return default


class HttpTransport:
    """Uma conexão por thread, reaproveitada; threads novas abrem a sua."""

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        backoff: float = 2.0,
        default_retry_after: float = 60.0,
        user_agent: str = USER_AGENT,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"api_base inválida: {base_url!r}")
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.base_path = parts.path.rstrip("/")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.backoff = backoff
        self.default_retry_after = default_retry_after
        self.user_agent = user_agent
        self._local = threading.local()
        self._count_lock = threading.Lock()
        self.requests_made = 0
        self.retries_made = 0
        self.rate_limit_hits = 0

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def _build_path(self, path: str, params: Optional[Mapping[str, Any]]) -> str:
        url = f"{self.base_path}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += ("&" if "?" in url else "?") + urlencode(clean)
        return url

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        if self.scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(self.read_timeout)
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.read_timeout)
        conn.sock = sock
        return conn

    def _connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _reset(self) -> None:
        self.close()

    def _sleep(self, attempt: int) -> None:
        with self._count_lock:
            self.retries_made += 1
        time.sleep(self.backoff * attempt + random.uniform(0, 0.25))

    # ------------------------------------------------------------------ #
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        attempts: int = 1,
        retry_5xx: bool = True,
        retry_network: bool = True,
    ) -> Response:
        url = self._build_path(path, params)
        body = None
        hdrs = {"Accept": "application/json", "User-Agent": self.user_agent}
        if headers:
            hdrs.update(headers)
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json; charset=utf-8"

        attempts = max(1, int(attempts))
        last_error: Optional[BaseException] = None

        for attempt in range(1, attempts + 1):
            conn = self._connection()
            try:
                with self._count_lock:
                    self.requests_made += 1
                conn.request(method, url, body=body, headers=hdrs)
                raw = conn.getresponse()
                status = int(raw.status)
                payload = raw.read()
                resp_headers = {k: v for k, v in raw.getheaders()}
                data = self._decode(payload)

                if status == 429:
                    with self._count_lock:
                        self.rate_limit_hits += 1
                    raise RateLimited(_parse_retry_after(resp_headers, self.default_retry_after))
                if status >= 500 and retry_5xx and attempt < attempts:
                    self._reset()
                    self._sleep(attempt)
                    continue
                return Response(status=status, data=data, text=payload.decode("utf-8", "replace"), headers=resp_headers)
            except RateLimited:
                self._reset()
                raise
            except (socket.timeout, TimeoutError) as exc:
                self._reset()
                last_error = exc
                if attempt >= attempts or not retry_network:
                    raise TransportError(f"tempo esgotado em {method} {path}") from exc
                self._sleep(attempt)
            except (OSError, http.client.HTTPException) as exc:
                self._reset()
                last_error = exc
                if attempt >= attempts or not retry_network:
                    raise TransportError(f"falha de rede em {method} {path}: {type(exc).__name__}") from exc
                self._sleep(attempt)

        raise TransportError(f"falha em {method} {path}: {last_error}")

    @staticmethod
    def _decode(payload: bytes) -> Any:
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return None


class ApiClient:
    """Cliente autenticado: injeta o token, renova em 401 e respeita o 429."""

    def __init__(
        self,
        transport: HttpTransport,
        tokens: TokenProvider,
        *,
        read_attempts: int = 1,
        write_attempts: int = 1,
        max_rate_waits: int = 3,
        on_event=None,
    ) -> None:
        self.transport = transport
        self.tokens = tokens
        self.read_attempts = max(1, int(read_attempts))
        self.write_attempts = max(1, int(write_attempts))
        self.max_rate_waits = max_rate_waits
        self._on_event = on_event or (lambda event, **fields: None)
        self.rate_waits = 0

    # ------------------------------------------------------------------ #
    def get(self, path: str, params: Optional[Mapping[str, Any]] = None, *, attempts: Optional[int] = None) -> Response:
        return self._send(
            "GET",
            path,
            params=params,
            body=None,
            attempts=attempts or self.read_attempts,
            retry_5xx=True,
        )

    def post(self, path: str, body: Any, *, attempts: Optional[int] = None, retry_5xx: bool = False) -> Response:
        return self._send(
            "POST",
            path,
            params=None,
            body=body,
            attempts=attempts or self.write_attempts,
            retry_5xx=retry_5xx,
        )

    # ------------------------------------------------------------------ #
    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]],
        body: Any,
        attempts: Optional[int],
        retry_5xx: bool,
    ) -> Response:
        auth_retried = False
        rate_waits = 0
        while True:
            try:
                token = self.tokens.token()
            except AuthError:
                raise
            headers = {"Authorization": f"Bearer {token}"}
            try:
                response = self.transport.request(
                    method,
                    path,
                    params=params,
                    json_body=body,
                    headers=headers,
                    attempts=attempts,
                    retry_5xx=retry_5xx,
                )
            except RateLimited as limited:
                if rate_waits >= self.max_rate_waits:
                    raise
                rate_waits += 1
                self.rate_waits += 1
                wait = limited.retry_after
                self._on_event("rate_limited", path=path, seconds=round(wait), attempt=rate_waits)
                time.sleep(wait)
                continue

            if response.status in (401, 403) and not auth_retried:
                auth_retried = True
                self._on_event("token_rejected", path=path, status=response.status)
                self.tokens.invalidate()
                continue
            return response
