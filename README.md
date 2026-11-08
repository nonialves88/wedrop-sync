# Sincronizador de Estoque — WeDrop

> **Duas implementações neste repositório:**
>
> | | Onde | Quando usar |
> |---|---|---|
> | **API** (`api-sync/`) — Python, sem navegador | `api-sync/README.md` | **Padrão recomendado** para a rotina agendada 2x/dia. Fala direto com `api.wedrop.com.br/v3/api`, roda num servidor Linux, gasta zero token, tem trava, retry, circuit breaker e alerta só quando precisa de gente. |
> | **Navegador** (esta pasta) — Node + Playwright/Chrome | abaixo | Uso manual no notebook, dry-run visual e plano B se a autenticação da API mudar (ele clica na tela como você faria). |
>
> Os dois respeitam as mesmas decisões de produto: canais Shopee → ML1 → ML2 em sequência, Bling fora, estoque baixo como padrão, nenhum alerta por anúncio órfão.

---

## Rodar sozinho no GitHub Actions (06:00 e 18:00, sem PC ligado)

O repositório é **público**, então nada sensível pode estar nele — nem nos logs.
Como isso é garantido:

| Dado | Onde fica | Proteção |
|---|---|---|
| Login e senha da WeDrop | **Secrets** do GitHub (`WEDROP_EMAIL`, `WEDROP_PASSWORD`) | nunca no código; mascarados nos logs |
| IDs das integrações | `config/channels.enc` | **AES-256-GCM** + scrypt; decifrado só dentro do job |
| Senha-mestra do cofre | Secret `VAULT_KEY` | gerada aleatória (256 bits) na sua máquina |
| Logs/relatório da rodada | artefato `report-*.enc` (7 dias) | **cifrado** com a mesma chave; o log público mostra só o código de saída |
| Avisos | WhatsApp (Z-API) | `ZAPI_*` e `ALERT_PHONE` em Secrets |
| Barreira | `scripts/check_public_safe.py` no CI | bloqueia JWT, `creds.env`, e-mail real, caminho do Windows… |

### Passo a passo (uma vez)

```bash
# 1) na SUA máquina: crie ~/.wedrop/channels.json com os IDs reais
#    (modelo: config/channels.example.json) e gere o cofre
pip install cryptography
python scripts/setup_vault.py ~/.wedrop/channels.json    # mostra a senha-mestra UMA vez
git add config/channels.enc && git commit -m "cofre" && git push
```

2) GitHub → *Settings → Secrets and variables → Actions → New repository secret*:
`VAULT_KEY`, `WEDROP_EMAIL`, `WEDROP_PASSWORD` e (opcional) `ZAPI_INSTANCE`,
`ZAPI_TOKEN`, `ZAPI_CLIENT_TOKEN`, `ALERT_PHONE` (ex.: `5581999999999`).

3) *Actions → WeDrop Sync → Run workflow*: faça o 1º teste com **dry_run** e depois
com um canal só. Se passar, o agendamento já está valendo.

- Modo padrão do agendado: `simple` (estoque baixo). Para `complete`, crie a
  *variable* `SYNC_MODE=complete` (Settings → Variables).
- Shopee e Mercado Livre rodam em **jobs paralelos**; ML1 e ML2 ficam no mesmo job
  (dividem o rate limit do marketplace).
- Para ler um relatório: baixe o artefato `.enc` e rode
  `VAULT_KEY=… python -m wedrop_sync.vault decrypt report-x.enc - | tar -xz`.
- Se a WeDrop bloquear IPs de datacenter, o login falha: use o servidor/timer
  em `api-sync/deploy/`.

> ⚠️ O cofre é público: se a `VAULT_KEY` vazar ou for fraca, ele é quebrável
> offline. Use a gerada por `setup_vault.py`, não a reutilize em outro lugar e,
> para trocar, rode o script de novo e atualize o Secret.

---

Automação local da rotina **"Atualização de Estoque em Massa"** do painel `dash.wedrop.com.br/my_items`, rodando de uma vez nos canais ativos (Shopee + as duas contas do Mercado Livre). O Bling fica fora (desativado no painel).

> Substitui o fluxo antigo de "copiar um comando e colar na conversa com um agente": aqui não existe LLM no meio, nem copiar/colar, nem credencial em arquivo. O app pilota a aba do seu próprio Chrome.

