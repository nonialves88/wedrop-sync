# Contrato da API do WeDrop — o que está confirmado e por quem

Consolidação de três fontes:

1. **handoff do Claude** (`claude/CONTEXTO_WEDROP_API.md`) — engenharia reversa
   por captura de tráfego numa aba já autenticada;
2. **especificação da rotina** (`ESPECIFICACAO_TECNICA.md`) — fluxo e resultados
   de rodadas reais;
3. **verificação ao vivo** feita aqui em 25/10/2026 com `scripts/probe_api.py` e
   `scripts/probe_update.py` — login por API e corpos de resposta reais.

Legenda: ✅ confirmado ao vivo · ⚠️ corrigido/completado em relação ao handoff ·
❓ ainda não verificado.

Base: `https://api.wedrop.com.br/v3/api` · `Authorization: Bearer <JWT>` ·
`userId` da conta: **10000**

---

## 1. Conta e integrações

| `integrationId` | Nome na tela | Situação |
|---|---|---|
| `10001` | shopee | ativa |
| `10002` | LOJA-EXEMPLO **CPF** | ativa, **perdeu autorização** (403 do ML) |
| `10003` | LOJA-EXEMPLO **PJ** | ativa |
| `10004` | Bling | **inativa** — fora do fluxo |

✅ `GET /my-integrations` devolve exatamente esses quatro, com `status`.

---

## 2. Autenticação — ⚠️ **correção importante**

O handoff dizia: *"não foi localizado nem procurado um endpoint de login/refresh
— renovação manual do token"*. **Existe e funciona:**

```
POST /auth
{ "email": "...", "password": "..." }

200 → { "access_token": "<JWT>" }
```

- ✅ testado ao vivo: JWT com `exp` **~240 minutos**.
- O cliente lê `exp` do próprio token e renova **5 min antes** de expirar; em
  401 renova e repete a chamada uma vez.
- Também aceita `{requiresCode: true, message: "…código de verificação…"}` quando
  o WeDrop pede verificação por e-mail → tratado como erro `email_code` (alerta
  "precisa de login manual"), não como senha errada.
- `GET /me` → 200 com **`id`** = 10000 (⚠️ a chave é `id`, não `userId`; o código
  aceita as duas).

Consequência prática: a limitação "token manual a cada 4h" **deixou de existir**.

---

## 3. `GET /my-items` — listar anúncios

| Param | Observação |
|---|---|
| `integrationId` | ✅ obrigatório |
| `userId` | ✅ 10000 |
| `situation` | ✅ `mapped` |
| `limit` | ✅ `999999` traz tudo numa chamada (2278 em ~8s; 2427 em ~23s) |
| `offset` | ✅ 0 |
| `lowStock` | ✅ `true` → filtro **de estoque baixo do servidor** |
| `outdatedStock` | ✅ `true` → só os desatualizados |

Resposta: `{"total": <int>, "results": [ … ]}` ✅

### Contagens de exemplo (fictícias)

| Canal | catálogo | `lowStock=true` | `availableQuantity<=10` (local) | `outdatedStock=true` |
|---|---|---|---|---|
| Shopee 10001 | 2000 | **800** | 1300 | 3 |
| ML CPF 10002 | 900 | **180** | 170 | 40 |
| ML PJ 10003 | 2400 | **700** | 1300 | 4 |

⚠️ **Importante**: `lowStock` **não** é sinônimo de
`itemsProducts[0].availableQuantity <= 10` (800 vs 1300 na Shopee). O servidor
aplica a regra dele. Portanto:

- "anúncios de estoque baixo" = **usar `lowStock=true`**;
- `outdatedStock=true` é um segundo sinal, que entra **na frente da fila**;
- filtrar no cliente por `availableQuantity <= 10` (como o workflow n8n faz)
  produz uma seleção **maior** que a da tela — não é o mesmo conjunto.

### Campos de cada anúncio (amostra real)

```json
{
  "id": 9000004,                  "key": "43235435642",
  "integrationId": 10001,          "userId": 10000,
  "isMapped": true,                "isVariation": false, "isKit": false,
  "outdatedStock": false,          "outdatedVariations": [],
  "stockSyncState": null,          "stockSyncReason": null, "stockSyncAction": null,
  "lastStockUpdate": "2026-10-23T19:32:18.738Z",
  "itemsProducts": [
    { "availableQuantity": 20,     // ← publicado NO MARKETPLACE
      "product": { "availableQuantity": 94,  // ← estoque na WeDrop
                   "reservedQuantity": 0, "sku": "TUD0685", "cost": 34.39 } }
  ]
}
```

### `stockSyncState` — ⚠️ não estava no handoff

Valores vistos ao vivo: `bloqueado`, `na_fila`, vazio. Quando vem
`bloqueado`, o **próprio WeDrop** explica o motivo em `stockSyncReason`:

- `"Combo: o canal calcula o estoque pelos produtos que o compõem"` →
  `stockSyncAction: "Desfaça o combo no canal…"`
- `"Anúncio em revisão no canal"`
- `"Na fila de recuperação"`

Regra adotada: **item bloqueado não recebe `update-stock`** — conta como
`pulado` (motivo `channel_status`), sem gastar requisição e sem virar erro.

---

## 4. `POST /my-items/update-stock` — a ação real

```
{ "integrationId": 10001, "itemId": 9000004 }
```

A WeDrop calcula a quantidade a partir do estoque de origem; o cliente **não
envia número**. ✅ Latência medida: **~1,7s** por anúncio aceito.

