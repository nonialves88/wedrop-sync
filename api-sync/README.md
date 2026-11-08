# wedrop-sync — sincronização de estoque WeDrop via API (sem navegador)

Rotina que atualiza o estoque dos anúncios mapeados no painel WeDrop
(`dash.wedrop.com.br`) usando **a API que o próprio front-end usa**, sem
navegador, sem agente no meio e sem clicar em nada.

- **Canais** (IDs reais ficam em `~/.wedrop/channels.json`, nunca no git; modelo em `../config/channels.example.json`), nesta ordem: Shopee (`10001`) → Mercado Livre 1 (`10002`) →
  Mercado Livre 2 (`10003`). O Bling (`10004`) fica de fora.
- **Regra padrão:** anúncios com **estoque baixo** (filtro do servidor,
  `lowStock=true`). Modo completo com `--all` (ou `WEDROP_ALL=1`).
- **Runtime:** Python 3.9+ **sem nenhuma dependência externa** (só stdlib).
- **Aviso:** só quando precisa de gente (login, código por e-mail, integração
  sem autorização, pico de erro). Ruído de catálogo nunca gera mensagem.

```
$ python -m wedrop_sync run
2026-10-25 06:07:01 INFO  rodada iniciada · modo=normal · canais=shopee,ml1,ml2 · workers=3 · delay=0.20s
2026-10-25 06:07:01 INFO  seleção shopee: 864 anúncio(s) · mapeados=2278 · desatualizados=112 · duplicados removidos=112
2026-10-25 06:12:41 INFO  progresso shopee: 300/864 (35%) ok=298 erro=2 timeout=0 pulado=0 · 0.9/s · restam ~10min
...
2026-10-25 10:18:33 INFO  SUMMARY shopee mapeados=2278 selecionados=864 ok=859 parcial=0 pulado=0 erro=4 timeout=1 duracao=15092s
```

---

## 0. O jeito mais simples: painel com um botão por integração

```bash
python -m wedrop_sync serve --open      # credenciais em ~/.wedrop/creds.env
# → http://127.0.0.1:8790
```

> **Onde fica a senha:** `~/.wedrop/creds.env` — fora da pasta do projeto e
> **fora do OneDrive**, com permissão só do dono (`chmod 600` no Linux;
> `icacls … /inheritance:r /grant:r "$USER:(F)"` no Windows). O app procura
> nessa pasta automaticamente; `--env-file caminho` sobrepõe. Nada de
> credencial em arquivo versionado ou sincronizado na nuvem.

Uma tela com **três botões** (Shopee, Mercado Livre **CPF**, Mercado Livre
**PJ** — os mesmos nomes da tela "Minhas Integrações"). Cada card mostra antes
de clicar: catálogo mapeado, quantos estão com estoque baixo, quantos estão
desatualizados e quantos entrariam na seleção. Você escolhe o critério,
opcionalmente **só simular** (padrão) ou limitar a N anúncios para testar, e
clica. O progresso (ok / pulados / erros / ETA) aparece ao vivo.

- **Barra de progresso ao vivo**: cada card tem a sua. Ela é alimentada pelo
  arquivo `state/live.json`, então mostra **qualquer** rodada — a que você
  clicou, a de `python -m wedrop_sync run`, a do cron e a do modo paralelo.
  O painel volta a consultar a cada 3s enquanto houver rodada em andamento.
- **Os 3 de uma vez** (o mesmo que fazer em 3 navegadores): botão
  **"Sincronizar os 3"** no painel, ou `--parallel` no CLI. Uma faixa por
  marketplace — a Shopee corre em paralelo com o Mercado Livre, mas **ML CPF e
  ML PJ se revezam** (dividem o mesmo orçamento de rate limit; disparar os dois
  juntos é o caminho curto para 403/captcha).
