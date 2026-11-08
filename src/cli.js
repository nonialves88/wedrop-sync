#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright-core';
import { loadConfig, DATA_DIR, ensureDataDir, activeChannels } from './lib/config.js';
import { logger } from './lib/log.js';
import { findChromeExecutable, isPortOpen, ensureChrome, getBrowser, getDashboardPage, assertLoggedIn } from './lib/chrome.js';
import { mapDashboard } from './lib/wedrop.js';
import { runner } from './lib/job.js';

const [, , command = 'help', ...rest] = process.argv;

function parseFlags(argv) {
  const flags = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith('--')) continue;
    const key = token.slice(2);
    const next = argv[i + 1];
    if (next && !next.startsWith('--')) {
      flags[key] = next;
      i += 1;
    } else {
      flags[key] = true;
    }
  }
  return flags;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cmdDoctor() {
  const config = loadConfig();
  const chrome = findChromeExecutable(config.chrome.executable);
  const portOpen = await isPortOpen(config.chrome.port);
  let playwright = 'ok';
  try {
    await import('playwright-core');
  } catch {
    playwright = 'AUSENTE — rode: npm install';
  }

  console.log('\nDiagnóstico do ambiente\n');
  console.log(`  Node              ${process.version}`);
  console.log(`  playwright-core   ${playwright}`);
  console.log(`  Chrome            ${chrome ?? 'NÃO ENCONTRADO'}`);
  console.log(`  Porta CDP         ${config.chrome.port} ${portOpen ? '(aberta)' : '(fechada)'}`);
  console.log(`  Perfil dedicado   ${config.chrome.profileDir}`);
  console.log(`  Painel            ${config.dashboardUrl}`);
  console.log(`  Canais ativos     ${activeChannels(config).map((c) => `${c.name} [${c.id}]`).join(', ') || '(nenhum!)'}`);
  console.log('');

  if (portOpen) {
    const browser = await getBrowser().catch((err) => {
      console.error(`  Browser           não conectou: ${err.message}`);
      return null;
    });
    if (browser) {
      const page = await getDashboardPage(browser, config.dashboardUrl);
      try {
        await assertLoggedIn(page, 15000);
        console.log('  Sessão WeDrop     OK (logado, seletor de canais encontrado)\n');
      } catch (err) {
        console.log(`  Sessão WeDrop     ${err.message}\n`);
      }
    }
  } else {
    console.log('  Dica: rode "npm run login" para abrir a janela do Chrome e entrar no WeDrop uma vez.\n');
  }
}

async function cmdLogin() {
  const config = loadConfig();
  console.log('\nAbrindo o Chrome dedicado do projeto…');
  console.log('Faça login no WeDrop nessa janela. O login fica gravado no perfil do próprio Chrome.\n');
  const { launched } = await ensureChrome({ startUrl: config.chrome.startUrl, onLog: (m) => console.log(`  ${m}`) });
  if (!launched) console.log('  (reaproveitei uma janela que já estava aberta na porta de depuração)');

  const browser = await getBrowser();
  const page = await getDashboardPage(browser, config.dashboardUrl);
  const deadline = Date.now() + 5 * 60 * 1000;
  process.stdout.write('  Aguardando a sessão ficar autenticada');
  while (Date.now() < deadline) {
    const ok = await assertLoggedIn(page, 4000)
      .then(() => true)
      .catch(() => false);
    if (ok) {
      process.stdout.write('\n\n  ✓ Sessão OK — pode fechar esta mensagem. Rode "npm start" e abra o painel.\n\n');
      return;
    }
    process.stdout.write('.');
    await sleep(3000);
  }
  console.log('\n\n  ! Não detectei a sessão logada em 5 minutos. Se você já entrou no site, rode "npm run doctor".\n');
}

async function cmdMap() {
  const config = loadConfig();
  console.log('Abrindo o painel real e inspecionando os elementos…\n');
  const browser = await getBrowser();
  const page = await getDashboardPage(browser, config.dashboardUrl);
  await assertLoggedIn(page, 20000);
  const result = await mapDashboard(page, config);
  ensureDataDir();
  const out = path.join(DATA_DIR, 'dom-map.json');
  fs.writeFileSync(out, JSON.stringify(result, null, 2), 'utf8');

  console.log('Selects encontrados:');
  for (const sel of result.page.selects) {
    console.log(`  · ${sel.name ?? '(sem nome)'} — ${sel.options.length} opções: ${sel.options.slice(0, 6).map((o) => `${o.text}[${o.value}]`).join(', ')}`);
  }
  console.log('\nBotões visíveis:');
  for (const b of result.page.buttons.slice(0, 20)) console.log(`  · ${b.text || '(sem texto)'}${b.disabled ? ' [disabled]' : ''}`);
  console.log('\nChecagens da automação:');
  for (const [key, value] of Object.entries(result.checks)) {
    console.log(`  ${value?.ok ? '✓' : '✗'} ${key}${value?.ok ? '' : ` — ${value?.reason ?? 'não encontrado'}`}`);
  }
  console.log(`\nRelatório completo: ${out}\n`);
}

/**
 * Testa os seletores contra um HTML salvo do painel (F12 → Copy outerHTML).
 * Não abre o site: renderiza o arquivo num Chrome headless e roda as mesmas
 * funções de busca que a automação usa em produção.
 */
