import fs from 'node:fs';
import path from 'node:path';
import { DATA_DIR, ensureDataDir, CRITERIA } from './config.js';
import { logger } from './log.js';
import { assertLoggedIn } from './chrome.js';

export class SelectorError extends Error {
  constructor(message, details) {
    super(message);
    this.name = 'SelectorError';
    this.details = details;
  }
}

export class FlowError extends Error {
  constructor(message, details) {
    super(message);
    this.name = 'FlowError';
    this.details = details;
  }
}

export class CancelledError extends Error {
  constructor(message = 'Execução cancelada pelo usuário.') {
    super(message);
    this.name = 'CancelledError';
  }
}

const MARK_ATTR = 'data-wd-sync';

/** Textos que identificam o dropdown "Seleção" do bloco de atualização em massa. */
const CRITERION_LABELS = ['nenhuma', 'todos mapeados', 'estoque baixo', 'customizado'];

/* ---------------------------------------------------------------------------
 * Helpers que rodam DENTRO da página.
 * A ideia: em vez de depender de seletores CSS que a gente não conhece, o
 * script marca o elemento certo com um atributo próprio e depois o Playwright
 * usa esse marcador. Assim a automação sobrevive a mudanças de classe/id.
 * ------------------------------------------------------------------------- */
function initScript() {
  if (window.__wdsync) return;

  const norm = (s) =>
    String(s ?? '')
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .replace(/\s+/g, ' ')
      .trim()
      .toLowerCase();

  const visible = (el) => {
    if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || '1') > 0;
  };

  const clickTargets = () =>
    Array.from(
      document.querySelectorAll(
        'button, a, [role="button"], input[type="button"], input[type="submit"], .btn, [class*="button"]',
      ),
    );

  const api = {
    norm,
    visible,
    textOf(el) {
      if (!el) return '';
      return String(el.innerText ?? el.value ?? el.textContent ?? '');
    },
    mark(el, name) {
      try {
        el.setAttribute('data-wd-sync', name);
        return true;
      } catch {
        return false;
      }
    },
    clear(name) {
      document.querySelectorAll(`[data-wd-sync="${name}"]`).forEach((el) => el.removeAttribute('data-wd-sync'));
    },
    clickTargets,

    findChannelSelect(ids, aliases) {
      this.clear('channel');
      const wanted = (ids ?? []).map((v) => String(v).trim());
      const hints = (aliases ?? []).map(norm).filter(Boolean);
      const report = [];

      let textBest = null;
      for (const sel of document.querySelectorAll('select')) {
        const options = Array.from(sel.options);
        if (options.length < 2) continue;
        const values = options.map((o) => String(o.value).trim());
        const texts = options.map((o) => norm(o.textContent));
        report.push({ kind: 'select', values: values.slice(0, 8), texts: texts.slice(0, 8) });

        const matchedValues = values.filter((v) => wanted.includes(v));
        if (matchedValues.length) {
          this.mark(sel, 'channel');
          return { ok: true, mode: 'value', matchedValues, options: options.length, texts };
        }
        const hits = hints.filter((h) => texts.some((t) => t.includes(h))).length;
        if (hits >= 2 && (!textBest || hits > textBest.hits)) textBest = { sel, hits, texts, options: options.length };
      }
      if (textBest) {
        this.mark(textBest.sel, 'channel');
        return { ok: true, mode: 'text', hits: textBest.hits, options: textBest.options, texts: textBest.texts };
      }
      return { ok: false, reason: 'channel-select-not-found', report };
    },

    findCriterionSelect(labels) {
      this.clear('criterion');
      // aceita tanto ['a','b'] quanto [['a','b']]
      const flat = Array.isArray(labels) ? labels.flat() : [labels];
      const wanted = flat.map(norm).filter(Boolean);
      let best = null;
      for (const sel of document.querySelectorAll('select')) {
        if (sel.getAttribute('data-wd-sync') === 'channel') continue;
        const options = Array.from(sel.options);
        if (options.length < 2) continue;
        const texts = options.map((o) => norm(o.textContent));
        const hits = wanted.filter((w) => texts.some((t) => t.includes(w))).length;
        if (hits >= 2 && (!best || hits > best.hits)) best = { sel, hits, texts };
      }
      if (!best) return { ok: false, reason: 'criterion-select-not-found' };
      this.mark(best.sel, 'criterion');
      return { ok: true, hits: best.hits, texts: best.texts, value: best.sel.value };
    },

    findApplyButton() {
      this.clear('apply');
      const exact = clickTargets().filter((el) => visible(el) && /^aplicar$/.test(norm(this.textOf(el))));
      const loose = clickTargets().filter((el) => visible(el) && /aplicar/.test(norm(this.textOf(el))));
      const target = exact[0] ?? loose[0];
      if (!target) return { ok: false, reason: 'apply-button-not-found' };
      this.mark(target, 'apply');
      return { ok: true, text: this.textOf(target).trim(), disabled: Boolean(target.disabled) };
    },

    findBulkBlock() {
      this.clear('block');
      // Um bloco válido contém o seletor de critério E um botão de ação.
      // Subimos a partir do título até achar esse contêiner — sem parar cedo
      // demais (ex.: só o "cabeçalho" com o botão Atualizar).
      const qualifies = (node) => {
        if (!node || node === document.body || node === document.documentElement) return false;
        const hasSelect = Array.from(node.querySelectorAll('select')).some((s) => visible(s));
        const hasAction = clickTargets().some(
          (b) => node.contains(b) && visible(b) && /aplicar|atualizar/.test(norm(this.textOf(b))),
        );
        return hasSelect && hasAction;
      };

      const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,legend,span,div,p,label,b,strong'))
        .filter((el) => visible(el) && /atualizacao de estoque em massa|estoque em massa/i.test(norm(this.textOf(el))))
        .slice(0, 40);
      for (const heading of headings) {
        let node = heading;
        for (let i = 0; i < 8 && node; i += 1) {
          if (qualifies(node)) {
            this.mark(node, 'block');
            return { ok: true, mode: 'heading', text: this.textOf(node) };
          }
          node = node.parentElement;
        }
      }
      // 2) pela seleção de critério ("Seleção" + Aplicar)
      const criterion = document.querySelector('[data-wd-sync="criterion"]');
      if (criterion) {
        let node = criterion;
        for (let i = 0; i < 8 && node; i += 1) {
          if (qualifies(node)) {
            this.mark(node, 'block');
            return { ok: true, mode: 'criterion', text: this.textOf(node) };
          }
          node = node.parentElement;
        }
      }
      // 3) último recurso: contêiner que tem o botão Aplicar
      const applyBtn = clickTargets().find((b) => visible(b) && /^aplicar$/.test(norm(this.textOf(b))));
      if (applyBtn) {
        let node = applyBtn.parentElement;
        for (let i = 0; i < 6 && node; i += 1) {
          if (node.querySelector('select')) {
            this.mark(node, 'block');
            return { ok: true, mode: 'apply-ancestor', text: this.textOf(node) };
          }
          node = node.parentElement;
        }
      }
      return { ok: false, reason: 'bulk-block-not-found' };
    },

    findLimitInput() {
      this.clear('limit');
      const scope =
        document.querySelector('[data-wd-sync="block"]') ??
        document.querySelector('[data-wd-sync="criterion"]')?.closest('section, fieldset, form, div') ??
        document;
      const candidates = Array.from(
        scope.querySelectorAll('input[type="number"], input[inputmode="numeric"], input[type="text"]'),
      ).filter((el) => visible(el) && !el.readOnly && !el.disabled);
      const numberish = candidates.find((el) => el.type === 'number' || el.inputMode === 'numeric') ?? candidates[0];
      if (!numberish) return { ok: false, reason: 'limit-input-not-found' };
      this.mark(numberish, 'limit');
      return {
        ok: true,
        value: numberish.value,
        type: numberish.type,
        min: numberish.min,
        max: numberish.max,
        placeholder: numberish.placeholder,
      };
    },

    findUpdateButton() {
      this.clear('update');
      const block = document.querySelector('[data-wd-sync="block"]') ?? document;
      const inBlock = clickTargets().filter(
        (el) => block.contains(el) && visible(el) && /^(atualizar|atualizar estoque|iniciar atualizacao)$/.test(norm(this.textOf(el))),
      );
      const global = clickTargets().filter((el) => visible(el) && /^atualizar$/.test(norm(this.textOf(el))));
      const target = inBlock[0] ?? global[0];
      if (!target) return { ok: false, reason: 'update-button-not-found' };
      this.mark(target, 'update');
      const style = window.getComputedStyle(target);
      return {
        ok: true,
        text: this.textOf(target).trim(),
        disabled: Boolean(target.disabled),
        color: style.backgroundColor,
        inBlock: block.contains(target),
      };
    },

    findModal() {
      this.clear('modal');
      const all = Array.from(document.querySelectorAll('body *')).filter((el) => visible(el));
      const candidateSelectors = '.modal, [role="dialog"], dialog, [class*="modal"], [class*="swal"], [class*="dialog"]';
      const scoped = Array.from(document.querySelectorAll(candidateSelectors)).filter((el) => visible(el));
      const pool = scoped.length ? scoped : all;
      const match = pool.find((el) => {
        const t = norm(this.textOf(el));
        return t.includes('confirmar atualizacao em massa') || (t.includes('confirmar') && t.includes('atualizacao'));
      });
      if (!match) return { ok: false, reason: 'confirm-modal-not-found' };
      this.mark(match, 'modal');
      return { ok: true, text: this.textOf(match) };
    },

    findConfirmButton() {
      this.clear('confirm');
      const modal = document.querySelector('[data-wd-sync="modal"]');
      const scope = modal ?? document;
      const inModal = clickTargets().filter(
        (el) => scope.contains(el) && visible(el) && /iniciar atualizacao|iniciar/.test(norm(this.textOf(el))),
      );
      const global = clickTargets().filter((el) => visible(el) && /iniciar atualizacao/.test(norm(this.textOf(el))));
      const target = inModal[0] ?? global[0];
      if (!target) return { ok: false, reason: 'confirm-button-not-found' };
      this.mark(target, 'confirm');
      return { ok: true, text: this.textOf(target).trim(), disabled: Boolean(target.disabled) };
    },

    /**
     * Lê o "status" da verificação (X/Y (Z%) e "N selecionados").
     * Faz uma varredura global pelo elemento MAIS INTERNO que casa com o padrão:
     * assim não dependemos de acertar o contêiner do bloco, e o indicador do
     * AutoPilot (que não usa "(%)") não entra na leitura.
     */
    readStatus() {
      const isStatus = (t) =>
        /selecionad/i.test(t) || /\d[\d.]*\s*\/\s*\d[\d.]*\s*\(\s*\d{1,3}\s*%\s*\)/i.test(t);
      const pool = Array.from(document.querySelectorAll('div,span,p,small,b,strong,label,td,li,section,form'));
      const hits = pool.filter((el) => {
        if (!visible(el)) return false;
        const text = this.textOf(el).trim();
        return text.length > 0 && text.length < 400 && isStatus(text);
      });
      if (!hits.length) return { ok: false, text: '' };
      hits.sort((a, b) => this.textOf(a).trim().length - this.textOf(b).trim().length);
      return { ok: true, text: this.textOf(hits[0]).trim() };
    },

    snapshotBlock() {
      const found = this.findBulkBlock();
      const status = this.readStatus();
      const parts = [];

      const blockEl = document.querySelector('[data-wd-sync="block"]');
      if (found.ok && blockEl) parts.push(this.textOf(blockEl));
      else {
        const criterion = document.querySelector('[data-wd-sync="criterion"]');
        const scope = criterion?.closest('section, fieldset, form');
        parts.push(scope ? this.textOf(scope) : (document.body?.innerText ?? '').slice(0, 4000));
      }
      if (status.ok) parts.push(status.text);

      return { ok: found.ok, mode: found.mode, status: status.ok ? status.text : null, text: parts.filter(Boolean).join('\n') };
    },

    describePage() {
      const selects = Array.from(document.querySelectorAll('select')).map((sel) => ({
        name: sel.name || sel.id || null,
        value: sel.value,
        options: Array.from(sel.options).map((o) => ({ value: o.value, text: this.textOf(o).trim() })),
      }));
      const buttons = clickTargets()
        .filter((el) => visible(el))
        .slice(0, 60)
        .map((el) => ({
          tag: el.tagName.toLowerCase(),
          text: this.textOf(el).slice(0, 60).replace(/\s+/g, ' ').trim(),
          disabled: Boolean(el.disabled),
          cls: String(el.className ?? '').slice(0, 60),
        }));
      const inputs = Array.from(document.querySelectorAll('input'))
        .filter((el) => visible(el))
        .slice(0, 40)
        .map((el) => ({ type: el.type, name: el.name || el.id || null, value: el.value, placeholder: el.placeholder }));
      const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,legend,label'))
        .filter((el) => visible(el))
        .slice(0, 40)
        .map((el) => this.textOf(el).replace(/\s+/g, ' ').trim().slice(0, 80));
      return { url: location.href, selects, buttons, inputs, headings };
    },
  };

  window.__wdsync = api;
}

