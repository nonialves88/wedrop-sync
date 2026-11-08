# Rotina agendada — 6h e 18h (America/Recife)

Duas formas. A primeira é a mais barata e a recomendada; a segunda existe para
quando o aviso precisa chegar numa conversa.

---

## A) Determinística (recomendada): timer + aviso pelo próprio script

Não usa LLM, não gasta token, não lê log no olho e não abre navegador. Quem
decide "avisar ou ficar quieto" é `report.decide_alerts`, com números do
`state/last-run.json`.

```bash
sudo cp deploy/wedrop-sync.service deploy/wedrop-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wedrop-sync.timer
```

Aviso no celular/chat — acrescente **uma** destas linhas ao
`/etc/systemd/system/wedrop-sync.service` (seção `[Service]`) e rode
`sudo systemctl daemon-reload`:

```ini
Environment=WEDROP_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
# ou
Environment=WEDROP_NOTIFY_COMMAND=telegram-send --stdin
# ou
Environment=WEDROP_NOTIFY_FILE=/home/box/.wedrop/avisos.txt
```

Teste o destino antes de confiar nele:

```bash
python3 -m wedrop_sync notify-test --webhook https://exemplo/hook
```

O que dispara mensagem: falha de login, pedido de código por e-mail,
integração que perdeu autorização, canal abortado, pico de erro
(`≥15 erros` **e** `≥10%` do canal) e piora forte contra a rodada anterior.
Órfão, `under_review`, `closed` e erro isolado **não** avisam.

---

## B) Se o aviso precisa sair de uma rotina de agente (chat)

Texto da rotina, para colar em quem for executá-la. Repare que ele **não lê
log**: usa o `last-run.json` e o código de saída.

```
Verificação da sincronização de estoque do WeDrop (Shopee, ML1, ML2).

1. Não abra navegador e não clique no site.

2. Se existir processo de sincronização ativo (serviço wedrop-sync rodando ou
   `pgrep -f "wedrop_sync"`), não faça nada e fique em silêncio.

3. Rode:
   cd /home/box/.wedrop && python3 -m wedrop_sync run --quiet

4. Leia o código de saída:
   - 0  → tudo certo: fique em silêncio.
   - 4  → já havia rodada em andamento: fique em silêncio.
   - 3  → falha de autenticação: avise.
   - 2  → rodou, mas tem alerta: avise.
   - outro → avise com o final de last-run.log.

5. Se for avisar, leia /home/box/.wedrop/state/last-run.json e mande uma
   mensagem curta em pt-BR com: cada item de "alerts" (title + detail) e, por
   canal, selecionados / ok / erro / timeout. Cite os motivos mais comuns
   (campo "reasons") quando existirem.

6. Nunca inclua senha, token ou trecho de creds.env na mensagem.
```

Por que assim: uma execução do agente gasta **um comando** e, no caso normal,
nenhuma leitura de log. O script já fez o trabalho caro.

---

## Verificação manual rápida

```bash
systemctl list-timers wedrop-sync.timer          # próximo disparo
journalctl -u wedrop-sync -n 80 --no-pager       # última rodada
python3 -m wedrop_sync status                    # últimas rodadas (histórico)
tail -f /home/box/.wedrop/last-run.log           # acompanhar ao vivo
```

Se a rodada das 18h começar antes de a das 6h terminar, a segunda sai com
código `4` e não toca em nada — é a trava `.sync.lock` fazendo o trabalho.

---

## C) GitHub Actions (sem servidor, sem PC)

Workflow em `.github/workflows/sync.yml`, 06:00 e 18:00 de Recife. IDs vêm do
cofre cifrado e credenciais dos Secrets; veja o passo a passo no `README.md` da
raiz. O aviso por WhatsApp usa `deploy/zapi_notify.py` via `WEDROP_NOTIFY_COMMAND`.
O sincronizador precisa de `channels.json` (ver `config/channels.example.json`)
em `~/.wedrop/` ou em `WEDROP_CHANNELS_FILE`.
