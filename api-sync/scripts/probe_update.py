#!/usr/bin/env python3
"""Teste de UM item no update-stock real (aprende formato de resposta e latência).

Sem `--apply` não altera nada: só lista e mostra o que seria enviado.
Com `--apply`, envia **um único** anúncio e mostra o efeito (outdatedStock
antes/depois). É a menor ação possível para validar o contrato da API.

    python scripts/probe_update.py                 # dry: mostra o candidato
    python scripts/probe_update.py --apply         # envia 1 item (Shopee)
    python scripts/probe_update.py --apply --only ml1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from wedrop_sync.auth import TokenProvider  # noqa: E402
from wedrop_sync.classify import classify_update  # noqa: E402
from wedrop_sync.client import ApiClient, HttpTransport  # noqa: E402
from wedrop_sync.config import CHANNELS, load_credentials, mask_email  # noqa: E402
from wedrop_sync.items import extract_items  # noqa: E402

MARKETPLACES = {
    "shopee": ("shopee", "shopee-br", "shopee_br", "shopee.com.br"),
    "ml1": ("mercadolivre",),
    "ml2": ("mercadolivre",),
}


def items_of(api, integration_id: int, user_id, extra: dict | None = None):
    params = {
        "integrationId": integration_id,
        "userId": user_id,
        "situation": "mapped",
        "limit": 999999,
        "offset": 0,
    }
    if extra:
        params.update(extra)
    return extract_items(api.get("/my-items", params=params).data)


def resumo_item(item: dict) -> dict:
    produtos = item.get("itemsProducts") or []
    publicado = origem = None
    if produtos and isinstance(produtos[0], dict):
        publicado = produtos[0].get("availableQuantity")
        produto = produtos[0].get("product")
        if isinstance(produto, dict):
            origem = produto.get("availableQuantity")
    return {
        "id": item.get("id"),
        "key": str(item.get("key"))[:40],
        "status": item.get("status"),
        "outdatedStock": item.get("outdatedStock"),
        "outdatedVariations": item.get("outdatedVariations"),
        "stockSyncState": item.get("stockSyncState"),
        "stockSyncReason": item.get("stockSyncReason"),
        "stockSyncAction": item.get("stockSyncAction"),
        "qtd_publicada": publicado,
        "qtd_origem": origem,
        "lastStockUpdate": item.get("lastStockUpdate"),
        "isVariation": item.get("isVariation"),
        "n_variacoes": len(item.get("variations") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="teste de 1 item no update-stock")
    parser.add_argument("--env-file", default=str(Path.home() / ".wedrop" / "creds.env"))
    parser.add_argument("--only", default="shopee", choices=sorted(CHANNELS))
    parser.add_argument("--apply", action="store_true", help="envia de verdade (1 item)")
    parser.add_argument("--item-id", type=int, default=None, help="escolhe um item específico")
    args = parser.parse_args()

    creds = load_credentials(Path(args.env_file))
    transport = HttpTransport("https://api.wedrop.com.br/v3/api", connect_timeout=15, read_timeout=90)
    tokens = TokenProvider(transport, creds)
    api = ApiClient(transport, tokens, read_attempts=2, write_attempts=1)
    try:
        token = tokens.token()
        me = api.get("/me").data or {}
        user_id = me.get("id")
        channel = CHANNELS[args.only]
        print(f"login: {mask_email(creds.email)} · userId={user_id} · canal={channel.key} ({channel.integration_id})")

        # nomes de marketplace aceitos pelo gate de rate limit
        for nome in MARKETPLACES.get(args.only, ()):
            resp = api.get(f"/internal/rate-limit/{nome}/{channel.integration_id}")
            print(f"rate-limit/{nome}: HTTP {resp.status} {json.dumps(resp.data, ensure_ascii=False) if resp.status == 200 else ''}")

        todos = items_of(api, channel.integration_id, user_id)
        desatualizados = [it for it in todos if it.get("outdatedStock")]
        print(f"catálogo: {len(todos)} · desatualizados: {len(desatualizados)}")
        if not desatualizados:
            print("nada com outdatedStock=true agora; escolhendo um item com qtd publicada <= 10")
            candidatos = []
            for item in todos:
                info = resumo_item(item)
                if isinstance(info["qtd_publicada"], (int, float)) and info["qtd_publicada"] <= 10:
                    candidatos.append(item)
            desatualizados = candidatos
        if not desatualizados:
            print("nenhum candidato — nada a fazer")
            return 0

        alvo = None
        if args.item_id is not None:
            alvo = next((it for it in todos if it.get("id") == args.item_id), None)
        alvo = alvo or desatualizados[0]

        print("\ncandidato:")
        print(json.dumps(resumo_item(alvo), ensure_ascii=False, indent=2))

        if not args.apply:
            print("\n(dry) use --apply para enviar este único item")
            return 0

        antes = len(items_of(api, channel.integration_id, user_id, {"outdatedStock": "true"}))
        print(f"\nenviando POST /my-items/update-stock {json.dumps({'integrationId': channel.integration_id, 'itemId': alvo['id']})}")
        started = time.time()
        response = api.post(
            "/my-items/update-stock",
            {"integrationId": channel.integration_id, "itemId": alvo["id"]},
            attempts=1,
        )
        elapsed = time.time() - started
        verdict = classify_update(response.status, response.data)
        print(f"HTTP {response.status} em {elapsed:.2f}s · veredito: {verdict.describe()}")
        print(f"corpo: {json.dumps(response.data, ensure_ascii=False)[:600]}")

        # reconsulta: o item saiu de "desatualizado"?
        for espera in (2, 5, 10):
            time.sleep(espera)
            atual = items_of(api, channel.integration_id, user_id, {"outdatedStock": "true"})
            print(f"  após +{espera}s: {len(atual)} desatualizados (antes: {antes})")
            if len(atual) < antes:
                break
        print("\nOK: o contrato do update-stock está validado com 1 item real.")
        return 0
    finally:
        transport.close()


if __name__ == "__main__":
    sys.exit(main())
