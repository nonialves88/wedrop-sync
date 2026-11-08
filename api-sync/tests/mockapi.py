"""API WeDrop falsa: servidor HTTP real, em memória.

Simula o que importa para a rodada: login com JWT, paginação de `/my-items`
com `lowStock`/`outdatedStock`, e `/my-items/update-stock` com os formatos de
resposta que o painel devolve (inclusive os "200 que são falha"), além de 429,
5xx, timeout e token recusado.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PARA = "perdeu a autorização"
ORPHAN = "O canal não encontrou este anúncio (orphan)"


def make_token(ttl: int = 3600, user_id: int = 10000) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    header = b64({"alg": "HS256", "typ": "JWT"})
    payload = b64({"userId": user_id, "exp": int(time.time()) + int(ttl), "iat": int(time.time())})
    return f"{header}.{payload}.assinatura-de-teste"


def default_items() -> dict:
    """Um catálogo pequeno e determinístico por canal."""

    def build(prefix: str, total: int, low: int, outdated: int) -> list:
        items = []
        for index in range(total):
            stock = 3 if index < low else 50
            items.append(
                {
                    "id": f"{prefix}-{index:03d}",
                    "title": f"Produto {prefix} {index}",
                    "sku": f"SKU-{prefix}-{index}",
                    "stock": stock,
                    "situation": "mapped",
                    "outdatedStock": index >= (low - outdated) and index < low,
                }
            )
        return items

    return {
        10001: build("shp", 12, 6, 3),
        10002: build("ml1", 8, 4, 2),
        10003: build("ml2", 10, 5, 0),
        10004: build("bng", 3, 1, 0),
    }


PAYLOADS = {
    "ok": {"success": True},
    "partial": {"success": False, "partialSuccess": True},
    "skip": {"success": False, "skipped": True},
    "orphan": {"success": False, "errors": [{"message": ORPHAN}]},
    "under_review": {"success": False, "errors": [{"message": "anúncio under_review"}]},
    "auth_loss": {"success": False, "errors": [{"message": f"A conexão com esta loja {PARA}"}]},
    "model": {"success": False, "errors": [{"message": "model_id is mandatory for variations"}]},
    "plain_error": {"success": False, "errors": "falha genérica do marketplace"},
}


class FakeWeDrop:
    def __init__(
        self,
        *,
        items: dict | None = None,
        token_ttl: int = 3600,
        retry_after: int = 1,
    ) -> None:
        self.lock = threading.Lock()
        self.items_by_channel = default_items() if items is None else items
        self.token_ttl = token_ttl
        self.retry_after = retry_after
        self.behaviors: dict = {}
        self.reject_login = False
        self.email_code = False
        self.update_delay = 0.0
        self.timeout_sleep = 0.6
        self.auth_calls = 0
        self.update_calls = 0
        self.item_calls: dict = {}
        self.paths: list = []
        self.rate_limit_queue: list = []
        self.integrations = [
            (10001, "Shopee"),
            (10002, "LOJA-EXEMPLO - ML1"),
            (10003, "LOJA-EXEMPLO - ML2"),
            (10004, "Bling"),
        ]
        self.reject_once: dict = {}  # path -> quantidade de 401 a emitir antes de voltar ao normal
        self.fail_read_queue: dict = {}  # path -> lista de status a emitir
        self.server: ThreadingHTTPServer | None = None
        self.base_url = ""

    # ------------------------------------------------------------------ #
    def behavior(self, item_id, kind: str) -> None:
        self.behaviors[str(item_id)] = kind

    def payload_for(self, kind: str) -> dict:
        return dict(PAYLOADS.get(kind, PAYLOADS["ok"]))

    def start(self) -> str:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self.base_url

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    # ------------------------------------------------------------------ #
    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silêncio nos testes
                pass

            def handle_one_request(self):
                # cliente que dá timeout/desiste no meio: não é erro do teste
                try:
                    super().handle_one_request()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
                    self.close_connection = True

            # ---- infraestrutura ----
            def _send(self, status: int, payload=None, headers=None) -> None:
                body = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw)
                except ValueError:
                    return {}

            def _take_reject_once(self, path: str) -> bool:
                with fake.lock:
                    remaining = fake.reject_once.get(path, 0)
                    if remaining:
                        fake.reject_once[path] = remaining - 1
                        return True
                return False

            # ---- GET ----
            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                path = url.path.rstrip("/") or "/"
                params = {key: values[0] for key, values in parse_qs(url.query).items()}
                fake.paths.append(("GET", path, params))

                if self._take_reject_once(path):
                    return self._send(401, {"message": "token inválido"})

                fail = fake.fail_read_queue.get(path)
                if fail:
                    return self._send(fail.pop(0), {"error": "falha injetada"})

                if path.endswith("/me"):
                    return self._send(200, {"userId": 10000, "name": "Usuário Demo"})
                if path.endswith("/my-integrations"):
                    entries = [
                        {"integrationId": channel, "name": name, "status": "active"}
                        for channel, name in fake.integrations
                    ]
                    return self._send(200, {"items": entries})
                if path.endswith("/my-items"):
                    return self._my_items(params)
                return self._send(404, {"error": "rota desconhecida"})

            def _my_items(self, params: dict):
                try:
                    integration_id = int(params.get("integrationId") or 0)
                except (TypeError, ValueError):
                    return self._send(400, {"error": "integrationId inválido"})
                try:
                    limit = int(params.get("limit") or 50)
                    offset = int(params.get("offset") or 0)
                except (TypeError, ValueError):
                    limit, offset = 50, 0

                items = list(fake.items_by_channel.get(integration_id, []))
                if (params.get("situation") or "mapped") != "mapped":
                    items = []
                if params.get("lowStock") in ("true", "1"):
                    items = [item for item in items if item.get("stock", 0) <= 10]
                if params.get("outdatedStock") in ("true", "1"):
                    items = [item for item in items if item.get("outdatedStock")]
                total = len(items)
                page = items[offset : offset + limit]
                return self._send(200, {"items": page, "total": total})

            # ---- POST ----
            def do_POST(self):  # noqa: N802
                url = urlparse(self.path)
                path = url.path.rstrip("/") or "/"
                fake.paths.append(("POST", path, self._json_body()))
                body = fake.paths[-1][2]

                if path.endswith("/auth"):
                    with fake.lock:
                        fake.auth_calls += 1
                    if fake.reject_login:
                        return self._send(401, {"message": "usuário ou senha inválidos"})
                    if fake.email_code:
                        return self._send(
                            200,
                            {"requiresCode": True, "message": "enviamos um código de verificação por e-mail"},
                        )
                    return self._send(200, {"access_token": make_token(fake.token_ttl)})

                if path.endswith("/update-stock"):
                    return self._update_stock(body)

                return self._send(404, {"error": "rota desconhecida"})

            def _update_stock(self, body: dict):
                item_id = str(body.get("itemId"))
                integration_id = int(body.get("integrationId") or 0)
                with fake.lock:
                    fake.update_calls += 1
                    fake.item_calls[item_id] = fake.item_calls.get(item_id, 0) + 1
                    kind = fake.behaviors.get(item_id, "ok")
                    if fake.rate_limit_queue:
                        kind = fake.rate_limit_queue.pop(0)
                    if kind in ("ok", "partial"):
                        # como no painel real, sucesso tira o anúncio de "desatualizado"
                        for item in fake.items_by_channel.get(integration_id, []):
                            if str(item.get("id")) == item_id:
                                item["outdatedStock"] = False

                if fake.update_delay:
                    time.sleep(fake.update_delay)
                if kind == "http429":
                    return self._send(
                        429, {"error": "too many requests"}, {"Retry-After": str(fake.retry_after)}
                    )
                if kind == "http500":
                    return self._send(500, {"error": "falha interna"})
                if kind == "timeout":
                    time.sleep(fake.timeout_sleep)
                    return self._send(200, {"success": True})
                return self._send(200, fake.payload_for(kind))

        return Handler