- Um canal por vez, o escolhido — nada de mexer nos outros.
- Duas rodadas ao mesmo tempo são recusadas (trava em disco).
- `update-stock` sai **serializado** (um por vez): é limite do marketplace
  (Shopee/ML), não da WeDrop. Antes de cada envio o app consulta o gate do
  próprio WeDrop (`canMakeRequests` / `backoffUntil`) — ver
  [`docs/API_CONTRATO.md`](docs/API_CONTRATO.md#5-rate-limit--get-internalrate-limitmarketplaceintegrationid).
- O login é feito pela API, sem navegador: a senha fica só no arquivo de
  credenciais desta máquina.

Scripts de reconhecimento (úteis quando algo mudar no WeDrop):

```bash
python scripts/probe_api.py                 # login, canais, filtros, formatos
python scripts/probe_update.py --item-id 123456 --apply   # testa 1 anúncio, mostra o corpo bruto
```

## 0.1 O que a engenharia reversa confirmou com dados reais (25/10/2026)

Metodologia do [reverse-api-engineer](https://github.com/kalil0321/reverse-api-engineer):
descobrir os endpoints que o front-end usa, mapear autenticação e formato, e
transformar isso em código que roda sozinho — **inclusive reautenticando**.

> O contrato completo — com o que o handoff do Claude acertou, o que foi
> corrigido e o que ainda falta — está em
> [`docs/API_CONTRATO.md`](docs/API_CONTRATO.md). Resumo das divergências:
> o login **existe** (o handoff dizia que não), o corpo do `update-stock`
> **está documentado** (sucesso e erro), o filtro "estoque baixo" é
> `lowStock=true` do servidor (não `availableQuantity<=10`), anúncios
> `bloqueado` devem ser pulados, e o gate de rate limit tem corpo conhecido.

| Achado | Detalhe |
|---|---|
| **Login funciona por API** | `POST /auth {email, password}` devolve JWT válido por **240 min**. O cliente renova sozinho lendo `exp`; nada de copiar token do DevTools. |
| `/me` | devolve `id` (userId `10000`), não `userId`. O código aceita os dois. |
| `/my-integrations` | `10001 shopee` (active), `10002` ML **CPF** e `10003` ML **PJ** (active), `10004 Bling` (pending → fora). |
| `/my-items` | envelope `{results, total}`; `limit=999999` traz tudo numa chamada (2278 itens em ~8s; 2427 em ~23s). |
| **Filtros são do servidor** | `lowStock=true` → 878 / 180 / 740 anúncios nos três canais (o mesmo que a tela). `outdatedStock=true` → 3 / 48 / 4. |
| **Payload de cada anúncio** | `outdatedStock`, `stockSyncState` (`bloqueado`, `na_fila`), `stockSyncReason`, `itemsProducts[0].availableQuantity` (publicado no canal) e `itemsProducts[0].product.availableQuantity` (estoque na WeDrop). |
| **`update-stock`** | `{integrationId, itemId}` → HTTP 200 com `{results:[{success, availableQuantity, mlRaw}], errors:[...], mensagem}`. Sucesso medido em **1,7s** por anúncio. |
| Anúncio **bloqueado** | combo/revisão vem com `stockSyncState=bloqueado` + motivo. Agora é **pulado sem gastar chamada** (antes contava como erro). |
| **ML1 sem autorização** | o payload real traz `HTTP 403 ... The caller is not authorized` e `mensagem: "A conexão com esta loja perdeu a autorização. Reconecte a integração"`. Classificado como `auth_loss` → circuit breaker + alerta pedindo reconexão. **É o problema que a especificação já apontava (142 erros).** |
| Quantidade enviada | o WeDrop calcula a quantidade a partir do estoque dele; o cliente **não** envia número. Às vezes ele envia `0` mesmo com `product.availableQuantity>0` — por isso o painel mostra "unidades enviadas" em vez de prometer o valor. |
| Rate limit | `/internal/rate-limit/mercadolivre/{integrationId}` responde com `canMakeRequests`, `backoffUntil`, `consecutive429s` (para Shopee o path devolve 404). 429 do marketplace é respeitado via `Retry-After`. |

---

## 1. Instalação

```bash
# não precisa de pip: é só copiar a pasta e ter Python 3.9+
cd api-sync
chmod +x   # opcional
python3 -m unittest discover -s tests -t .   # 89 testes, roda em ~18s
```

Para ter o comando `wedrop-sync` no PATH:

```bash
pip install -e .      # instala o entry point (sem baixar dependência nenhuma)
```

### Credenciais

```bash
cp creds.env.example creds.env
$EDITOR creds.env          # WEDROP_EMAIL e WEDROP_PASSWORD
chmod 600 creds.env        # só o dono lê
```

Aceita também as chaves `usuario` e `senha` (o arquivo antigo do projeto), com
ou sem espaços em volta do `=`. Variáveis de ambiente têm precedência sobre o
arquivo, então `WEDROP_EMAIL`/`WEDROP_PASSWORD` exportadas funcionam igual.

**A senha nunca é impressa**: `Credentials.__repr__` mascara, nada grava token
em disco e o script não ecoa o corpo do login em log. Há teste automatizado
para isso (`test_senha_nao_aparece_no_log`, `test_senha_nunca_aparece_no_repr`).

---

## 2. Uso

```bash
python -m wedrop_sync serve        # PAINEL com botão por integração (o mais simples)
python -m wedrop_sync check        # login + integrações (não altera nada)
python -m wedrop_sync --dry-run    # monta a fila e mostra quantos seriam atualizados
python -m wedrop_sync              # rodada normal (comando padrão)
python -m wedrop_sync status       # últimas rodadas
```

| Opção | Padrão | Para quê |
|---|---|---|
| `--only shopee,ml1,ml2` | os três | canais e ordem |
| `--workers 3` | 3 | atualizações em paralelo |
| `--delay 0.2` | 0.2s | pausa mínima entre envios |
| `--max N` | 0 | limita itens por canal (para testar) |
| `--dry-run` | — | não envia nada |
| `--all` | — | todos os mapeados (equivale a `WEDROP_ALL=1`) |
| `--resume` | — | pula itens já enviados com sucesso |
| `--verify-samples N` | 25 | relê N itens no fim para provar que o estoque saiu de "desatualizado" |
| `--threshold N` | — | compatibilidade: o filtro hoje é do servidor, a opção é aceita e ignorada |
| `--webhook URL` | — | aviso por POST JSON (Discord/Slack/n8n) |
| `--notify-command CMD` | — | aviso por stdin em qualquer comando (telegram, mail…) |
| `--notify-file PATH` | — | aviso em arquivo |
| `--quiet` | — | stdout só com avisos (o log em arquivo continua completo) |
| `--lock-timeout S` | 0 | espera pela trava em vez de sair |

Códigos de saída: `0` ok · `2` rodou mas tem algo para olhar · `3` falha de
autenticação · `4` já existe rodada em andamento · `130` interrompida.

---

## 3. Como funciona por dentro

```
credentials ──► TokenProvider ──► POST /auth  (JWT; exp lido do token, renova 5min antes)
                     │
                     ▼
       Runner ──► para cada canal, na ordem:
            1. GET /my-items?situation=mapped&lowStock=true   (pagina 50 em 50)
            2. GET /my-items?...&outdatedStock=true           (esses entram na frente)
            3. dedupe por itemId
            4. POST /my-items/update-stock {itemId, integrationId}  (pool + ritmo)
            5. SUMMARY do canal + artefato JSONL + last-run.json
```

### Por que não perde tempo nem queima a conta

| Peça | O que resolve |
|---|---|
| `TokenProvider` | renova o token **antes** de expirar (lendo `exp` do JWT) e reautentica uma vez em 401 — rodada de 9h+ não morre no meio |
| `Gate` | concorrência adaptativa: em 429/5xx fecha a porta (menos workers) e reabre aos poucos; não existe a escolha burra entre "lento para sempre" e "429 em cascata" |
| `Pacer` | espaçamento mínimo com jitter, para os workers não sincronizarem os envios |
| circuit breaker | N erros seguidos de **perda de autorização** abortam o canal na hora (o ML1 gastou 23min para produzir 142 erros iguais; agora para no 12º e avisa) |
| `--resume` | derrubou a rodada de 24h? retoma sem reenviar o que já deu certo |
| `--verify-samples` | relê uma amostra com `outdatedStock=true` no fim: se ainda vier tudo marcado, a atualização não teve efeito (fim do "falso alarme" de 22/10) |
| `RunLock` | `flock` (POSIX) / `msvcrt` (Windows): nunca duas rodadas juntas |

### Classificação das respostas (`classify.py`)

HTTP 200 não é sucesso. O corpo é lido como o front-end faz (`success`,
`partialSuccess`, `skipped`, `errors`) e cada resposta vira um veredito com um
**motivo** — é isso que permite resumir, alertar e comparar rodadas sem ler
log no olho:

| Motivo | Tratamento |
|---|---|
| `orphan` (não existe mais, deletado, removido) | **pulado**, segue a fila, nunca alerta |
| `channel_status` (`under_review`, `inactive`, `closed`) | **pulado**, nunca alerta |
| `auth_loss` (perdeu a autorização) | erro: conta, e alimenta o circuit breaker |
| `missing_model_id` (variações da Shopee) | erro de catálogo: precisa reimportar no painel |
| `rate_limit` / `http_5xx` / `timeout` | contados à parte; 5xx na escrita **não** repete, na leitura repete |
| `partial`, `skipped` | contados como tal |

### Política de alerta (`report.py`)

Só avisa quando precisa de gente:

- falha de login, ou pedido de **código por e-mail**;
- integração com **perda de autorização** (ou canal abortado por isso);
- **pico de erro**: `erros+timetouts ≥ 15` **e** `≥ 10%` do processado do canal;
- piora relevante contra a rodada anterior (`> 2,5×` os erros);
- rodada interrompida.

Órfãos, `under_review`, `closed` e erro isolado **não** geram mensagem — é
exatamente o ruído que a versão anterior filtrava a olho no log.

---

## 4. O que a rodada grava

```
~/.wedrop/
├── creds.env              # 600, só o dono lê
├── .sync.lock / .pid      # trava de execução
├── last-run.log           # log da rodada atual/última (flush por linha)
├── prev-run.log           # cópia da rodada anterior
└── state/
    ├── last-run.json      # resumo estruturado + alertas da última rodada
    ├── history.jsonl      # últimas 200 rodadas (para `status`)
    ├── done-<canal>.json  # ids já enviados (usado por --resume)
    └── runs/
        └── 20261025-060701-shopee.jsonl   # um registro por anúncio
```

`python -m wedrop_sync status` lê esse histórico. `last-run.json` é a fonte da
verdade para a rotina decidir se avisa — não há mais parse de log no olho.

---

## 5. Rodar todo dia às 6h e 18h

### systemd (recomendado)

```bash
sudo cp deploy/wedrop-sync.service deploy/wedrop-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wedrop-sync.timer
systemctl list-timers wedrop-sync.timer        # conferir o próximo disparo
journalctl -u wedrop-sync -n 50                # conferir a última rodada
```

### cron

```cron
CRON_TZ=America/Recife
0 6,18 * * * cd /home/box/.wedrop && WEDROP_HOME=/home/box/.wedrop python3 -m wedrop_sync run --quiet 2>>cron-err.log
```

Em qualquer um dos dois, a trava `.sync.lock` garante que a rodada das 18h não
pise na das 6h se uma delas ainda estiver rodando (e o processo sai com
código `4`, em silêncio).

---

## 6. Testes

```bash
python3 -m unittest discover -s tests -t .      # 89 testes, ~18s
python3 scripts/demo.py                        # rodada inteira contra API falsa
python3 scripts/demo.py --broken                # simula integração sem autorização
python3 scripts/demo.py --full                  # modo completo
```

Cobrem: parsing de credenciais (inclusive o formato `usuario`/`senha`), ordem e
limpeza de canais, todas as formas de resposta da API, expiração/renovação de
token, código por e-mail, credencial inválida, paginação, dedupe, prioridade
dos desatualizados, 429, 5xx em leitura e escrita, timeout, circuit breaker,
`--resume`, verificação de resíduo, trava, política de alerta e a CLI ponta a
ponta (estado, histórico, rotação de log, código de saída).

Nada toca a internet: os testes sobem uma API falsa em `127.0.0.1` (`tests/mockapi.py`).

---

## 7. Problemas comuns

| Sintoma | O que fazer |
|---|---|
| saída `3` e "login recusado" | confira `creds.env`; se a senha mudou, atualize o arquivo |
| "pediu código de verificação por e-mail" | entre manualmente no painel uma vez, depois rode de novo |
| `4` "já existe uma sincronização em andamento" | é a trava funcionando; veja o pid no `.sync.lock.pid` |
| muitos `orphan` no ML1 | são anúncios que não existem mais: desative/exclua no painel para reduzir ruído |
| muitos `missing_model_id` na Shopee | anúncios com variação órfã: precisam ser reimportados/remapeados no painel |
| canal abortado por autorização | reconecte a integração em "Minhas integrações" no WeDrop |
| rodada longa (ML2 ~9h) | é o tamanho do catálogo; teste `--workers 5` ou `--delay 0.1` observando 429 |

---

## 8. Referência de variáveis de ambiente

| Variável | Padrão | |
|---|---|---|
| `WEDROP_HOME` | `~/.wedrop` | pasta de trabalho |
| `WEDROP_ENV_FILE` | `<home>/creds.env` | credenciais |
| `WEDROP_EMAIL` / `WEDROP_PASSWORD` | — | ganham do arquivo |
| `WEDROP_API_BASE` | `https://api.wedrop.com.br/v3/api` | útil para testes |
| `WEDROP_ONLY` | `shopee,ml1,ml2` | canais |
| `WEDROP_WORKERS` / `WEDROP_DELAY` | `3` / `0.2` | ritmo |
| `WEDROP_ALL` | `0` | modo completo |
| `WEDROP_DRY_RUN` | `0` | simulação |
| `WEDROP_RESUME` | `0` | retomar |
| `WEDROP_MAX` | `0` | limite de itens |
| `WEDROP_PAGE_SIZE` | `50` | paginação |
| `WEDROP_CONNECT_TIMEOUT` / `WEDROP_READ_TIMEOUT` | `10` / `30` | timeouts (s) |
| `WEDROP_READ_ATTEMPTS` / `WEDROP_UPDATE_ATTEMPTS` | `3` / `2` | tentativas |
| `WEDROP_RETRY_AFTER` | `60` | espera padrão em 429 (s) |
| `WEDROP_BREAKER` | `12` | erros de autorização seguidos até abortar o canal |
| `WEDROP_VERIFY_SAMPLES` | `25` | amostra da conferência final |
| `WEDROP_NOTIFY_MIN_ERRORS` / `WEDROP_NOTIFY_ERROR_RATE` | `15` / `0.10` | gatilho do pico |
| `WEDROP_WEBHOOK_URL` / `WEDROP_NOTIFY_COMMAND` / `WEDROP_NOTIFY_FILE` | — | destinos do aviso |
| `WEDROP_PROGRESS_EVERY` | `60` | intervalo do heartbeat no log (s) |
| `WEDROP_QUIET` | `0` | stdout só com avisos |
