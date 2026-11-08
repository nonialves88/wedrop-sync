import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { chromium } from 'playwright-core';
import { ROOT, loadConfig } from './config.js';
import { logger } from './log.js';

export class LoginRequiredError extends Error {
  constructor(message) {
    super(message);
    this.name = 'LoginRequiredError';
  }
}

const CHROME_CANDIDATES = [
  process.env.WEDROP_CHROME_PATH,
  path.join(process.env['PROGRAMFILES'] ?? 'C:\\Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
  path.join(process.env['PROGRAMFILES(X86)'] ?? 'C:\\Program Files (x86)', 'Google', 'Chrome', 'Application', 'chrome.exe'),
  path.join(process.env.LOCALAPPDATA ?? '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
  path.join(process.env.LOCALAPPDATA ?? '', 'Chromium', 'Application', 'chrome.exe'),
  path.join(process.env.PROGRAMFILES ?? '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
].filter(Boolean);

export function findChromeExecutable(configured = '') {
  const candidates = [configured, ...CHROME_CANDIDATES].filter(Boolean);
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {
      /* ignora path inválido */
    }
  }
  return null;
}

export function isPortOpen(port, host = '127.0.0.1', timeoutMs = 800) {
  return new Promise((resolve) => {
    const socket = new net.Socket();
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(value);
    };
    socket.setTimeout(timeoutMs);
    socket.once('connect', () => done(true));
    socket.once('timeout', () => done(false));
    socket.once('error', () => done(false));
    socket.connect(port, host);
  });
}

async function cdpVersion(port) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(1500) });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

async function waitForCdp(port, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const info = await cdpVersion(port);
    if (info?.webSocketDebuggerUrl) return info;
    await new Promise((r) => setTimeout(r, 300));
  }
  return null;
}

/**
 * Garante uma janela do Chrome com depuração remota ligada, usando um perfil
 * dedicado do projeto (não o perfil pessoal do usuário). O login no WeDrop é
 * feito UMA vez nessa janela e fica gravado nos cookies do próprio Chrome —
 * o app nunca vê nem guarda credencial.
 */
export async function ensureChrome({ startUrl, onLog = () => {} } = {}) {
  const config = loadConfig();
  const port = config.chrome.port;
  const profileDir = config.chrome.profileDir;
  const url = startUrl ?? config.chrome.startUrl;

  if (await isPortOpen(port)) {
    const info = await cdpVersion(port);
    if (info?.webSocketDebuggerUrl) return { port, launched: false, userDataDir: profileDir, pid: null };
    logger.warn('Porta de depuração aberta, mas sem CDP respondendo', { port });
  }

  const executable = findChromeExecutable(config.chrome.executable);
  if (!executable) {
    throw new Error(
      'Chrome não encontrado. Instale o Google Chrome ou defina "chrome.executable" em config/channels.json ' +
        '(ou a variável de ambiente WEDROP_CHROME_PATH).',
    );
  }

  fs.mkdirSync(profileDir, { recursive: true });

  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-features=Translate,OptimizationHints',
    '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows',
    '--restore-last-session',
    url,
  ];

  onLog(`Abrindo Chrome dedicado (porta ${port})…`);
  const child = spawn(executable, args, {
    detached: true,
    stdio: 'ignore',
    cwd: ROOT,
  });
  child.unref();

  const info = await waitForCdp(port, 40000);
  if (!info) {
    throw new Error(
      `O Chrome foi aberto, mas a porta de depuração ${port} não respondeu. Feche qualquer Chrome aberto com o mesmo perfil e tente de novo.`,
    );
  }

  logger.info('Chrome dedicado pronto', { port, profileDir, pid: child.pid });
  return { port, launched: true, userDataDir: profileDir, pid: child.pid ?? null };
}

let cachedBrowser = null;

export async function getBrowser({ onLog = () => {} } = {}) {
  if (cachedBrowser?.isConnected?.()) return cachedBrowser;

  const { port } = await ensureChrome({ onLog });
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, { timeout: 30000 });
  browser.on('disconnected', () => {
    if (cachedBrowser === browser) cachedBrowser = null;
  });
  cachedBrowser = browser;
  logger.info('Conectado ao Chrome via CDP', { port });
  return browser;
}

export function forgetBrowser() {
  cachedBrowser = null;
}

/**
 * Reaproveita a aba do WeDrop se já existir; senão cria uma nova.
 */
export async function getDashboardPage(browser, url) {
  const context = browser.contexts()[0];
  if (!context) throw new Error('Nenhum contexto de navegador disponível no Chrome conectado.');

  const pages = context.pages();
  const existing = pages.find((p) => {
    try {
      return p.url().includes('dash.wedrop.com.br');
    } catch {
      return false;
    }
  });

  const page = existing ?? (await context.newPage());
  if (!existing) {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  }
  await page.bringToFront().catch(() => {});
  return page;
}

export async function assertLoggedIn(page, timeoutMs = 8000) {
  const hasChannelSelect = await page
    .waitForFunction(
      () =>
        Array.from(document.querySelectorAll('select')).some((sel) =>
          Array.from(sel.options).some((o) => /shopee|mercado\s*livre|bling/i.test(o.textContent ?? '')),
        ),
      null,
      { timeout: timeoutMs, polling: 500 },
    )
    .then(() => true)
    .catch(() => false);

  if (hasChannelSelect) return true;

  const url = page.url();
  const bodyText = (await page.evaluate(() => document.body?.innerText ?? '').catch(() => '')).slice(0, 400);
  const looksLoggedOut = /login|entrar|senha|password|signin/i.test(url) || /entrar|login|senha/i.test(bodyText);

  throw new LoginRequiredError(
    looksLoggedOut
      ? `Sessão do WeDrop não está autenticada nesta janela do Chrome (${url}). Faça login manualmente na janela aberta e rode de novo.`
      : `Não encontrei o seletor "Selecione o canal de venda" em ${url}. Confirme que a página é /my_items e que você está logado.`,
  );
}