/* ------------------------------- parsing ---------------------------------- */

function toNumber(raw) {
  const digits = String(raw ?? '').replace(/[^\d]/g, '');
  return digits ? Number(digits) : null;
}

/**
 * Interpreta o texto do bloco "Atualização de Estoque em Massa".
 * Formatos observados: "X/Y (Z%)" na verificação e "N selecionados" ao final.
 */
export function parseSelectionState(text) {
  const flat = String(text ?? '').replace(/\u00a0/g, ' ');
  const progress = /(\d[\d.]*)\s*\/\s*(\d[\d.]*)\s*\(?\s*(\d{1,3})\s*%\s*\)?/.exec(flat);
  const selected = /(\d[\d.]*)\s*selecionad/i.exec(flat);
  const errorish = /(erro ao|falha ao|nao foi possivel|não foi possível|ocorreu um erro)/i.test(flat);

  return {
    progress: progress
      ? { done: toNumber(progress[1]), total: toNumber(progress[2]), pct: Number(progress[3]) }
      : null,
    selected: selected ? toNumber(selected[1]) : null,
    errorish,
  };
}

/** "12 anúncios serão atualizados" — contagem exibida no modal de confirmação. */
export function parseAnnouncementCount(text) {
  const m = /(\d[\d.]*)\s*an[úu]ncio/i.exec(String(text ?? '').replace(/\u00a0/g, ' '));
  return m ? toNumber(m[1]) : null;
}

