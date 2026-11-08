#!/usr/bin/env python3
"""Reconhecimento (somente leitura) da API do WeDrop.

Aplica a metodologia do `reverse-api-engineer` (kalil0321): descobrir os
endpoints que o próprio front-end usa, como a autenticação funciona e qual é o
formato real das respostas — e transformar isso em código que roda sozinho.

Este script **não altera nada**: só faz POST /auth (para pegar o token) e GETs.
A senha nunca é impressa, nunca vai para arquivo e não aparece em log.

    python scripts/probe_api.py            # usa ~/.wedrop/creds.env
    python scripts/probe_api.py --json > recon.json

Serve para confirmar, contra a conta real:
  * se o login por e-mail/senha funciona (e por quanto tempo o token vale);
  * os ids reais das integrações;
  * o envelope do /my-items (`total` + `results`?) e os campos de cada anúncio;
  * se os filtros `lowStock` / `outdatedStock` são aplicados pelo servidor;
  * como o /internal/rate-limit responde.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from wedrop_sync.auth import TokenProvider, jwt_expiry  # noqa: E402
from wedrop_sync.client import ApiClient, HttpTransport  # noqa: E402
from wedrop_sync.config import CHANNELS, load_credentials, mask_email  # noqa: E402

STATIC_KEYS = {"", "integrationId", "userId", "situation", "limit", "offset"}


def shape(value, depth=0, max_depth=3):
    """Descreve o formato de um JSON sem mostrar os valores (privacidade)."""
    if depth >= max_depth:
        return type(value).__name__
    if isinstance(value, dict):
        return {key: shape(val, depth + 1, max_depth) for key, val in list(value.items())[:40]}
    if isinstance(value, list):
        return [shape(value[0], depth + 1, max_depth)] if value else []
    return type(value).__name__


def keys_of(payload):
    if isinstance(payload, dict):
        return sorted(payload.keys())
    if isinstance(payload, list):
        return ["<lista>"]
    return [type(payload).__name__]


def probe(env_file: Path, api_base: str | None) -> dict:
    creds = load_credentials(env_file)
    report: dict = {
        "credenciais": {"email": mask_email(creds.email), "origem": creds.source},
        "api_base": api_base or "https://api.wedrop.com.br/v3/api",
        "etapas": {},
    }

    transport = HttpTransport(
        report["api_base"], connect_timeout=15.0, read_timeout=60.0, default_retry_after=20.0
    )
    tokens = TokenProvider(transport, creds)
    api = ApiClient(transport, tokens, read_attempts=2, write_attempts=1)

    try:
        # 1) login -------------------------------------------------------
        try:
            token = tokens.token()
            exp = jwt_expiry(token)
            report["etapas"]["login"] = {
                "ok": True,
                "token_len": len(token),
                "exp_epoch": exp,
                "exp_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else None,
                "minutos_de_validade": round((exp - time.time()) / 60, 1) if exp else None,
                "formato_jwt": token.count(".") == 2,
            }
        except Exception as exc:  # noqa: BLE001
            report["etapas"]["login"] = {"ok": False, "erro": f"{type(exc).__name__}: {exc}"}
            return report

        # 2) /me ---------------------------------------------------------
        me = api.get("/me")
        report["etapas"]["me"] = {"status": me.status, "chaves": keys_of(me.data), "shape": shape(me.data)}
        user_id = None
        if isinstance(me.data, dict):
            for key in ("userId", "user_id", "id"):
                if isinstance(me.data.get(key), (int, str)):
                    user_id = int(me.data[key])
                    break
        report["user_id"] = user_id

        # 3) /my-integrations -------------------------------------------
        integrations = api.get("/my-integrations")
        report["etapas"]["my-integrations"] = {
            "status": integrations.status,
            "chaves": keys_of(integrations.data),
            "shape": shape(integrations.data),
        }
        found = {}
        payload = integrations.data
        entries = payload
        if isinstance(payload, dict):
            for key in ("items", "results", "data", "integrations"):
                if isinstance(payload.get(key), list):
                    entries = payload[key]
                    break
            else:
                entries = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                ident = entry.get("integrationId") or entry.get("id")
                if ident is None:
                    continue
                found[str(ident)] = {
                    "name": entry.get("name") or entry.get("integrationName"),
                    "status": entry.get("status") or entry.get("situation"),
                    "marketplace": entry.get("marketplace") or entry.get("integration") or entry.get("type"),
                }
        report["integracoes"] = found

        # 4) /my-items por canal ----------------------------------------
        canais = {}
        for channel in CHANNELS.values():
            entry: dict = {"nome": channel.name, "integrationId": channel.integration_id}
            started = time.time()
            small = api.get(
                "/my-items",
                params={
                    "integrationId": channel.integration_id,
                    "userId": user_id,
                    "situation": "mapped",
                    "limit": 2,
                    "offset": 0,
                },
            )
            entry["status"] = small.status
            entry["envelope"] = keys_of(small.data)
            results = None
            if isinstance(small.data, dict):
                for key in ("results", "items", "data"):
                    if isinstance(small.data.get(key), list):
                        results = small.data[key]
                        entry["campo_da_lista"] = key
                        entry["total_informado"] = small.data.get("total")
                        break
            elif isinstance(small.data, list):
                results = small.data
                entry["campo_da_lista"] = "<raiz>"

            if results:
                item = results[0]
                entry["campos_do_item"] = sorted(item.keys())
                entry["shape_do_item"] = shape(item)
                products = item.get("itemsProducts") or item.get("items_products") or []
                if products and isinstance(products[0], dict):
                    entry["campos_itemsProducts"] = sorted(products[0].keys())
                    entry["shape_itemsProducts"] = shape(products[0])
                    produto = products[0].get("product")
                    if isinstance(produto, dict):
                        entry["campos_product"] = sorted(produto.keys())
                        entry["shape_product"] = shape(produto)

            # contagem total (uma chamada grande, como no front-end)
            big = api.get(
                "/my-items",
                params={
                    "integrationId": channel.integration_id,
                    "userId": user_id,
                    "situation": "mapped",
                    "limit": 999999,
                    "offset": 0,
                },
            )
            lista = []
            if isinstance(big.data, dict):
                for key in ("results", "items", "data"):
                    if isinstance(big.data.get(key), list):
                        lista = big.data[key]
                        break
            elif isinstance(big.data, list):
                lista = big.data
            entry["catalogo_mapeado"] = len(lista)
            entry["total_no_corpo"] = big.data.get("total") if isinstance(big.data, dict) else None
            entry["segundos_para_listar"] = round(time.time() - started, 2)
            if lista:
                outdated = sum(1 for it in lista if it.get("outdatedStock"))
                baixo = 0
                for it in lista:
                    products = it.get("itemsProducts") or []
                    if products and isinstance(products[0], dict):
                        qty = products[0].get("availableQuantity")
                        if isinstance(qty, (int, float)) and qty <= 10:
                            baixo += 1
                entry["com_outdatedStock"] = outdated
                entry["com_availableQuantity<=10"] = baixo

            # 5) os filtros são do servidor? ----------------------------
            for nome, params in (
                ("lowStock", {"lowStock": "true"}),
                ("outdatedStock", {"outdatedStock": "true"}),
                ("lowStock+outdatedStock", {"lowStock": "true", "outdatedStock": "true"}),
            ):
                query = {
                    "integrationId": channel.integration_id,
                    "userId": user_id,
                    "situation": "mapped",
                    "limit": 999999,
                    "offset": 0,
                    **params,
                }
                response = api.get("/my-items", params=query)
                contagem = None
                if isinstance(response.data, dict):
                    for key in ("results", "items", "data"):
                        if isinstance(response.data.get(key), list):
                            contagem = len(response.data[key])
                            break
                    total = response.data.get("total")
                elif isinstance(response.data, list):
                    contagem, total = len(response.data), None
                else:
                    total = None
                entry[f"filtro_{nome}"] = {"status": response.status, "itens": contagem, "total": total}

            canais[channel.key] = entry
        report["canais"] = canais

        # 6) rate limit interno -----------------------------------------
        rate: dict = {}
        for marketplace in ("shopee", "mercadolivre", "mercado_livre", "ml"):
            response = api.get(f"/internal/rate-limit/{marketplace}/{CHANNELS['shopee'].integration_id}")
            rate[marketplace] = {"status": response.status, "corpo": response.data if response.status == 200 else None}
        report["rate_limit"] = rate
        return report
    finally:
        transport.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="reconhecimento read-only da API WeDrop")
    parser.add_argument("--env-file", default=str(Path.home() / ".wedrop" / "creds.env"))
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--json", action="store_true", help="imprime o relatório inteiro em JSON")
    args = parser.parse_args()

    env_file = Path(args.env_file)
    if not env_file.exists():
        sys.stderr.write(f"arquivo de credenciais não encontrado: {env_file}\n")
        return 2

    report = probe(env_file, args.api_base)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"\ncredenciais: {report['credenciais']['email']} (origem: {report['credenciais']['origem']})")
    print(f"api_base: {report['api_base']}\n")

    login = report["etapas"].get("login", {})
    if not login.get("ok"):
        print(f"LOGIN: FALHOU -> {login.get('erro')}")
        return 1
    print(
        f"LOGIN: ok · JWT com {login['minutos_de_validade']} min de validade"
        f" · expira {login['exp_iso']} · formato JWT: {login['formato_jwt']}"
    )
    print(f"/me: status {report['etapas']['me']['status']} · chaves {report['etapas']['me']['chaves']}")

    integracoes = report.get("integracoes") or {}
    print(f"/my-integrations: {len(integracoes)} integração(ões)")
    for ident, info in sorted(integracoes.items()):
        print(f"  - {ident}: {info.get('name')} (status={info.get('status')})")

    print("\ncanais usados pela automação:")
    for key, entry in (report.get("canais") or {}).items():
        print(f"  [{key}] {entry['nome']} · integrationId={entry['integrationId']}")
        print(f"      envelope: {entry.get('envelope')} · lista em '{entry.get('campo_da_lista')}' · total={entry.get('total_no_corpo')}")
        if entry.get("campos_do_item"):
            print(f"      campos do item: {entry['campos_do_item']}")
        print(
            f"      catálogo mapeado: {entry.get('catalogo_mapeado')} itens"
            f" ({entry.get('segundos_para_listar')}s) · outdatedStock: {entry.get('com_outdatedStock')}"
            f" · qtd<=10: {entry.get('com_availableQuantity<=10')}"
        )
        for filtro in ("lowStock", "outdatedStock", "lowStock+outdatedStock"):
            dados = entry.get(f"filtro_{filtro}") or {}
            print(f"      filtro {filtro}: {dados.get('itens')} itens (total={dados.get('total')})")

    print("\nrate limit interno:")
    for marketplace, dados in (report.get("rate_limit") or {}).items():
        print(f"  {marketplace}: HTTP {dados['status']} {dados['corpo'] if dados['corpo'] else ''}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
