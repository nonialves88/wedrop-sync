/* Sincronizador de estoque WeDrop — UI local */
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const STORE_KEY = 'wedrop-sync:prefs';

  const state = {
    config: null,
    diagnostics: null,
    criterion: 'low',
    limit: 10,
    dryRun: false,
    selectedChannels: new Set(),
    targets: [],
    logLines: [],
    running: false,
    history: [],
  };

  const STATUS_LABEL = {
    pending: 'na fila',
    running: 'rodando',
    retrying: 'retentando',
    'dry-run': 'dry-run',
    started: 'atualização iniciada',
    error: 'erro',
    canceled: 'cancelado',
  };

  function prefs() {
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY) ?? '{}') ?? {};
    } catch {
      return {};
    }
  }

  function savePrefs() {
    try {
      localStorage.setItem(
        STORE_KEY,
        JSON.stringify({
          criterion: state.criterion,
          limit: state.limit,
          dryRun: state.dryRun,
          channels: [...state.selectedChannels],
        }),
      );
    } catch {
      /* localStorage pode estar bloqueado; ignora */
    }
  }

  function fmtTime(ts) {
    const d = new Date(ts ?? Date.now());
    return d.toLocaleTimeString('pt-BR', { hour12: false });
  }

  function fmtDuration(ms) {
    if (!ms) return '';
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    return `${Math.floor(s / 60)}min${String(s % 60).padStart(2, '0')}s`;
  }

  function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return `${d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' })} ${d.toLocaleTimeString('pt-BR', {
      hour: '2-digit',
      minute: '2-digit',
    })}`;
  }

  /* ------------------------------- render --------------------------------- */

  function renderChannels() {
    const host = $('#channels');
    host.innerHTML = '';
    const active = new Set(state.config?.activeChannels ?? []);
    for (const ch of state.config?.channels ?? []) {
      const isActive = active.has(ch.key);
      const label = document.createElement('label');
      label.className = `channel${isActive ? '' : ' off'}`;
      label.title = ch.note || '';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.disabled = !isActive;
      input.checked = isActive && state.selectedChannels.has(ch.key);
      input.addEventListener('change', () => {
        if (input.checked) state.selectedChannels.add(ch.key);
        else state.selectedChannels.delete(ch.key);
        savePrefs();
        updateRunAvailability();
      });
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = ch.name;
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = isActive ? ch.id : 'desativado';
      label.append(input, name, tag);
      host.append(label);
    }
  }

  function renderLimitRow() {
    $('#limit-row').hidden = state.criterion !== 'low';
    $('#limit-input').value = state.limit;
  }

  function updateRunAvailability() {
    const btn = $('#run-btn');
    const anyChannel = state.selectedChannels.size > 0;
    btn.disabled = state.running || !anyChannel;
    $('#cancel-btn').disabled = !state.running;
    $('#run-note').textContent = state.running
      ? 'Rodando — pode minimizar esta aba, a sincronia continua.'
      : anyChannel
        ? ''
        : 'Selecione ao menos um canal.';
  }

  function renderTargets() {
    const host = $('#targets');
    if (!state.targets.length) {
      host.innerHTML = '';
      return;
    }
    host.innerHTML = '';
    for (const t of state.targets) {
      const wrap = document.createElement('div');
      wrap.className = `target ${t.status}`;

      const top = document.createElement('div');
      top.className = 'target-top';

      const name = document.createElement('div');
      name.className = 'target-name';
      name.textContent = t.name;

      const badge = document.createElement('span');
      badge.className = `badge ${t.status}`;
      badge.textContent = STATUS_LABEL[t.status] ?? t.status;
      name.append(badge);
      top.append(name);

      const count = document.createElement('span');
      count.className = 'target-count';
      count.textContent = targetCountText(t);
      top.append(count);

      const bar = document.createElement('div');
      bar.className = 'bar';
      const fill = document.createElement('div');
      fill.className = 'bar-fill';
      fill.style.width = `${targetPct(t)}%`;
      bar.append(fill);

      const msg = document.createElement('div');
      msg.className = `target-msg${t.error ? ' err' : ''}`;
      msg.textContent = t.error ? t.error : targetMsg(t);

      wrap.append(top, bar, msg);
      host.append(wrap);
    }
  }

  function targetPct(t) {
    if (t.selected != null && (t.status === 'started' || t.status === 'dry-run')) return 100;
    if (t.progress?.pct != null) return Math.min(100, t.progress.pct);
    if (t.status === 'running' || t.status === 'retrying') return 6;
    if (t.status === 'error') return 100;
    return 0;
  }

  function targetCountText(t) {
    if (t.selected != null) return `${t.selected} anúncio(s)`;
    if (t.progress?.total) return `${t.progress.done ?? 0}/${t.progress.total} verificados`;
    if (t.checked != null) return `${t.checked} verificados`;
    return '';
  }

  function targetMsg(t) {
    if (t.status === 'pending') return 'Aguardando a vez.';
    if (t.status === 'running') {
      if (t.progress?.total) return `Aplicando filtro: ${t.progress.pct ?? 0}% (${t.progress.done}/${t.progress.total}).`;
      if (t.attempts > 1) return `Tentativa ${t.attempts}.`;
      return 'Abrindo canal, aplicando o filtro…';
    }
    if (t.status === 'retrying') return `Tentativa ${t.attempts} após erro.`;
    if (t.status === 'started') return `Atualização em lote enviada ao WeDrop${t.durationMs ? ` em ${fmtDuration(t.durationMs)}` : ''}. O painel continua processando em segundo plano.`;
    if (t.status === 'dry-run') return `Dry-run concluído: nada foi alterado${t.durationMs ? ` (${fmtDuration(t.durationMs)})` : ''}.`;
    if (t.status === 'canceled') return 'Cancelado antes de concluir.';
    if (t.status === 'error') return 'Falhou — veja o log abaixo.';
    return '';
  }

  function renderLog() {
    const host = $('#log');
    host.innerHTML = '';
    for (const line of state.logLines.slice(-180)) {
      const div = document.createElement('div');
      if (line.level && line.level !== 'info') div.className = `l-${line.level}`;
      const time = document.createElement('span');
      time.className = 'l-time';
      time.textContent = fmtTime(line.ts);
      div.append(time, document.createTextNode(line.text));
      host.append(div);
    }
    host.scrollTop = host.scrollHeight;
  }

  function renderHistory() {
    const host = $('#history');
    const rows = state.history ?? [];
    $('#history-panel').hidden = rows.length === 0;
    host.innerHTML = '';
    for (const run of rows) {
      const row = document.createElement('div');
      row.className = 'hrow';

      const when = document.createElement('span');
      when.className = 'when';
      when.textContent = fmtDate(run.finishedAt ?? run.startedAt);

      const detail = document.createElement('span');
      detail.className = 'detail';
      const okCount = (run.targets ?? []).filter((t) => t.status === 'started' || t.status === 'dry-run').length;
      const totalSelected = (run.targets ?? []).reduce((sum, t) => sum + (t.selected ?? 0), 0);
      const errors = (run.targets ?? []).filter((t) => t.status === 'error').map((t) => t.name);
      detail.innerHTML = `<strong>${okCount}/${(run.targets ?? []).length}</strong> canais · ${
        run.dryRun ? 'dry-run' : 'iniciadas'
      } · ${totalSelected} anúncio(s) · ${run.criterion === 'low' ? `≤ ${run.limit}` : 'todos'}${
        errors.length ? ` · <span style="color:var(--err)">erro: ${errors.join(', ')}</span>` : ''
      }`;

      const badge = document.createElement('span');
      badge.className = `badge ${run.status === 'ok' ? 'started' : run.status === 'partial' ? 'canceled' : 'error'}`;
      badge.textContent = run.status;

      row.append(when, detail, badge);
      host.append(row);
    }
  }

  function renderAll() {
    renderChannels();
    renderLimitRow();
    renderTargets();
    renderLog();
    renderHistory();
    updateRunAvailability();
    renderPill();
    renderDiagnostics();
  }

  function renderPill() {
    const pill = $('#pill-status');
    const chromePill = $('#pill-chrome');
    if (state.running) {
      pill.dataset.state = 'running';
      pill.textContent = 'Rodando…';
    } else if (state.lastStatus === 'ok') {
      pill.dataset.state = 'ok';
      pill.textContent = 'Concluído';
    } else if (state.lastStatus === 'partial' || state.lastStatus === 'canceled') {
      pill.dataset.state = 'partial';
      pill.textContent = state.lastStatus === 'canceled' ? 'Cancelado' : 'Concluído com erros';
    } else if (state.lastStatus === 'error') {
      pill.dataset.state = 'error';
      pill.textContent = 'Falhou';
    } else {
      pill.dataset.state = 'idle';
      pill.textContent = 'Pronto';
    }

    if (state.diagnostics) {
      const ok = state.diagnostics.chromeFound;
      chromePill.dataset.state = ok ? 'ok-chrome' : 'error';
      chromePill.textContent = ok
        ? `Chrome ${state.diagnostics.chromePortOpen ? 'conectado' : 'disponível'}`
        : 'Chrome não encontrado';
    }
  }

  function renderDiagnostics() {
    const d = state.diagnostics;
    if (!d) return;
    $('#diagnostics').textContent = `${d.node} · ${d.chromePath ?? 'chrome não localizado'} · porta ${d.chromePort}${
      d.chromePortOpen ? ' (aberta)' : ' (fechada — abre ao sincronizar)'
    }`;
  }

  function logLine(text, level = 'info', ts = Date.now()) {
    state.logLines.push({ text, level, ts });
    if (state.logLines.length > 400) state.logLines.splice(0, state.logLines.length - 400);
  }

  /* ------------------------------- eventos -------------------------------- */

  function applySnapshot(snapshot) {
    if (!snapshot) {
      state.running = false;
      state.targets = [];
      return;
    }
    state.running = Boolean(snapshot.running);
    state.criterion = snapshot.criterion === 'all' ? 'all' : 'low';
    if (snapshot.limit) state.limit = snapshot.limit;
    state.dryRun = Boolean(snapshot.dryRun);
    state.targets = (snapshot.targets ?? []).map((t) => ({ ...t }));
    state.lastStatus = snapshot.running ? undefined : snapshot.status;
    if (snapshot.events?.length) {
      state.logLines = snapshot.events
        .filter((e) => e.message)
        .map((e) => ({ text: e.message, level: e.level ?? 'info', ts: e.ts }));
    }
    if (snapshot.running) {
      $('#live-panel').hidden = false;
      $('#run-meta').textContent = `${snapshot.criterionLabel}${snapshot.limit ? ` ≤ ${snapshot.limit}` : ''}${
        snapshot.dryRun ? ' · dry-run' : ''
      } · início ${fmtTime(snapshot.startedAt)}`;
    }
    syncControlsFromSnapshot();
  }

  function syncControlsFromSnapshot() {
    for (const btn of document.querySelectorAll('.choice button')) {
      btn.setAttribute('aria-pressed', btn.dataset.criterion === state.criterion ? 'true' : 'false');
    }
    $('#dry-run').checked = state.dryRun;
  }

  function handleEvent(ev) {
    if (ev.type === 'snapshot') {
      applySnapshot(ev.snapshot);
      renderAll();
      return;
    }

    if (ev.type === 'run') {
      if (ev.phase === 'start') {
        state.running = true;
        state.lastStatus = undefined;
        state.logLines = [];
        state.targets = (ev.targets ?? []).map((t) => ({
          key: t.key,
          name: t.name,
          status: 'pending',
          selected: null,
          checked: null,
          progress: null,
          attempts: 0,
          error: null,
        }));
        $('#live-panel').hidden = false;
        $('#run-meta').textContent = `${ev.criterionLabel}${ev.limit ? ` ≤ ${ev.limit}` : ''}${ev.dryRun ? ' · dry-run' : ''} · início ${fmtTime(ev.ts)}`;
        logLine(ev.message ?? 'Iniciando.', 'info', ev.ts);
      } else if (ev.phase === 'finish') {
        state.running = false;
        state.lastStatus = ev.status;
        logLine(ev.message ?? 'Fim.', ev.status === 'ok' ? 'ok' : ev.status === 'error' ? 'error' : 'warn', ev.ts);
        refreshState();
      }
      renderAll();
      return;
    }

    if (ev.type === 'channel') {
      const target = state.targets.find((t) => t.key === ev.channel);
      if (target) {
        if (ev.phase === 'attempt') {
          target.status = ev.attempt > 1 ? 'retrying' : 'running';
          target.attempts = ev.attempt;
          target.progress = null;
        } else if (ev.phase === 'selected') {
          target.checked = ev.checked ?? target.checked;
          target.mappedTotal = ev.mappedTotal ?? target.mappedTotal;
        } else if (ev.phase === 'started') {
          target.selected = ev.selected ?? target.selected;
        } else if (ev.phase === 'done') {
          target.status = ev.status ?? (ev.selected != null ? 'started' : target.status);
          if (ev.selected != null) target.selected = ev.selected;
          if (ev.checked != null) target.checked = ev.checked;
          if (ev.durationMs) target.durationMs = ev.durationMs;
          target.progress = null;
        }
      }
      if (ev.message) {
        const level = ev.status === 'error' ? 'error' : ev.status === 'canceled' ? 'warn' : ev.phase === 'done' ? 'ok' : 'info';
        logLine(ev.message, level, ev.ts);
      }
      renderTargets();
      renderLog();
      return;
    }

    if (ev.type === 'selection-progress') {
      const target = state.targets.find((t) => t.key === ev.channel);
      if (target) {
        target.status = 'running';
        target.progress = { done: ev.done, total: ev.total, pct: ev.pct, selected: ev.selected };
        renderTargets();
      }
      return;
    }

    if (ev.type === 'error') {
      const target = state.targets.find((t) => t.key === ev.channel);
      if (target) {
        target.status = 'error';
        target.error = ev.message;
        renderTargets();
      }
      logLine(ev.message ?? 'Erro.', 'error', ev.ts);
      renderLog();
      return;
    }

    if (ev.type === 'log') {
      const level = ev.level === 'error' ? 'error' : ev.level === 'warn' ? 'warn' : 'info';
      logLine(ev.message, level, ev.ts);
      renderLog();
    }
  }

  function connectSse() {
    if (!window.EventSource) return;
    const es = new EventSource('/api/events');
    es.onmessage = (e) => {
      try {
        handleEvent(JSON.parse(e.data));
      } catch {
        /* ignora payload inválido */
      }
    };
    es.onerror = () => {
      /* EventSource reconecta sozinho */
    };
  }

  /* ------------------------------- ações ---------------------------------- */

  async function refreshState() {
    try {
      const res = await fetch('/api/state', { cache: 'no-store' });
      const data = await res.json();
      state.config = data.config;
      state.diagnostics = data.diagnostics;
      state.history = data.history ?? [];
      if (!state.targets.length) applySnapshot(data.snapshot);
      else if (data.snapshot) {
        state.running = Boolean(data.snapshot.running);
        if (data.snapshot.targets?.length === state.targets.length) {
          state.targets = data.snapshot.targets.map((t) => ({ ...t }));
        }
      }
      if (!state.userTouchedCriterion) state.criterion = data.config?.criterion === 'all' ? 'all' : 'low';
      if (!state.userTouchedLimit) state.limit = data.config?.lowStockMax ?? 10;
      renderAll();
      applyPrefsOnce(data.config);
    } catch (err) {
      logLine(`Não consegui falar com o servidor local: ${err.message}`, 'error');
      renderLog();
    }
  }

  function applyPrefsOnce(config) {
    if (state.prefsApplied) return;
    state.prefsApplied = true;
    const p = prefs();
    const active = new Set(config?.activeChannels ?? []);
    if (p.criterion === 'all' || p.criterion === 'low') {
      state.criterion = p.criterion;
      state.userTouchedCriterion = true;
    }
    if (Number(p.limit) > 0) {
      state.limit = Number(p.limit);
      state.userTouchedLimit = true;
    }
    if (typeof p.dryRun === 'boolean') state.dryRun = p.dryRun;
    if (Array.isArray(p.channels) && p.channels.length) {
      state.selectedChannels = new Set(p.channels.filter((k) => active.has(k)));
    }
    if (!state.selectedChannels.size) state.selectedChannels = new Set(active);
    renderAll();
  }

  async function startRun() {
    if (state.running) return;
    const payload = {
      criterion: state.criterion,
      max: state.limit,
      dryRun: state.dryRun,
      channels: [...state.selectedChannels],
    };
    $('#run-btn').disabled = true;
    try {
      const res = await fetch('/api/run', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? 'Falha ao iniciar.');
      savePrefs();
    } catch (err) {
      logLine(`Não consegui iniciar: ${err.message}`, 'error');
      renderLog();
      $('#live-panel').hidden = false;
    } finally {
      updateRunAvailability();
    }
  }

  async function cancelRun() {
    try {
      await fetch('/api/cancel', { method: 'POST' });
    } catch (err) {
      logLine(`Falha ao cancelar: ${err.message}`, 'error');
      renderLog();
    }
  }

  /* ------------------------------- bind ----------------------------------- */

  function bind() {
    document.querySelectorAll('.choice button').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.criterion = btn.dataset.criterion;
        state.userTouchedCriterion = true;
        document.querySelectorAll('.choice button').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
        renderLimitRow();
        savePrefs();
      });
    });

    $('#limit-input').addEventListener('input', (e) => {
      const value = Number(e.target.value);
      if (Number.isFinite(value) && value > 0) {
        state.limit = Math.floor(value);
        state.userTouchedLimit = true;
        savePrefs();
      }
    });

    $('#dry-run').addEventListener('change', (e) => {
      state.dryRun = e.target.checked;
      savePrefs();
    });

    $('#run-btn').addEventListener('click', startRun);
    $('#cancel-btn').addEventListener('click', cancelRun);
    $('#refresh-history').addEventListener('click', refreshState);
  }

  bind();
  connectSse();
  refreshState();
  setInterval(() => {
    if (!state.running) refreshState();
  }, 30000);
})();
