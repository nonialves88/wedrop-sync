# Sincronização de Estoque WeDrop — Especificação Técnica e Histórico Funcional

## 1. Visão geral

**Problema:** o usuário atualizava o estoque manualmente, 3x ao dia, em cada uma das integrações ativas do painel WeDrop (dash.wedrop.com.br) — escolhendo o canal, selecionando os anúncios (todos ou só os com estoque baixo), aplicando o filtro e disparando a atualização em massa.

**Objetivo:** permitir acionar essa rotina com um único comando/clique, repetindo a sequência nos canais ativos sem repetir manualmente os passos em cada um.

**Status:** fluxo mapeado e executado com sucesso uma vez (teste real, ver seção 5). Botão de acionamento (HTML) publicado e entregue; teste final do botão pelo usuário ainda pendente.

---

## 2. Contexto operacional — painel WeDrop

- **URL base:** `https://dash.wedrop.com.br/`
- **Tela usada:** `https://dash.wedrop.com.br/my_items` ("Meus Anúncios")
- **Autenticação:** nenhuma credencial foi digitada por automação. A sessão já autenticada do usuário no Google Chrome (via extensão **Claude in Chrome**) foi reaproveitada. Isso é uma decisão de segurança, não uma limitação técnica contornável — ver seção 7.

---

## 3. Canais de venda mapeados

| Canal | Identificador interno | Status | Observação |
|---|---|---|---|
| Shopee | `10001` | Ativo | Tem processo próprio de sincronia contínua em segundo plano (ver seção 5.1) |
| Mercado Livre — conta 1 (LOJA-EXEMPLO) | `10002` | Ativo | |
| Mercado Livre — conta 2 (LOJA-EXEMPLO) | `10003` | Ativo | Mesma razão social de loja, conta distinta — confirmado pelo usuário |
| Bling | `10004` | **Inativo** | Excluído do fluxo por decisão do usuário; painel também o marca como "fora do ar" |

O seletor de canal ("Selecione o canal de venda", em `/my_items`) é um `<select>` nativo com essas 4 opções.

---

## 4. Fluxo técnico de sincronização (passo a passo)

Repetido individualmente para cada um dos 3 canais ativos:

1. Navegar para `https://dash.wedrop.com.br/my_items`.
2. No dropdown **"Selecione o canal de venda"**, escolher o canal pelo identificador (tabela acima).
3. A página recarrega o painel do canal, exibindo:
   - Cards **Total / Mapeados / Não mapeado**
   - Bloco **"Atualização de Estoque em Massa"**
4. No dropdown **"Seleção"**, escolher o critério:
   - `Nenhuma`
   - `Todos mapeados`
   - `Estoque baixo` — ao escolher, surge um campo numérico de limite (padrão **≤ 10** unidades)
   - `Customizado`
5. Clicar em **"Aplicar"**. Isso dispara uma verificação assíncrona item a item de todo o catálogo mapeado do canal, com barra de progresso (`X/Y (Z%)`) e contador parcial de itens encontrados. Ao final, exibe **"N selecionados"**.
   - Tempo observado: proporcional ao nº de itens mapeados, ritmo aproximado de 0,3–1 item/segundo.
6. Clicar no botão verde **"Atualizar"** (canto superior direito da seção).
7. Confirmar no modal **"Confirmar Atualização em Massa"**, que reexibe a contagem de anúncios e o filtro aplicado, clicando em **"Iniciar Atualização"**.
8. O job de atualização roda em segundo plano (indicador flutuante no canto inferior esquerdo, formato `ok/erro/fila/total`) e **continua mesmo que o usuário navegue para outro canal** — confirmado durante o teste: trocar de canal não interrompe o job anterior.

---

## 5. Execução de teste realizada (2026-10-20)

Critério usado nos 3 canais: **Estoque baixo (≤ 10 unidades)**, por escolha do usuário naquele teste.