async function cmdCalibrate() {
  const flags = parseFlags(rest);
  const candidate = flags.file ?? rest.find((a) => !a.startsWith('--'));
  const file = path.resolve(candidate ?? path.join(DATA_DIR, 'my_items.html'));
  if (!fs.existsSync(file)) {
    console.error(`\n✗ Não achei o arquivo: ${file}`);
    console.error('  Salve o HTML do painel e rode: npm run calibrate -- caminho\\arquivo.html\n');
    process.exitCode = 1;
    return;
  }

  const config = loadConfig();
  const html = fs.readFileSync(file, 'utf8');
  const executablePath = findChromeExecutable(config.chrome.executable);
  if (!executablePath) throw new Error('Chrome não encontrado para rodar a calibração.');

  console.log(`\nInspecionando ${path.basename(file)} (${Math.round(html.length / 1024)} KB)…\n`);
  const browser = await chromium.launch({ executablePath, headless: true, args: ['--disable-gpu'] });
  try {
    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);
    const result = await mapDashboard(page, config);

    console.log('Seletores de canal encontrados:');
    for (const sel of result.page.selects) {
      const opts = sel.options.slice(0, 6).map((o) => `${o.text}[${o.value}]`).join(', ');
      console.log(`  · ${sel.name ?? '(sem nome)'} — ${sel.options.length} opção(ões): ${opts}`);
    }

    console.log('\nChecagens da automação:');
    for (const [key, value] of Object.entries(result.checks)) {
      const detalhe = value?.ok ? (value.text ? ` (${String(value.text).slice(0, 60).replace(/\s+/g, ' ')})` : '') : ` — ${value?.reason ?? 'não encontrado'}`;
      console.log(`  ${value?.ok ? '✓' : '✗'} ${key}${detalhe}`);
    }

    console.log(`\nCanais da config presentes no HTML: ${result.channels.found.join(', ') || '(nenhum)'}`);
    if (result.channels.missing.length) {
      console.log(`⚠ Ausentes no HTML: ${result.channels.missing.join(', ')} (podem existir só na página ao vivo)`);
    }

    ensureDataDir();
    const out = path.join(DATA_DIR, 'calibracao.json');
    fs.writeFileSync(out, JSON.stringify({ file, ...result }, null, 2), 'utf8');

    const falhas = Object.entries(result.checks).filter(([, v]) => !v?.ok).map(([k]) => k);
    if (falhas.length) {
      console.log(`\nPontos a ajustar: ${falhas.join(', ')}`);
      console.log(`Detalhes em ${out}\n`);
      process.exitCode = 2;
    } else {
      console.log(`\nTudo reconhecido. Relatório: ${out}\n`);
    }
  } finally {
    await browser.close().catch(() => {});
  }
}

async function cmdRun() {
  const flags = parseFlags(rest);
  const config = loadConfig();
  const criterion = flags.criterion ?? config.run.criterion;
  const max = flags.max ? Number(flags.max) : config.run.lowStockMax;
  const dryRun = Boolean(flags.dry ?? flags['dry-run']);
  const channels = flags.channels ? String(flags.channels).split(',').map((c) => c.trim()) : undefined;

  const unsubscribe = runner.on((event) => {
    if (event.type === 'log' || event.message) {
      const stamp = new Date(event.ts).toLocaleTimeString('pt-BR', { hour12: false });
      const prefix = event.level === 'error' ? '✗' : event.level === 'warn' ? '!' : '·';
      console.log(`${stamp} ${prefix} ${event.message ?? ''}`);
    }
  });

  process.on('SIGINT', () => {
    console.log('\nCancelando…');
    runner.cancel();
  });

  const done = new Promise((resolve) => {
    const off = runner.on((event) => {
      if (event.type === 'run' && event.phase === 'finish') {
        off();
        resolve(event);
      }
    });
  });

  await runner.start({ criterion, max, dryRun, channels });
  const result = await done;
  unsubscribe();

  const snapshot = runner.snapshot();
  console.log('\nResumo:');
  for (const t of snapshot?.targets ?? []) {
    const count = t.selected != null ? `${t.selected} anúncio(s)` : '—';
    console.log(`  ${t.status === 'error' ? '✗' : t.status === 'canceled' ? '⏹' : '✓'} ${t.name.padEnd(32)} ${String(t.status).padEnd(9)} ${count}`);
    if (t.error) console.log(`      ${t.error}`);
  }
  console.log(`\n${result.message ?? ''}\n`);
  process.exitCode = result.status === 'ok' || result.status === 'partial' ? 0 : 1;
}

function cmdHelp() {
  console.log(`
Sincronizador de estoque WeDrop

  npm start                 abre o painel local (http://127.0.0.1:8787)
  npm run login             abre o Chrome dedicado para você logar uma vez
  npm run doctor            diagnóstico do ambiente e da sessão

  npm run run -- [flags]    roda a sincronia pelo terminal
      --criterion low|all   critério de seleção (padrão: config)
      --max 10              limite de unidades para "estoque baixo"
      --dry                 aplica o filtro sem disparar a atualização
      --channels shopee,ml-1  limita a canais específicos

  npm run map               inspeciona a tela real e salva data/dom-map.json
  npm run calibrate -- arquivo.html
                            testa os seletores contra um HTML salvo do painel
  npm test                  roda os testes do fluxo contra um painel simulado

  Como salvar o HTML do painel: no Chrome, F12 → Elements → botão direito no
  <html> → Copy → Copy outerHTML → cole num arquivo .html e rode o calibrate.
`);
}

const commands = { doctor: cmdDoctor, login: cmdLogin, map: cmdMap, calibrate: cmdCalibrate, run: cmdRun, help: cmdHelp };

try {
  const fn = commands[command] ?? cmdHelp;
  await fn();
} catch (err) {
  logger.error(`Falha em "${command}"`, { message: err?.message });
  console.error(`\n✗ ${err?.message ?? err}\n`);
  if (process.env.WEDROP_DEBUG) console.error(err?.stack);
  process.exitCode = 1;
}
