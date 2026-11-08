import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { PUBLIC_DIR, loadConfig, activeChannels } from './lib/config.js';
import { logger } from './lib/log.js';
import { runner } from './lib/job.js';
import { findChromeExecutable, isPortOpen } from './lib/chrome.js';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
};

function publicConfig(config) {
  return {
    dashboardUrl: config.dashboardUrl,
    criterion: config.run.criterion,
    lowStockMax: config.run.lowStockMax,
    dryRun: config.run.dryRun,
    retries: config.run.retries,
    selectionTimeoutMin: Math.round(config.run.selectionTimeoutMs / 60000),
    chromePort: config.chrome.port,
    channels: config.channels.map((c) => ({ key: c.key, name: c.name, id: c.id, active: c.active, note: c.note })),
    activeChannels: activeChannels(config).map((c) => c.key),
  };
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

function readBody(req, limit = 64 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error('Corpo da requisição grande demais.'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim();
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(new Error('JSON inválido no corpo da requisição.'));
      }
    });
    req.on('error', reject);
  });
}

function serveStatic(req, res, urlPath) {
  const relative = urlPath === '/' ? 'index.html' : urlPath.replace(/^\/+/, '');
  const target = path.resolve(PUBLIC_DIR, relative);
  if (!target.startsWith(PUBLIC_DIR)) {
    res.writeHead(403).end('Forbidden');
    return;
  }
  fs.readFile(target, (err, data) => {
    if (err) {
      res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' }).end('Não encontrado');
      return;
    }
    const type = MIME[path.extname(target).toLowerCase()] ?? 'application/octet-stream';
    res.writeHead(200, { 'content-type': type, 'cache-control': 'no-cache' }).end(data);
  });
}

function sseHandler(req, res) {
  res.writeHead(200, {
    'content-type': 'text/event-stream; charset=utf-8',
    'cache-control': 'no-cache, no-transform',
    connection: 'keep-alive',
    'x-accel-buffering': 'no',
  });
  res.write('retry: 2000\n\n');

  const push = (event) => {
    res.write(`data: ${JSON.stringify(event)}\n\n`);
  };

  push({ type: 'snapshot', snapshot: runner.snapshot() });
  const unsubscribe = runner.on(push);
  const ping = setInterval(() => res.write(': ping\n\n'), 25000);

  const cleanup = () => {
    clearInterval(ping);
    unsubscribe();
  };
  req.on('close', cleanup);
  req.on('error', cleanup);
}

async function handleApi(req, res, url) {
  if (url.pathname === '/api/state' && req.method === 'GET') {
    const config = loadConfig({ fresh: true });
    const chromePath = findChromeExecutable(config.chrome.executable);
    const chromeUp = await isPortOpen(config.chrome.port);
    return sendJson(res, 200, {
      config: publicConfig(config),
      snapshot: runner.snapshot(),
      history: runner.history(20),
      diagnostics: {
        chromePath: chromePath ?? null,
        chromeFound: Boolean(chromePath),
        chromePort: config.chrome.port,
        chromePortOpen: chromeUp,
        node: process.version,
      },
    });
  }

  if (url.pathname === '/api/run' && req.method === 'POST') {
    const body = await readBody(req);
    try {
      const snapshot = await runner.start({
        criterion: body.criterion,
        max: body.max,
        dryRun: body.dryRun,
        channels: body.channels,
      });
      return sendJson(res, 202, { ok: true, snapshot });
    } catch (err) {
      if (err?.code === 'ALREADY_RUNNING') return sendJson(res, 409, { ok: false, error: err.message });
      return sendJson(res, 400, { ok: false, error: err?.message ?? String(err) });
    }
  }

  if (url.pathname === '/api/cancel' && req.method === 'POST') {
    const ok = runner.cancel();
    return sendJson(res, ok ? 202 : 409, { ok, error: ok ? null : 'Nada rodando agora.' });
  }

  if (url.pathname === '/api/events' && req.method === 'GET') {
    return sseHandler(req, res);
  }

  if (url.pathname === '/api/history' && req.method === 'GET') {
    return sendJson(res, 200, { ok: true, history: runner.history(50) });
  }

  return sendJson(res, 404, { ok: false, error: 'Rota não encontrada.' });
}

export function createServer() {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', `http://${req.headers.host ?? '127.0.0.1'}`);
    try {
      if (url.pathname.startsWith('/api/')) return await handleApi(req, res, url);
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405).end('Method Not Allowed');
        return;
      }
      serveStatic(req, res, url.pathname);
    } catch (err) {
      logger.error('Erro no servidor', { message: err?.message, path: url.pathname });
      if (!res.headersSent) sendJson(res, 500, { ok: false, error: err?.message ?? String(err) });
      else res.end();
    }
  });
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));

if (isMain) {
  const config = loadConfig();
  const { host, port } = config.server;
  const server = createServer();
  server.listen(port, host, () => {
    const url = `http://${host}:${port}`;
    logger.info(`Sincronizador WeDrop no ar em ${url}`);
    console.log(`\n  ▸ Abra no navegador: ${url}\n`);
  });
  const shutdown = () => {
    logger.info('Encerrando servidor…');
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}