---

## Como funciona

```
npm start  →  http://127.0.0.1:8787  →  [Sincronizar agora]
                    │
                    ├─ abre/reaproveita um Chrome dedicado (perfil em data/chrome-profile, porta 9222)
                    ├─ conecta por CDP (Playwright), sem tocar no seu perfil pessoal
                    └─ para cada canal ativo:
                         1. abre /my_items
                         2. escolhe o canal pelo identificador interno (10001 / 10002 / 10003)
                         3. escolhe o critério ("Estoque baixo ≤ N" ou "Todos mapeados")
                         4. clica Aplicar e acompanha "X/Y (Z%)" até "N selecionados"
                         5. (opcional) clica Atualizar → confirma "Iniciar Atualização" no modal
                         6. segue para o próximo canal (o job do painel continua em segundo plano)
```

O **login é seu**: na primeira vez você abre a janela do projeto, entra no WeDrop normalmente, e a sessão fica gravada nos cookies do *próprio Chrome* (`data/chrome-profile`). O app nunca vê, digita ou guarda usuário e senha.

---

## Requisitos

- Windows com **Google Chrome** instalado (o app detecta em `Program Files`, `Program Files (x86)` ou `%LOCALAPPDATA%`; dá para fixar em `config/channels.json → chrome.executable` ou na env `WEDROP_CHROME_PATH`).
- **Node.js 20+** (testado no 24).
- Conexão com o painel WeDrop e acesso à sua conta.

## Primeira execução

```powershell
npm install          # instala o playwright-core (não baixa navegador: usa o seu Chrome)

npm run login        # abre o Chrome dedicado e espera você logar no WeDrop
npm run doctor       # confirma: Chrome encontrado, sessão OK, canais configurados

npm start            # sobe o painel local em http://127.0.0.1:8787
```

No painel:

1. Escolha o critério: **Estoque baixo** (com o limite, padrão 10) ou **Todos mapeados**.
2. Marque os canais (os 3 ativos vêm marcados; o Bling aparece riscado como desativado).
3. Opcional: ligue **Dry-run** na primeira vez — ele aplica o filtro e mostra *quantos anúncios seriam atualizados*, sem clicar em Atualizar.
4. **Sincronizar agora**. Progresso por canal, log ao vivo e histórico ficam na mesma tela.

---

## Linha de comando

```powershell
npm run run -- --criterion low --max 10            # estoque baixo (≤10) nos 3 canais
npm run run -- --criterion all                     # todos os mapeados
npm run run -- --dry --channels shopee,ml-1        # dry-run em canais específicos
Ctrl+C                                             # cancela no próximo passo seguro
```

`npm run map` abre o painel real e salva `data/dom-map.json` com todos os `<select>`, botões e campos que ele encontrou — é o comando de diagnóstico quando algo muda no painel.

### Calibrar contra o HTML real (sem rodar nada no site)

Se o painel mudar (ou antes do primeiro uso real), dá para testar os seletores offline:

```powershell
# 1. no Chrome: abra /my_items, F12 → Elements → botão direito no <html>
#    → Copy → Copy outerHTML → cole num arquivo, ex.: data/my_items.html
npm run calibrate -- data/my_items.html
```

Ele renderiza o arquivo num Chrome headless e roda exatamente as mesmas funções de busca que a automação usa, listando o que reconheceu (canal, critério, bloco, Aplicar, Atualizar, limite) e o que faltou. Relatório em `data/calibracao.json`.

---

## Segurança e escopo das ações

- Nenhuma credencial é pedida, digitada ou persistida pelo app. A autenticação é a sua sessão do Chrome.
- A atualização em massa **não é destrutiva de dados**, mas tem efeito público nos anúncios: ela roda só quando você escolhe o critério e clica em Sincronizar (e o modal do painel é confirmado pelo app, exatamente como no fluxo manual).
- Por padrão o app processa **um canal por vez**, de forma sequencial — sem paralelismo entre canais.
- O `data/` guarda apenas: histórico de execuções (`runs.jsonl`), logs do dia (`logs/`), artefatos de falha (`artifacts/`: screenshot + HTML) e o perfil do Chrome dedicado. Tudo está no `.gitignore`.