| Canal | Total mapeado | Selecionados (estoque baixo) | Atualização |
|---|---|---|---|
| Shopee | 2273 | 855 | Iniciada com sucesso |
| Mercado Livre — conta 1 | 794 | 167 | Iniciada com sucesso |
| Mercado Livre — conta 2 | 2413 | 719 | Iniciada com sucesso |
| Bling | — | — | Excluído (inativo) |

### 5.1 Observação importante: processo paralelo no Shopee

Antes mesmo de qualquer ação manual, já havia um job contínuo rodando em segundo plano, rotulado **"Atualização de estoque — Shopee"**, ciclando por todos os 2273 itens mapeados. Tudo indica ser o recurso **WeDrop AutoPilot** (visível no menu lateral), independente do botão manual de "Atualização em Massa". Isso explica contadores grandes e crescimento lento (`572/2273`, etc.) que não devem ser confundidos com o resultado da ação manual dos 855 itens selecionados.

---

## 6. Decisões de produto (perguntas respondidas pelo usuário)

| Pergunta | Resposta do usuário |
|---|---|
| Critério de seleção varia entre as execuções diárias? | Sim, varia — perguntar a cada execução em vez de fixar um padrão |
| As duas entradas "LOJA-EXEMPLO (Mercado Livre)" são a mesma loja duplicada? | Não, são 2 contas/lojas distintas — tratar como 2 canais separados |
| Forma de acionar o fluxo | Botão/atalho acionado sob demanda (opção recomendada), não automação 100% silenciosa em horários fixos |
| Uso de múltiplos navegadores (Chrome/Opera/Edge) por integração | Confirmado que rodar sequencialmente dentro de uma única sessão do Chrome (trocando o canal) é suficiente — não precisa dos outros navegadores |
| Acionamento pelo iPhone | A escolha do critério e cópia do comando funcionam em qualquer navegador/dispositivo; a **execução real** exige a extensão Claude in Chrome ativa no notebook — não há equivalente no app do Claude para iOS. Usuário optou por seguir operando a execução pelo notebook |

---

## 7. Restrições de segurança aplicadas

- **Nenhuma credencial foi inserida por automação.** O print com usuário/senha enviado pelo usuário no início não foi usado — a navegação reaproveitou a sessão já logada no Chrome local via extensão Claude in Chrome. Essa é uma regra fixa de segurança, não contornável mesmo com autorização explícita do usuário.
- A ação de **atualização em massa de estoque** foi tratada como ação regular (não destrutiva de dados do usuário, mas com efeito visível publicamente nos anúncios), executada apenas após o usuário confirmar o critério desejado em cada rodada.
- Nenhum dado de login foi persistido em arquivo ou nesta documentação.

---

## 8. Ferramenta de acionamento (botão HTML)

- **Arquivo:** `wedrop_sync_trigger.html` (entregue ao usuário e salvo na pasta do projeto)
- **Publicado também como Claude Artifact** (URL privada, acessível de qualquer dispositivo logado na conta do usuário)
- **Funcionalidade:**
  - Seleção única (toggle) entre **"Estoque baixo"** e **"Todo o estoque"**, aplicada aos 3 canais ativos
  - Lista visual dos canais incluídos (3 ativos) e do Bling (marcado como desativado/ignorado)
  - Botão **"Preparar comando"**: gera o texto de instrução e tenta copiá-lo automaticamente para a área de transferência (`navigator.clipboard`), com fallback de seleção manual de texto caso o navegador bloqueie o acesso à área de transferência
- **Limitação arquitetural (por desenho, não contornável):** uma página web publicada não tem capacidade de acionar as ferramentas de automação de navegador do Claude Code diretamente — não existe, nas capacidades disponíveis para Artifacts, uma ponte que permita a uma página disparar uma sessão de agente. Por isso o botão **prepara e copia** o comando; a execução de fato só ocorre quando esse texto é colado na conversa do Claude Code rodando no notebook, onde a extensão Claude in Chrome está conectada à sessão autenticada do WeDrop.

