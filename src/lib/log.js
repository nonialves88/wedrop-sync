import fs from 'node:fs';
import path from 'node:path';
import { DATA_DIR, ensureDataDir } from './config.js';

const LOG_DIR = path.join(DATA_DIR, 'logs');
let stream = null;

function todayPath() {
  const d = new Date();
  const stamp = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  return path.join(LOG_DIR, `${stamp}.log`);
}

function getStream() {
  if (stream) return stream;
  ensureDataDir();
  fs.mkdirSync(LOG_DIR, { recursive: true });
  stream = fs.createWriteStream(todayPath(), { flags: 'a' });
  return stream;
}

export function log(level, message, extra) {
  const ts = new Date().toISOString();
  const line = `[${ts}] ${level.toUpperCase().padEnd(5)} ${message}${extra ? ' ' + safeJson(extra) : ''}`;
  try {
    getStream().write(line + '\n');
  } catch {
    /* log nunca deve derrubar o app */
  }
  const consoleFn = level === 'error' ? console.error : level === 'warn' ? console.warn : console.log;
  consoleFn(line);
  return line;
}

export const logger = {
  info: (m, x) => log('info', m, x),
  warn: (m, x) => log('warn', m, x),
  error: (m, x) => log('error', m, x),
  debug: (m, x) => (process.env.WEDROP_DEBUG ? log('debug', m, x) : null),
};

function safeJson(value) {
  try {
    return JSON.stringify(value);
  } catch {
    return '[unserializable]';
  }
}