## Onde ficam as coisas

| Caminho | Para que serve |
|---|---|
| `config/channels.json` | canais (id interno, nome, ativo), tempo limite, retry, porta do Chrome, porta do painel |
| `src/lib/wedrop.js` | o fluxo em si (seleção de canal/critério, Aplicar, progresso, Atualizar, modal) |
| `src/lib/chrome.js` | abre/reaproveita o Chrome dedicado e conecta por CDP; detecta sessão expirada |
| `src/lib/job.js` | execução sequencial, retry por canal, eventos (SSE), cancelamento, histórico |
| `src/server.js` + `public/` | painel local (UI + API + progresso ao vivo) |
| `mock/index.html` | painel WeDrop **simulado**, usado pelos testes automatizados |
| `data/artifacts/` | quando um canal falha: screenshot + HTML + descrição do DOM no momento da falha |

## Configuração (`config/channels.json`)

```jsonc
{
  "dashboardUrl": "https://dash.wedrop.com.br/my_items",
  "chrome": { "port": 9222, "profileDir": "data/chrome-profile", "executable": "" },
  "run": {
    "criterion": "low",            // low | all
    "lowStockMax": 10,
    "dryRun": false,
    "retries": 1,                  // tentativas extras por canal
    "retryDelayMs": 15000,
    "stepTimeoutMs": 90000,        // cada clique/campo
    "selectionTimeoutMs": 1800000, // espera da verificação do filtro (30 min)
    "betweenChannelsMs": 3000
  },
  "channels": [
    { "key": "shopee", "name": "Shopee", "id": "10001", "active": true },
    { "key": "ml-1",   "name": "Mercado Livre — conta 1", "id": "10002", "active": true },
    { "key": "ml-2",   "name": "Mercado Livre — conta 2", "id": "10003", "active": true },
    { "key": "bling",  "name": "Bling",  "id": "10004", "active": false }
  ]
}
```

`id` é o valor da opção no dropdown "Selecione o canal de venda" — é por ele que o canal é escolhido, não por nome (as duas contas do Mercado Livre têm o mesmo nome de loja).

---

## Como a automação não quebra fácil

O app não depende de classes ou ids do painel (que mudam a cada deploy). Ele:

1. localiza o `<select>` de canal **pelo valor da opção** (o id interno) e, se preciso, pelos nomes/aliases;
2. localiza o `<select>` de critério **pelos textos das opções** ("Nenhuma", "Todos mapeados", "Estoque baixo", "Customizado");
3. acha o bloco "Atualização de Estoque em Massa" subindo a árvore a partir do título até o contêiner que tem seletor **e** botão de ação;
4. marca o elemento com `data-wd-sync="..."` e clica por esse marcador (a marca é refeita a cada passo, sobrevivendo a re-render);
5. lê o progresso pelo elemento mais específico que casa com `X/Y (Z%)` ou `N selecionados` — por isso o indicador do **WeDrop AutoPilot** (que não usa `(%)`) não é confundido com o resultado da ação manual.

Se o WeDrop mudar rótulos, `npm run map` mostra o que existe hoje e o ajuste fica em um arquivo só (`src/lib/wedrop.js`, funções `find*`).

## Testes

```powershell
npm test
```

Roda o Chrome de verdade (headless) contra o painel simulado e valida: dry-run nos 3 canais, critério "todos mapeados", execução completa com confirmação do modal, retry após falha transitória, interrupção quando a sessão expira, bloqueio de execução dupla, calibração de um HTML com estrutura de DOM diferente e o servidor/UI.

## Problemas comuns

| Sintoma | O que fazer |
|---|---|
| "Chrome não encontrado" | instale o Chrome ou defina `chrome.executable` em `config/channels.json` |
| "Sessão do WeDrop não está autenticada" | `npm run login`, entre no painel nessa janela, rode de novo |
| Porta 9222 ocupada por outro app | troque `chrome.port` no config |
| "Não encontrei o seletor … Aplicar/Atualizar" | rode `npm run map` e abra `data/artifacts/*.html` para ver o DOM no momento da falha |
| Verificação demorando muito | normal em canais grandes (~2.400 itens); aumente `selectionTimeoutMs` se necessário |