### Sucesso — corpo real ✅

```json
{
  "results": [
    { "itemProductId": 6741916, "itemProductKey": "MLB5406129907",
      "availableQuantity": 0,                       // ← o que a WeDrop enviou
      "success": true,
      "mlRaw": { "primary": { "endpoint": "PUT /items/MLB5406129907",
                              "requestBody": { "available_quantity": 0 },
                              "status": 200, "response": { … } } } }
  ]
}
```

### Falha — corpo real ⚠️ (o handoff não tinha capturado)

```json
{
  "results": [],
  "errors": [
    { "itemProductId": 3374833, "itemProductKey": "MLB4548499747",
      "error": "PUT /items/… falhou: … The caller is not authorized to access this resource | forbidden | HTTP 403",
      "errorType": "PUT_ITEMS_FAILED",
      "mensagem": "A conexão com esta loja perdeu a autorização. Reconecte a integração…" }
  ],
  "success": false, "partialSuccess": false,
  "message": "1 erro(s)",
  "mensagem": "A conexão com esta loja perdeu a autorização. Reconecte a integração…"
}
```

Regras de leitura implementadas em `classify.py`:

- **HTTP 200 não é sucesso** — o veredito sai do corpo;
- `mensagem` (pt-BR) tem prioridade sobre `error` (texto técnico);
- `perdeu a autorização` / `not authorized` / `forbidden` → `auth_loss`
  (alimenta o circuit breaker e o alerta "reconecte a integração");
- `under_review` / `inactive` / `closed` / `bloqueado` → `channel_status`
  (pulado, benigno);
- órfão (`not found`, `não existe`, `deleted`) → `orphan` (pulado, benigno);
- `model_id is mandatory` → `missing_model_id` (erro real: reimportar anúncio);
- `partialSuccess` → `partial`; `success:false` com mistura de produtos → `partial`.

### Quantidade enviada

A WeDrop pode enviar **0** mesmo quando `product.availableQuantity > 0`
(observado: produto 29649 com 4 unidades na WeDrop → `available_quantity: 0` na
publicação). Ela é a dona da conta de estoque; por isso o painel mostra
**"unidades enviadas"** em vez de prometer "0 → 4".

---

## 5. Rate limit — `GET /internal/rate-limit/{marketplace}/{integrationId}`

⚠️ o handoff registrou "corpo não inspecionado". Corpo real:

```json
{ "success": true,
  "data": { "integrationId": 10001, "requestCount": 0,
            "windowStart": 1792964411298, "timeRemaining": 60000,
            "consecutive429s": 0, "isInBackoff": false,
            "backoffUntil": 0, "canMakeRequests": true } }
```

- ✅ `mercadolivre` responde 200 (inclusive para a integração Shopee);
  `shopee`, `mercado_livre`, `ml` respondem **404**.
- Implementado em `ratelimit.py`: consulta com cache de 10s antes de cada
  envio; se `canMakeRequests=false`, espera até `backoffUntil`; se nenhum
  caminho responder, a sonda se desliga e sobra o backoff de 429.

### Cadência do marketplace

Do handoff (lote real de 894 itens Shopee): **1 anúncio a cada 30–50s**. Isso é
limite do marketplace, não da WeDrop. Regras adotadas:

- `update-stock` é **serializado** (`SerializedPoster`, `WEDROP_SERIALIZE_UPDATES=1`
  por padrão): com `--workers N` a preparação é paralela, mas o POST sai um por
  vez. Paralelizar é o caminho curto para 403/captcha.
- ritmos do painel: **cuidadoso** (5s), **padrão** (2s), **agressivo** (0,5s) —
  e mesmo o agressivo respeita o gate e o 429.

---

## 6. Endpoints conhecidos e não usados

| Endpoint | Estado |
|---|---|
| `GET /my-items/{itemId}/details` | ✅ existe (200, mesmo shape + `variations`). ❓ não precisamos: a listagem já traz tudo. |
| `GET /my-products` | ✅ existe: `{total: 2478, results: [...]}` com catálogo de produtos/fornecedor. ❓ não mapeado a fundo (o id do produto do anúncio **não** aparece nessa lista). |
| `GET /products`, `/products/stock` | **403 Permission Missing** |
| `GET /stock`, `/my-stock`, `/my-items/products` | **404** |

---

## 7. Resumo das divergências com o handoff do Claude

| Tema | Handoff | Verificado |
|---|---|---|
| Login/refresh | "não existe, renove o token à mão" | ✅ `POST /auth` com e-mail/senha, JWT 240 min, renovação automática |
| Corpo do `update-stock` | "não confirmado o schema de erro" | ✅ sucesso e erro documentados acima |
| `update-stock` no ML | "não testado" | ✅ testado: **ML CPF está sem autorização (403)**; ML PJ responde 200 |
| Filtro "estoque baixo" | "combine `outdatedStock` com `availableQuantity<=10`" | ⚠️ o filtro da tela é `lowStock=true` do servidor (878 ≠ 1383) |
| Rate limit endpoint | "corpo não inspecionado" | ✅ `canMakeRequests`/`backoffUntil`/`consecutive429s` |
| Anúncios bloqueados | não mencionado | ⚠️ `stockSyncState=bloqueado` + motivo → pular sem chamar |
| Latência por item | "30–50s" | ✅ ~1,7s quando aceito; a cadência de 30–50s é limite de lote do marketplace |
