import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(fileURLToPath(new URL('../..', import.meta.url)));
export const DATA_DIR = path.join(ROOT, 'data');
export const PUBLIC_DIR = path.join(ROOT, 'public');
export const CONFIG_PATH = path.join(ROOT, 'config', 'channels.json');

export const CRITERIA = {
  low: { key: 'low', label: 'Estoque baixo', optionText: /estoque\s+baixo/i, needsLimit: true },
  all: { key: 'all', label: 'Todos mapeados', optionText: /todos\s+mapeados/i, needsLimit: false },
  custom: { key: 'custom', label: 'Customizado', optionText: /customizad/i, needsLimit: true },
};

export function ensureDataDir() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  return DATA_DIR;
}

let cache = null;

export function loadConfig({ fresh = false } = {}) {
  if (cache && !fresh) return cache;

  if (!fs.existsSync(CONFIG_PATH)) {
    throw new Error('config/channels.json não existe: copie config/channels.example.json e preencha com os IDs reais (esse arquivo não vai para o git).');
  }
  const raw = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  const config = {
    dashboardUrl: raw.dashboardUrl ?? 'https://dash.wedrop.com.br/my_items',
    chrome: {
      port: Number(raw.chrome?.port ?? 9222),
      profileDir: path.resolve(ROOT, raw.chrome?.profileDir ?? 'data/chrome-profile'),
      executable: raw.chrome?.executable || '',
      startUrl: raw.chrome?.startUrl ?? raw.dashboardUrl ?? 'https://dash.wedrop.com.br/my_items',
    },
    run: {
      criterion: raw.run?.criterion ?? 'low',
      lowStockMax: Number(raw.run?.lowStockMax ?? 10),
      dryRun: Boolean(raw.run?.dryRun),
      channelDelayMs: Number(raw.run?.channelDelayMs ?? 4000),
      betweenChannelsMs: Number(raw.run?.betweenChannelsMs ?? 3000),
      retries: Number(raw.run?.retries ?? 1),
      retryDelayMs: Number(raw.run?.retryDelayMs ?? 15000),
      stepTimeoutMs: Number(raw.run?.stepTimeoutMs ?? 90000),
      selectionTimeoutMs: Number(raw.run?.selectionTimeoutMs ?? 30 * 60 * 1000),
      updateStartTimeoutMs: Number(raw.run?.updateStartTimeoutMs ?? 120000),
      keepChromeOpen: raw.run?.keepChromeOpen !== false,
    },
    server: {
      host: raw.server?.host ?? '127.0.0.1',
      port: Number(raw.server?.port ?? 8787),
    },
    channels: Array.isArray(raw.channels) ? raw.channels.map(normalizeChannel) : [],
  };

  if (!config.channels.length) throw new Error('config/channels.json não tem nenhum canal configurado.');
  cache = config;
  return config;
}

function normalizeChannel(c) {
  if (!c || !c.id) throw new Error('Canal sem "id" (identificador interno do WeDrop) em config/channels.json.');
  return {
    key: String(c.key ?? c.id),
    name: String(c.name ?? `Canal ${c.id}`),
    id: String(c.id),
    aliases: Array.isArray(c.aliases) ? c.aliases.map(String) : [],
    active: c.active !== false,
    note: c.note ?? '',
  };
}

export function activeChannels(config = loadConfig()) {
  return config.channels.filter((c) => c.active);
}

export function resolveChannels(keys, config = loadConfig()) {
  const active = activeChannels(config);
  if (!keys || !keys.length) return active;
  const wanted = new Set(keys.map((k) => String(k).toLowerCase()));
  const picked = active.filter((c) => wanted.has(c.key.toLowerCase()) || wanted.has(c.id));
  if (!picked.length) throw new Error(`Nenhum canal ativo corresponde a: ${keys.join(', ')}`);
  return picked;
}
