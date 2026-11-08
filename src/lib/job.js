import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { DATA_DIR, ensureDataDir, loadConfig, resolveChannels, CRITERIA } from './config.js';
import { logger } from './log.js';
import { getBrowser, getDashboardPage, LoginRequiredError } from './chrome.js';
import { syncChannel, captureFailure, CancelledError } from './wedrop.js';

const MAX_EVENTS = 600;

/** O arquivo de histórico pode ser redirecionado por env (usado nos testes). */
function historyFile() {
  return process.env.WEDROP_HISTORY_FILE || path.join(DATA_DIR, 'runs.jsonl');
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class Runner {
  constructor(deps = {}) {
    this.bus = new EventEmitter();
    this.bus.setMaxListeners(100);
    this.current = null;
    this.cancelRequested = false;
    // Injeção de dependências: os testes trocam Chrome/automação por dublês.
    this.deps = { getBrowser, getDashboardPage, syncChannel, captureFailure, ...deps };
  }

  get running() {
    return Boolean(this.current && this.current.running);
  }

  snapshot() {
    return this.current ? JSON.parse(JSON.stringify(this.current)) : null;
  }

  on(fn) {
    this.bus.on('event', fn);
    return () => this.bus.off('event', fn);
  }

  emit(event) {
    const payload = { ts: Date.now(), runId: this.current?.id ?? null, ...event };
    if (this.current) {
      this.current.events.push(payload);
      if (this.current.events.length > MAX_EVENTS) this.current.events.splice(0, this.current.events.length - MAX_EVENTS);
    }
    this.bus.emit('event', payload);
    if (payload.type === 'log' && payload.level === 'error') logger.error(payload.message);
  }

  cancel() {
    if (!this.running) return false;
    this.cancelRequested = true;
    this.emit({ type: 'log', level: 'warn', message: 'Cancelamento solicitado — vou parar no próximo passo seguro.' });
    return true;
  }

  async start(options = {}) {
    if (this.running) {
      const err = new Error('Já existe uma sincronia em andamento. Espere terminar (ou cancele) antes de iniciar outra.');
      err.code = 'ALREADY_RUNNING';
      throw err;
    }

    const config = options.config ?? loadConfig({ fresh: true });
    const criterionDef = CRITERIA[options.criterion] ?? CRITERIA[config.run.criterion] ?? CRITERIA.low;
    const requestedLimit = Number(options.max);
    const limit = Number.isFinite(requestedLimit) && requestedLimit > 0 ? Math.floor(requestedLimit) : config.run.lowStockMax;
    const dryRun = options.dryRun === undefined ? config.run.dryRun : Boolean(options.dryRun);
    const channels = resolveChannels(options.channels, config);

    this.cancelRequested = false;
    const run = {
      id: crypto.randomUUID(),
      startedAt: Date.now(),
      finishedAt: null,
      running: true,
      status: 'running',
      criterion: criterionDef.key,
      criterionLabel: criterionDef.label,
      limit: criterionDef.needsLimit ? limit : null,
      dryRun,
      events: [],
      targets: channels.map((c) => ({
        key: c.key,
        name: c.name,
        id: c.id,
        note: c.note,
        status: 'pending',
        selected: null,
        checked: null,
        mappedTotal: null,
        progress: null,
        attempts: 0,
        error: null,
        startedAt: null,
        finishedAt: null,
        durationMs: null,
      })),
    };
    this.current = run;

    this.emit({
      type: 'run',
      phase: 'start',
      criterion: run.criterion,
      criterionLabel: run.criterionLabel,
      limit: run.limit,
      dryRun,
      targets: run.targets.map((t) => ({ key: t.key, name: t.name })),
      message: `Iniciando sincronia: ${run.criterionLabel}${run.limit ? ` (≤ ${run.limit})` : ''} em ${run.targets.length} canal(is)${dryRun ? ' — modo dry-run' : ''}.`,
    });

    this.execute(run, config).catch((err) => {
      logger.error('Falha inesperada na execução', { message: err?.message });
      run.running = false;
      run.status = 'error';
      run.error = err?.message ?? String(err);
      run.finishedAt = Date.now();
      this.emit({ type: 'run', phase: 'finish', status: 'error', message: `Erro inesperado: ${run.error}` });
      this.persist(run);
    });

    return this.snapshot();
  }

  async execute(run, config) {
    let browser = null;
    const channelsByKey = new Map(config.channels.map((c) => [c.key, c]));
    const { getBrowser: connect, getDashboardPage: openDashboard, syncChannel: runChannel, captureFailure: grabArtifacts } = this.deps;
    try {
      this.emit({ type: 'log', message: 'Conectando ao Chrome (abrindo a janela dedicada se necessário)…' });
      browser = await connect({ onLog: (m) => this.emit({ type: 'log', message: m }) });
      const page = await openDashboard(browser, config.dashboardUrl);

      for (let i = 0; i < run.targets.length; i += 1) {
        const target = run.targets[i];
        const channel = channelsByKey.get(target.key);

        if (this.cancelRequested) {
          target.status = 'canceled';
          target.finishedAt = Date.now();
          continue;
        }

        const maxAttempts = Math.max(1, Number(config.run.retries) + 1);
        let attempt = 0;
        let succeeded = false;

        while (attempt < maxAttempts && !succeeded) {
          attempt += 1;
          target.attempts = attempt;
          target.status = attempt === 1 ? 'running' : 'retrying';
          target.startedAt = Date.now();
          target.error = null;
          this.emit({
            type: 'channel',
            phase: 'attempt',
            channel: target.key,
            name: target.name,
            attempt,
            maxAttempts,
            message: attempt === 1 ? `▶ ${target.name}: começando.` : `↻ ${target.name}: tentativa ${attempt} de ${maxAttempts}.`,
          });

          try {
            const result = await runChannel(page, channel, {
              config,
              criterionKey: run.criterion,
              max: run.limit,
              dryRun: run.dryRun,
              isCancelled: () => this.cancelRequested,
              onEvent: (ev) => this.handleChannelEvent(run, target, ev),
            });

            target.status = run.dryRun ? 'dry-run' : 'started';
            target.selected = result.selected;
            target.checked = result.checked ?? null;
            target.mappedTotal = result.mappedTotal ?? null;
            target.durationMs = result.durationMs;
            target.finishedAt = Date.now();
            target.progress = null;
            succeeded = true;

            this.emit({
              type: 'channel',
              phase: 'done',
              channel: target.key,
              status: target.status,
              selected: target.selected,
              checked: target.checked,
              durationMs: target.durationMs,
              message: run.dryRun
                ? `✓ ${target.name}: ${target.selected} anúncio(s) selecionados (dry-run, nada alterado).`
                : `✓ ${target.name}: atualização iniciada com ${target.selected} anúncio(s).`,
            });
          } catch (err) {
            if (err instanceof CancelledError) {
              target.status = 'canceled';
              target.finishedAt = Date.now();
              this.emit({ type: 'channel', phase: 'done', channel: target.key, status: 'canceled', message: `⏹ ${target.name}: cancelado.` });
              break;
            }

            target.status = 'error';
            target.error = err?.message ?? String(err);
            target.finishedAt = Date.now();
            const artifact = await grabArtifacts(page, `${target.key}-tentativa${attempt}`);
            logger.error(`Canal ${target.name} falhou (tentativa ${attempt})`, { error: target.error, artifact });

            const retryable = !(err instanceof LoginRequiredError) && attempt < maxAttempts && !this.cancelRequested;
            this.emit({
              type: 'error',
              channel: target.key,
              name: target.name,
              message: `✗ ${target.name}: ${target.error}`,
              artifact,
              willRetry: retryable,
            });

            if (!retryable) {
              if (err instanceof LoginRequiredError) {
                this.cancelRequested = true;
                this.emit({
                  type: 'log',
                  level: 'error',
                  message: 'Sessão do WeDrop não autenticada. Faça login na janela do Chrome (npm run login) e rode de novo. Interrompendo os canais restantes.',
                });
              }
              break;
            }

            this.emit({
              type: 'log',
              level: 'warn',
              message: `Tentando ${target.name} novamente em ${Math.round(config.run.retryDelayMs / 1000)}s…`,
            });
            await sleep(config.run.retryDelayMs);
          }
        }

        const isLast = i === run.targets.length - 1;
        if (!isLast && !this.cancelRequested) await sleep(config.run.betweenChannelsMs);
      }

      run.status = this.cancelRequested ? 'canceled' : run.targets.some((t) => t.status === 'error') ? 'partial' : 'ok';
      run.running = false;
      run.finishedAt = Date.now();
      const summary = this.summarize(run);
      this.emit({ type: 'run', phase: 'finish', status: run.status, summary, message: summary.text });
      this.persist(run);
    } catch (err) {
      run.running = false;
      run.status = this.cancelRequested ? 'canceled' : 'error';
      run.error = err?.message ?? String(err);
      run.finishedAt = Date.now();
      this.emit({ type: 'run', phase: 'finish', status: run.status, message: `Falhou: ${run.error}` });
      this.persist(run);
    }
  }

  handleChannelEvent(run, target, event) {
    if (event.type === 'selection-progress') {
      target.progress = { done: event.done, total: event.total, pct: event.pct, selected: event.selected };
    }
    if (event.type === 'channel' && event.phase === 'selected') {
      target.checked = event.checked ?? target.checked;
      target.mappedTotal = event.mappedTotal ?? target.mappedTotal;
    }
    this.emit(event);
  }

  summarize(run) {
    const done = run.targets.filter((t) => t.status === 'started' || t.status === 'dry-run');
    const failed = run.targets.filter((t) => t.status === 'error');
    const total = done.reduce((sum, t) => sum + (t.selected ?? 0), 0);
    const verb = run.dryRun ? 'selecionados' : 'atualizações iniciadas';
    const parts = [];
    parts.push(`${done.length}/${run.targets.length} canais ok`);
    if (run.dryRun) parts.push(`${total} anúncio(s) ${verb}`);
    else parts.push(`${total} anúncio(s) em atualização`);
    if (failed.length) parts.push(`${failed.length} com erro (${failed.map((f) => f.name).join(', ')})`);
    const dur = Math.round(((run.finishedAt ?? Date.now()) - run.startedAt) / 1000);
    const mm = `${Math.floor(dur / 60)}min${String(dur % 60).padStart(2, '0')}s`;
    return { text: `Sincronia ${run.status}: ${parts.join(' · ')} · ${mm}`, totalSelected: total, durationSec: dur };
  }

  persist(run) {
    try {
      ensureDataDir();
      const record = {
        id: run.id,
        startedAt: new Date(run.startedAt).toISOString(),
        finishedAt: run.finishedAt ? new Date(run.finishedAt).toISOString() : null,
        status: run.status,
        criterion: run.criterion,
        limit: run.limit,
        dryRun: run.dryRun,
        error: run.error ?? null,
        targets: run.targets.map((t) => ({
          key: t.key,
          name: t.name,
          status: t.status,
          selected: t.selected,
          checked: t.checked,
          attempts: t.attempts,
          durationMs: t.durationMs,
          error: t.error,
        })),
      };
      fs.appendFileSync(historyFile(), JSON.stringify(record) + '\n', 'utf8');
    } catch (err) {
      logger.warn('Não consegui gravar o histórico', { message: err?.message });
    }
  }

  history(limit = 25) {
    try {
      if (!fs.existsSync(historyFile())) return [];
      const lines = fs.readFileSync(historyFile(), 'utf8').split('\n').filter(Boolean);
      return lines
        .slice(-limit)
        .map((line) => {
          try {
            return JSON.parse(line);
          } catch {
            return null;
          }
        })
        .filter(Boolean)
        .reverse();
    } catch {
      return [];
    }
  }
}

export const runner = new Runner();
