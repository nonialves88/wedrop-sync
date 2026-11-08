#!/usr/bin/env python3
"""Aviso por WhatsApp (Z-API). Lê a mensagem do stdin (ou argv) e envia.

Plugado em `WEDROP_NOTIFY_COMMAND`. Configuração só por variáveis de ambiente
(secrets do GitHub): ZAPI_INSTANCE, ZAPI_TOKEN, ZAPI_CLIENT_TOKEN (opcional) e
ALERT_PHONE. Nunca imprime credencial; sempre sai com 0 para não derrubar a rodada.
"""
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    inst, token, phone = (os.environ.get(k, "").strip() for k in ("ZAPI_INSTANCE", "ZAPI_TOKEN", "ALERT_PHONE"))
    text = " ".join(sys.argv[1:]) or sys.stdin.read()
    text = text.strip()[:1500]
    if not (inst and token and phone and text):
        print("zapi: não configurado ou mensagem vazia; nada enviado")
        return 0
    headers = {"Content-Type": "application/json"}
    if os.environ.get("ZAPI_CLIENT_TOKEN"):
        headers["Client-Token"] = os.environ["ZAPI_CLIENT_TOKEN"].strip()
    request = urllib.request.Request(
        f"https://api.z-api.io/instances/{inst}/token/{token}/send-text",
        data=json.dumps({"phone": phone, "message": text}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            print(f"zapi: HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        print(f"zapi: falhou HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"zapi: falhou ({type(exc).__name__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