---

## 9. Limitações conhecidas

- Execução real depende de uma sessão do Claude Code ativa **no notebook**, com a extensão Claude in Chrome conectada e o Chrome logado no WeDrop.
- Não há acionamento 100% remoto/automático sem presença humana para colar o comando — decisão deliberada para evitar dependência de credenciais armazenadas ou de agentes em nuvem sem acesso à sessão local.
- O indicador de progresso do Shopee pode confundir por conta do processo paralelo do WeDrop AutoPilot (seção 5.1).
- Tempo de verificação do filtro "Estoque baixo"/"Todos mapeados" escala com o volume de itens mapeados do canal (minutos para canais grandes, como os ~2.400 itens do Mercado Livre conta 2).

---

## 10. Próximos passos sugeridos

- [ ] Usuário testar o botão publicado em uso real (pendente desde 2026-10-20)
- [ ] Decidir se vale fixar um critério padrão (ex.: sempre "Estoque baixo") para eliminar a pergunta a cada execução
- [ ] Avaliar se compensa configurar um agendamento (3x/dia) para lembrar o usuário de rodar o fluxo, mantendo a confirmação manual no chat

---

# Adendo (v2) — automação local, sem copiar/colar e sem LLM no meio

**Data:** 2026-10-25 · **Substitui a seção 8** (botão que só preparava o comando).

As seções 1–7 continuam válidas como mapeamento do fluxo (canais, ids, critérios, tempos, restrição de não usar credencial). O que mudou é **como a execução acontece**.

### 10.1 O que muda

| Antes (v1) | Agora (v2) |
|---|---|
| Página HTML gerava texto → usuário colava no chat do Claude | App local (`npm start`) com botão **Sincronizar agora** |
| Dependia de extensão Claude in Chrome + sessão de agente | Playwright (`playwright-core`) controlando o Chrome via **CDP**, sem download de navegador |
| Um comando por vez, colado manualmente | 3 canais em sequência numa execução só, com progresso ao vivo |
| Sem histórico, sem dry-run, sem retry | Dry-run, progresso por canal (X/Y (Z%)), retry automático, cancelamento, histórico em `data/runs.jsonl` |
| Dependente de o LLM interpretar a página | Seletores determinísticos por id interno / textos das opções, com marcação `data-wd-sync` para sobreviver a re-render |

### 10.2 Autenticação (regra preservada)

Nada de credencial em arquivo continua valendo: o app abre um **Chrome dedicado** com perfil próprio (`data/chrome-profile`) e porta de depuração; o login é feito **manualmente uma vez** (`npm run login`) e a sessão fica nos cookies do próprio Chrome. O app nunca digita, lê ou persiste usuário/senha.

### 10.3 Execução validada nesta versão

- Testes automatizados (Chrome headless + painel simulado): 10/10 — dry-run nos 3 canais, "todos mapeados", execução completa com confirmação do modal, retry após falha, interrupção por sessão expirada, bloqueio de execução dupla, calibração de DOM alternativo, servidor/UI.
- Execução de ponta a ponta pelo caminho de produção (Chrome real + CDP + fluxo completo em 2 canais) concluída com status `ok`.

### 10.4 Pendência para o primeiro uso real

Os seletores foram escritos de forma adaptativa, mas **ainda não foram exercitados contra o DOM real do WeDrop** (as sessões automatizadas acima usaram um painel simulado). Para fechar isso, basta:

1. abrir `/my_items` logado, com um canal carregado;
2. F12 → Elements → botão direito em `<html>` → Copy → Copy outerHTML;
3. salvar como `data/my_items.html` e rodar `npm run calibrate -- data/my_items.html`.

O relatório mostra o que foi reconhecido (canal, critério, bloco, Aplicar, Atualizar, campo de limite) e o que precisa de ajuste — o ajuste fica concentrado nas funções `find*` de `src/lib/wedrop.js`.