/** "Total / Mapeados / Não mapeado" — melhor esforço, só para relatório. */
export function parseMappedTotal(text) {
  const flat = String(text ?? '').replace(/\u00a0/g, ' ');
  const m = /(?:mapeados?|total)\D{0,20}?(\d[\d.]*)/i.exec(flat);
  return m ? toNumber(m[1]) : null;
}

/* --------------------------- interação com a página ----------------------- */

async function preparePage(page) {
  await page.addInitScript({ content: `(${initScript.toString()})();` });
  if (await page.evaluate(() => Boolean(window.__wdsync)).catch(() => false)) return;
  await page.evaluate(`(${initScript.toString()})();`).catch(() => {});
}

async function callPageApi(page, fn, ...args) {
  await preparePage(page);
  return page.evaluate(([name, a]) => window.__wdsync[name](...a), [fn, args]);
}

async function settle(page, ms) {
  if (ms > 0) await page.waitForTimeout(ms);
}

function checkCancelled(isCancelled) {
  if (isCancelled?.()) throw new CancelledError();
}

async function ensureMarked(page, markName, apiFn, args, { timeoutMs, isCancelled, what }) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    checkCancelled(isCancelled);
    last = await callPageApi(page, apiFn, ...args);
    if (last?.ok) {
      const count = await page.locator(`[${MARK_ATTR}="${markName}"]`).count();
      if (count > 0) return last;
    }
    await page.waitForTimeout(400);
  }
  throw new SelectorError(`Não encontrei ${what}.`, last);
}

