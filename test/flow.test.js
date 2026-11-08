import { describe, test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

import { findChromeExecutable, LoginRequiredError } from '../src/lib/chrome.js';
import { parseSelectionState, parseMappedTotal, parseAnnouncementCount, syncChannel, mapDashboard } from '../src/lib/wedrop.js';
import { Runner } from '../src/lib/job.js';

const MOCK_HTML = path.resolve(fileURLToPath(new URL('../mock/index.html', import.meta.url)));
const FIXTURE_HTML = path.resolve(fileURLToPath(new URL('./fixtures/my_items.html', import.meta.url)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function runDefaults(extra = {}) {
  return {
    criterion: 'low',
    lowStockMax: 10,
    dryRun: false,
    channelDelayMs: 120,
    betweenChannelsMs: 0,
    retries: 1,
    retryDelayMs: 400,
    stepTimeoutMs: 20000,
    selectionTimeoutMs: 60000,
    updateStartTimeoutMs: 15000,
    keepChromeOpen: false,
    ...extra,
  };
}

function mockConfig(dashboardUrl, extraRun = {}) {
  return {
    dashboardUrl,
    chrome: {
      port: 9222,
      profileDir: path.join(os.tmpdir(), 'wedrop-test-profile'),
      executable: '',
      startUrl: dashboardUrl,
    },
    run: runDefaults(extraRun),
    server: { host: '127.0.0.1', port: 0 },
    channels: [
      { key: 'shopee', name: 'Shopee', id: '10001', aliases: [], active: true, note: '' },
      { key: 'ml-1', name: 'Mercado Livre — conta 1', id: '10002', aliases: [], active: true, note: '' },
      { key: 'ml-2', name: 'Mercado Livre — conta 2', id: '10003', aliases: [], active: true, note: '' },
      { key: 'bling', name: 'Bling', id: '10004', aliases: [], active: false, note: '' },
    ],
  };
}

async function waitForFinish(runner, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (runner.running && Date.now() < deadline) await sleep(150);
  assert.equal(runner.running, false, 'a execução não terminou dentro do tempo esperado');
  return runner.snapshot();
}

/* ----------------------------- parsing (unidade) -------------------------- */

describe('parsing do painel', () => {
  test('parseSelectionState entende progresso e contagem', () => {
    const progresso = parseSelectionState('Verificando 572/2273 (25%)');
    assert.deepEqual(progresso.progress, { done: 572, total: 2273, pct: 25 });
    assert.equal(progresso.selected, null);

    const concluido = parseSelectionState('855 selecionados');
    assert.equal(concluido.selected, 855);
    assert.equal(concluido.progress, null);

    assert.equal(parseSelectionState('1.234 selecionados').selected, 1234);
    assert.equal(parseSelectionState('Seleção 10 Estoque baixo 719 selecionados').selected, 719);
    assert.equal(parseSelectionState('Erro ao aplicar seleção').errorish, true);
    assert.equal(parseSelectionState('Atualização de Estoque em Massa').errorish, false);
  });

  test('parseMappedTotal e parseAnnouncementCount extraem os totais', () => {
    assert.equal(parseMappedTotal('Total Mapeados Não mapeado 2.413 2.413 0'), 2413);
    assert.equal(parseMappedTotal('Mapeados: 794'), 794);
    assert.equal(parseMappedTotal('sem números aqui'), null);
    assert.equal(parseAnnouncementCount('855 anúncio(s) serão atualizados no canal Shopee'), 855);
  });
});

/* --------------------------- fluxo real no Chrome ------------------------- */

describe('automação do painel (Chrome headless + mock)', () => {
  let server;
  let baseUrl;
  let browser;
  let historyFile;

  before(async () => {
    historyFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'wedrop-test-')), 'runs.jsonl');
    process.env.WEDROP_HISTORY_FILE = historyFile;

    server = http.createServer((req, res) => {
      const url = new URL(req.url ?? '/', 'http://localhost');
      if (url.pathname === '/favicon.ico') return res.writeHead(204).end();
      if (url.pathname === '/' || url.pathname === '/my_items' || url.pathname === '/index.html') {
        return res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }).end(fs.readFileSync(MOCK_HTML));
      }
      return res.writeHead(404).end('não encontrado');
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    baseUrl = `http://127.0.0.1:${server.address().port}`;

    const executablePath = findChromeExecutable(process.env.WEDROP_CHROME_PATH ?? '');
    assert.ok(executablePath, 'O Chrome precisa estar instalado para rodar os testes de integração.');
    browser = await chromium.launch({ executablePath, headless: true, args: ['--disable-gpu'] });
  });

  after(async () => {
    await browser?.close().catch(() => {});
    await new Promise((resolve) => server?.close(resolve));
    if (historyFile) fs.rmSync(path.dirname(historyFile), { recursive: true, force: true });
  });

  async function newMockPage(query = '') {
    // browser.newPage() cria contexto+aba (browser.contexts() fica vazio num launch comum)
    const page = await browser.newPage();
    await page.goto(`${baseUrl}/${query ? `?${query}` : ''}`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => Boolean(window.__mock), null, { timeout: 10000 });
    return page;
  }

  test('dry-run: seleciona nos 3 canais ativos sem disparar atualização', async () => {
    const page = await newMockPage();
    const config = mockConfig(baseUrl);
    const events = [];
    try {
      for (const channel of config.channels.filter((c) => c.active)) {
        const result = await syncChannel(page, channel, {
          config,
          criterionKey: 'low',
          max: 10,
          dryRun: true,
          onEvent: (ev) => events.push(ev),
        });
        const expected = await page.evaluate((id) => window.__mock.expected(id, 'low', 10), channel.id);
        assert.ok(expected > 0, `o mock deveria ter itens de estoque baixo em ${channel.key}`);
        assert.equal(result.selected, expected, `canal ${channel.key}`);
        assert.equal(result.dryRun, true);
      }

      assert.equal(await page.evaluate(() => window.__mock.updates().length), 0, 'dry-run não pode iniciar atualização');
      assert.ok(events.some((e) => e.type === 'selection-progress'), 'deveria emitir progresso da verificação');
    } finally {
      await page.close();
    }
  });

  test('critério "Todos mapeados" seleciona o catálogo inteiro', async () => {
    const page = await newMockPage();
    const config = mockConfig(baseUrl);
    try {
      const channel = config.channels.find((c) => c.key === 'ml-2');
      const result = await syncChannel(page, channel, {
        config,
        criterionKey: 'all',
        max: 10,
        dryRun: true,
        onEvent: () => {},
      });
      const expected = await page.evaluate((id) => window.__mock.expected(id, 'all', 10), channel.id);
      assert.equal(result.selected, expected);
    } finally {
      await page.close();
    }
  });

  test('execução completa: aplica filtro, confirma o modal e inicia a atualização', async () => {
    const page = await newMockPage('speed=1');
    const config = mockConfig(baseUrl);
    try {
      const channel = config.channels.find((c) => c.key === 'ml-1');
      const result = await syncChannel(page, channel, {
        config,
        criterionKey: 'low',
        max: 5,
        dryRun: false,
        onEvent: () => {},
      });

      const expected = await page.evaluate(() => window.__mock.expected('10002', 'low', 5));
      assert.equal(result.selected, expected);

      const updates = await page.evaluate(() => window.__mock.updates());
      assert.equal(updates.length, 1, 'deveria ter confirmado exatamente uma atualização');
      assert.equal(updates[0].count, expected);
      assert.equal(updates[0].limit, 5, 'o limite digitado precisa chegar ao painel');
      assert.equal(await page.locator('.modal-backdrop').count(), 0, 'o modal precisa fechar');
    } finally {
      await page.close();
    }
  });

  test('Runner: falha transitória é repetida e o canal termina atualizado', async () => {
    const query = 'fail=10002&speed=1';
    const page = await newMockPage(query);
    const config = mockConfig(`${baseUrl}/?${query}`);
    const runner = new Runner({
      getBrowser: async () => ({}),
      getDashboardPage: async () => page,
      syncChannel,
      captureFailure: async () => null,
    });
    const events = [];
    runner.on((ev) => events.push(ev));

    try {
      await runner.start({ criterion: 'low', max: 10, dryRun: false, channels: ['ml-1'], config });
      const snapshot = await waitForFinish(runner);
      const target = snapshot.targets[0];

      assert.equal(target.attempts, 2, 'deveria ter tentado duas vezes');
      assert.equal(target.status, 'started');
      assert.equal(snapshot.status, 'ok');
      const expected = await page.evaluate(() => window.__mock.expected('10002', 'low', 10));
      assert.equal(target.selected, expected);
      assert.ok(events.some((e) => e.type === 'error'), 'deveria emitir erro na primeira tentativa');
      assert.ok(events.some((e) => e.type === 'run' && e.phase === 'finish'));
    } finally {
      await page.close();
    }
  });

  test('Runner: sessão expirada interrompe os canais restantes', async () => {
    const runner = new Runner({
      getBrowser: async () => ({}),
      getDashboardPage: async () => ({}),
      syncChannel: async () => {
        throw new LoginRequiredError('Sessão do WeDrop não está autenticada.');
      },
      captureFailure: async () => null,
    });

    await runner.start({ criterion: 'low', max: 10, dryRun: false, config: mockConfig(baseUrl) });
    const snapshot = await waitForFinish(runner);

    assert.equal(snapshot.targets[0].status, 'error');
    assert.equal(snapshot.targets[1].status, 'canceled');
    assert.equal(snapshot.targets[2].status, 'canceled');
    assert.equal(snapshot.status, 'canceled');
  });

  test('Runner: não permite duas execuções simultâneas', async () => {
    let release;
    const blocker = new Promise((resolve) => {
      release = resolve;
    });
    const runner = new Runner({
      getBrowser: async () => ({}),
      getDashboardPage: async () => ({}),
      syncChannel: async () => {
        await blocker;
        return { selected: 0, durationMs: 1 };
      },
      captureFailure: async () => null,
    });
    const config = mockConfig(baseUrl);
    await runner.start({ criterion: 'low', max: 10, dryRun: true, channels: ['ml-1'], config });
    await assert.rejects(
      () => runner.start({ criterion: 'low', max: 10, dryRun: true, channels: ['ml-1'], config }),
      /já existe uma sincronia/i,
    );
    release();
    await waitForFinish(runner);
  });

  test('calibração reconhece um HTML salvo com estrutura de DOM bem diferente', async () => {
    const page = await browser.newPage();
    try {
      await page.setContent(fs.readFileSync(FIXTURE_HTML, 'utf8'), { waitUntil: 'domcontentloaded' });
      const result = await mapDashboard(page, mockConfig(baseUrl));
      for (const [key, value] of Object.entries(result.checks)) {
        assert.ok(value?.ok, `${key} não foi reconhecido: ${value?.reason ?? 'sem detalhe'}`);
      }
      assert.deepEqual(result.channels.found.sort(), ['10001', '10002', '10003', '10004']);
      assert.deepEqual(result.channels.missing, []);
    } finally {
      await page.close();
    }
  });

  test('servidor entrega a UI e o estado com os 4 canais', async () => {
    const { createServer } = await import('../src/server.js');
    const srv = createServer();
    await new Promise((resolve) => srv.listen(0, '127.0.0.1', resolve));
    const port = srv.address().port;
    try {
      const html = await fetch(`http://127.0.0.1:${port}/`).then((r) => r.text());
      assert.match(html, /Sincronizar estoque/);
      assert.match(html, /app\.js/);

      const state = await fetch(`http://127.0.0.1:${port}/api/state`).then((r) => r.json());
      assert.equal(state.config.channels.length, 4);
      assert.equal(state.config.channels.find((c) => c.key === 'bling').active, false);
      assert.deepEqual(state.config.activeChannels.sort(), ['ml-1', 'ml-2', 'shopee']);
      assert.equal(typeof state.diagnostics.chromeFound, 'boolean');

      assert.equal((await fetch(`http://127.0.0.1:${port}/api/nada`)).status, 404);
      assert.equal((await fetch(`http://127.0.0.1:${port}/../package.json`)).status, 404);
    } finally {
      await new Promise((resolve) => srv.close(resolve));
    }
  });
});