export async function gotoDashboard(page, config, { isCancelled } = {}) {
  checkCancelled(isCancelled);
  const url = config.dashboardUrl;
  const current = page.url();
  if (!current.startsWith(url)) {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 90000 });
  } else {
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 90000 }).catch(async () => {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 90000 });
    });
  }
  await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {});
  await preparePage(page);
}

async function selectChannel(page, channel, config, hooks) {
  const ids = config.channels.map((c) => c.id);
  const aliases = [...new Set(config.channels.flatMap((c) => c.aliases).concat(['shopee', 'mercado livre', 'bling']))];

  const info = await ensureMarked(page, 'channel', 'findChannelSelect', [ids, aliases], {
    timeoutMs: config.run.stepTimeoutMs,
    isCancelled: hooks.isCancelled,
    what: 'o seletor "Selecione o canal de venda" (a página /my_items carregou? você está logado?)',
  });

  const loc = page.locator(`[${MARK_ATTR}="channel"]`).first();
  const options = await page.evaluate((mark) => {
    const sel = document.querySelector(`[data-wd-sync="${mark}"]`);
    return sel ? Array.from(sel.options).map((o) => ({ value: o.value, text: o.textContent.trim() })) : [];
  }, 'channel');

  // 1ª tentativa: pelo identificador interno; 2ª: pelo nome/alias.
  let chosen = options.find((o) => o.value === channel.id) ?? null;
  if (!chosen) {
    const aliasNorms = [channel.name, ...channel.aliases].map((a) =>
      String(a).normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase(),
    );
    const matches = options.filter((o) => {
      const t = o.text.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
      return aliasNorms.some((a) => a && t.includes(a));
    });
    if (matches.length === 1) chosen = matches[0];
    if (matches.length > 1) {
      hooks.log(`Canal ${channel.name}: ${matches.length} opções casaram pelo nome (${matches.map((m) => m.text).join(' | ')}). Usando o identificador ${channel.id}.`, 'warn');
    }
  }

  if (!chosen) {
    throw new SelectorError(
      `O canal "${channel.name}" (id ${channel.id}) não está no dropdown do painel. Opções: ${options
        .map((o) => `${o.text} [${o.value}]`)
        .join(' | ')}`,
      { options },
    );
  }

  hooks.log(`Canal selecionado no dropdown: "${chosen.text}" (${info.mode === 'value' ? 'id ' + chosen.value : 'por nome'}).`);
  await loc.selectOption({ value: chosen.value });
  await page.waitForTimeout(300);
  const applied = await loc.inputValue();
  if (String(applied) !== String(chosen.value)) {
    throw new FlowError(`O dropdown não aceitou a seleção do canal (ficou em "${applied}").`);
  }
  hooks.emit({ type: 'channel', phase: 'selected', channel: channel.key, detail: chosen.text });
}

async function selectCriterion(page, criterion, max, config, hooks) {
  const wanted = [criterion.optionText];

  const info = await ensureMarked(page, 'criterion', 'findCriterionSelect', [CRITERION_LABELS], {
    timeoutMs: config.run.stepTimeoutMs,
    isCancelled: hooks.isCancelled,
    what: 'o seletor "Seleção" do bloco de Atualização de Estoque em Massa',
  });

  const options = await page.evaluate(() => {
    const sel = document.querySelector('[data-wd-sync="criterion"]');
    return sel ? Array.from(sel.options).map((o) => ({ value: o.value, text: o.textContent.trim() })) : [];
  });

  const normalize = (s) => String(s ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const target = options.find((o) => wanted.some((re) => re.test(normalize(o.text))));
  if (!target) {
    throw new SelectorError(
      `Não achei a opção de critério "${criterion.label}" no dropdown "Seleção". Opções vistas: ${options
        .map((o) => o.text)
        .join(' | ')}`,
      { options, info },
    );
  }

  const loc = page.locator(`[${MARK_ATTR}="criterion"]`).first();
  await loc.selectOption({ value: target.value });
  await page.waitForTimeout(400);
  hooks.log(`Critério: "${target.text}".`);

  if (criterion.needsLimit) {
    const limitInfo = await ensureMarked(page, 'limit', 'findLimitInput', [], {
      timeoutMs: config.run.stepTimeoutMs,
      isCancelled: hooks.isCancelled,
      what: `o campo de limite de unidades (aparece ao escolher "${target.text}")`,
    });
    const limitLoc = page.locator(`[${MARK_ATTR}="limit"]`).first();
    const previous = limitInfo.value;
    await limitLoc.fill(String(max));
    await limitLoc.evaluate((el) => {
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.blur();
    });
    const after = await limitLoc.inputValue();
    if (String(after) !== String(max)) {
      throw new FlowError(`O campo de limite não aceitou o valor ${max} (ficou "${after}").`);
    }
    hooks.log(`Limite de unidades: ${previous} → ${after}.`);
    await page.waitForTimeout(250);
  }

  return target.text;
}

async function clickApply(page, config, hooks) {
  const info = await ensureMarked(page, 'apply', 'findApplyButton', [], {
    timeoutMs: config.run.stepTimeoutMs,
    isCancelled: hooks.isCancelled,
    what: 'o botão "Aplicar" do bloco de atualização em massa',
  });
  if (info.disabled) {
    throw new FlowError('O botão "Aplicar" está desabilitado — provavelmente falta escolher o critério ou o canal ainda está carregando.');
  }
  await page.locator(`[${MARK_ATTR}="apply"]`).first().click({ timeout: config.run.stepTimeoutMs });
  hooks.log('"Aplicar" clicado — verificando o catálogo item a item…');
  return info;
}

async function waitForSelection(page, config, hooks) {
  const deadline = Date.now() + config.run.selectionTimeoutMs;
  let lastKey = null;
  let stable = 0;
  let best = { selected: null, progress: null };
  let lastReport = 0;

  while (Date.now() < deadline) {
    checkCancelled(hooks.isCancelled);
    const snap = await callPageApi(page, 'snapshotBlock');
    const state = parseSelectionState(snap?.text ?? '');
    if (state.selected != null) best = state;

    if (state.progress?.total) {
      const now = Date.now();
      if (now - lastReport > 1500) {
        lastReport = now;
        hooks.emit({
          type: 'selection-progress',
          channel: hooks.channelKey,
          done: state.progress.done,
          total: state.progress.total,
          pct: state.progress.pct,
          selected: state.selected,
        });
      }
    }

    if (state.errorish) {
      throw new FlowError('O painel reportou erro durante a aplicação do filtro.', { text: snap?.text ?? '' });
    }

    const complete = state.selected != null && (!state.progress || state.progress.done >= state.progress.total);
    const key = `${state.selected}|${state.progress?.done}|${state.progress?.total}`;
    if (complete) {
      stable = key === lastKey ? stable + 1 : 0;
      if (stable >= 2) {
        return { selected: state.selected, progress: state.progress, text: snap?.text ?? '' };
      }
    } else {
      stable = 0;
    }
    lastKey = key;
    await page.waitForTimeout(700);
  }

  throw new FlowError(
    `A aplicação do filtro não terminou dentro de ${Math.round(config.run.selectionTimeoutMs / 60000)} min. ` +
      `Último estado lido: ${best.progress ? `${best.progress.done}/${best.progress.total} (${best.progress.pct}%)` : 'sem progresso'} / ${best.selected ?? '?'} selecionados.`,
    { best },
  );
}

async function confirmBulkUpdate(page, config, hooks, { expectedSelected }) {
  const updateInfo = await ensureMarked(page, 'update', 'findUpdateButton', [], {
    timeoutMs: config.run.stepTimeoutMs,
    isCancelled: hooks.isCancelled,
    what: 'o botão verde "Atualizar"',
  });
  if (updateInfo.disabled) throw new FlowError('O botão "Atualizar" está desabilitado (nenhum anúncio selecionado?).');

  await page.locator(`[${MARK_ATTR}="update"]`).first().click({ timeout: config.run.stepTimeoutMs });
  hooks.log(`"Atualizar" clicado${updateInfo.inBlock ? '' : ' (botão fora do bloco esperado — confira se é o certo)'}. Aguardando o modal de confirmação…`);

  const modal = await ensureMarked(page, 'modal', 'findModal', [], {
    timeoutMs: config.run.updateStartTimeoutMs,
    isCancelled: hooks.isCancelled,
    what: 'o modal "Confirmar Atualização em Massa"',
  });
  const modalSelected = parseSelectionState(modal.text).selected ?? parseAnnouncementCount(modal.text);
  hooks.log(`Modal de confirmação aberto (mostra ${modalSelected ?? '?'} anúncios; esperado ${expectedSelected}).`);
  if (modalSelected != null && expectedSelected != null && modalSelected !== expectedSelected) {
    hooks.log(`Divergência: o modal mostra ${modalSelected} e a seleção calculada foi ${expectedSelected}. Seguindo com o painel (fonte da verdade).`, 'warn');
  }

  const confirmInfo = await ensureMarked(page, 'confirm', 'findConfirmButton', [], {
    timeoutMs: config.run.stepTimeoutMs,
    isCancelled: hooks.isCancelled,
    what: 'o botão "Iniciar Atualização" dentro do modal',
  });
  if (confirmInfo.disabled) throw new FlowError('O botão "Iniciar Atualização" do modal está desabilitado.');

  await page.locator(`[${MARK_ATTR}="confirm"]`).first().click({ timeout: config.run.stepTimeoutMs });
  hooks.log('"Iniciar Atualização" clicado — o job agora roda em segundo plano no painel.');

  await page
    .locator(`[${MARK_ATTR}="modal"]`)
    .first()
    .waitFor({ state: 'hidden', timeout: config.run.updateStartTimeoutMs })
    .catch(() => hooks.log('O modal não fechou em tempo, mas a atualização pode ter sido iniciada — confira o indicador no painel.', 'warn'));

  return { modalSelected };
}

/**
 * Executa a rotina completa em UM canal.
 * @returns {Promise<{channel: object, selected: number|null, dryRun: boolean, durationMs: number}>}
 */
export async function syncChannel(page, channel, { config, criterionKey, max, dryRun, isCancelled, onEvent }) {
  const startedAt = Date.now();
  const hooks = {
    channelKey: channel.key,
    isCancelled,
    log: (message, level = 'info') => {
      logger.debug(message, { channel: channel.key });
      onEvent({ type: 'log', level, channel: channel.key, message: `${channel.name}: ${message}` });
    },
    emit: (event) => onEvent({ ...event, channel: channel.key }),
  };

  if (!channelsHaveId(config)) throw new Error('Configuração sem canais.');
  const criterion = CRITERIA[criterionKey];
  if (!criterion) throw new Error(`Critério desconhecido: ${criterionKey}`);

  onEvent({ type: 'channel', phase: 'start', channel: channel.key, name: channel.name });

  await gotoDashboard(page, config, { isCancelled });
  await assertLoggedIn(page, 20000);

  await selectChannel(page, channel, config, hooks);
  await settle(page, config.run.channelDelayMs);

  const mappedTotal = await callPageApi(page, 'snapshotBlock')
    .then((snap) => parseMappedTotal(snap?.text ?? ''))
    .catch(() => null);

  await selectCriterion(page, criterion, max, config, hooks);
  await clickApply(page, config, hooks);
  const selection = await waitForSelection(page, config, hooks);

  hooks.log(`Seleção concluída: ${selection.selected} anúncio(s)${selection.progress ? ` (${selection.progress.total} verificados)` : ''}.`);
  onEvent({
    type: 'channel',
    phase: 'selected',
    channel: channel.key,
    selected: selection.selected,
    checked: selection.progress?.total ?? null,
    mappedTotal,
  });

  if (dryRun) {
    hooks.log('Dry-run: parando antes do "Atualizar" (nada foi alterado).');
    return { channel, selected: selection.selected, dryRun: true, durationMs: Date.now() - startedAt };
  }

  const { modalSelected } = await confirmBulkUpdate(page, config, hooks, { expectedSelected: selection.selected });
  if (modalSelected != null && selection.selected != null && modalSelected !== selection.selected) {
    hooks.log(`Atenção: o modal mostrou ${modalSelected} e a seleção foi ${selection.selected}.`, 'warn');
  }

  onEvent({ type: 'channel', phase: 'started', channel: channel.key, selected: selection.selected });
  return {
    channel,
    selected: selection.selected,
    checked: selection.progress?.total ?? null,
    mappedTotal,
    modalSelected,
    dryRun: false,
    durationMs: Date.now() - startedAt,
  };
}

function channelsHaveId(config) {
  return Array.isArray(config.channels) && config.channels.length > 0;
}

/** Captura artefatos de diagnóstico quando algo falha. */
export async function captureFailure(page, tag) {
  try {
    ensureDataDir();
    const dir = path.join(DATA_DIR, 'artifacts');
    fs.mkdirSync(dir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const base = path.join(dir, `falha-${stamp}-${tag.replace(/[^\w.-]+/g, '_')}`);
    await page.screenshot({ path: `${base}.png`, fullPage: true }).catch(() => {});
    const html = await page.content().catch(() => '');
    if (html) fs.writeFileSync(`${base}.html`, html, 'utf8');
    const describe = await callPageApi(page, 'describePage').catch(() => null);
    if (describe) fs.writeFileSync(`${base}.json`, JSON.stringify(describe, null, 2), 'utf8');
    return base;
  } catch {
    return null;
  }
}

/** Inspeção da página real: usada por `npm run map` / `npm run calibrate`. */
export async function mapDashboard(page, config) {
  await preparePage(page);
  const base = await callPageApi(page, 'describePage');
  const ids = (config?.channels ?? []).map((c) => String(c.id));
  const aliases = [...new Set((config?.channels ?? []).flatMap((c) => [...(c.aliases ?? []), c.name]).filter(Boolean))];

  const checks = {
    channelSelect: await callPageApi(page, 'findChannelSelect', ids, aliases),
    criterionSelect: await callPageApi(page, 'findCriterionSelect', CRITERION_LABELS),
    applyButton: await callPageApi(page, 'findApplyButton'),
    bulkBlock: await callPageApi(page, 'findBulkBlock'),
    updateButton: await callPageApi(page, 'findUpdateButton'),
    limitInput: await callPageApi(page, 'findLimitInput'),
  };

  const options = await page.evaluate((mark) => {
    const sel = document.querySelector(`[data-wd-sync="${mark}"]`);
    return sel ? Array.from(sel.options).map((o) => String(o.value)) : [];
  }, 'channel');

  const allValues = new Set([...options, ...(base.selects ?? []).flatMap((s) => s.options.map((o) => String(o.value)))]);
  const channels = {
    found: ids.filter((id) => allValues.has(id)),
    missing: ids.filter((id) => !allValues.has(id)),
  };

  return { page: base, checks, channels };
}
