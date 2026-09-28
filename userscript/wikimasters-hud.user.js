// ==UserScript==
// @name         WikiMasters HUD — Signaux de marché
// @namespace    https://github.com/MaKSiiMe/Wikimasters
// @version      0.2.0
// @description  Sous chaque carte : un signal ACHAT / VENDRE expliqué (rareté réelle vs affichée, seuil de rareté proche, hype Wikipédia, prix vs moyenne des ventes), servi par votre propre API WikiMasters. Lecture seule : aucune action automatisée.
// @author       MaKSiiMe
// @license      MIT
// @homepageURL  https://github.com/MaKSiiMe/Wikimasters
// @supportURL   https://github.com/MaKSiiMe/Wikimasters/issues
// @updateURL    https://raw.githubusercontent.com/MaKSiiMe/Wikimasters/main/userscript/wikimasters-hud.user.js
// @downloadURL  https://raw.githubusercontent.com/MaKSiiMe/Wikimasters/main/userscript/wikimasters-hud.user.js
// @match        https://www.wiki-masters.com/*
// @run-at       document-idle
// @noframes
// @sandbox      JavaScript
// @inject-into  page
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// ==/UserScript==

/*
 * How it works
 *  1. A MutationObserver finds marketplace auctions ([id^="marketplace-auction-"])
 *     and card components on any page.
 *  2. Card data is read from the React props the game already rendered
 *     (card_id, wikipedia_title, rarity, bids). "@sandbox JavaScript" /
 *     "@inject-into page" run the script in the page context so those props are
 *     reachable. Fallback: the visible DOM (<h3> name, rarity glow).
 *  3. Optional, read-only calls to the game's own API with your session
 *     (average sale price per rarity, listing details): throttled and cached.
 *  4. One batched request to YOUR WikiMasters API returns, per card, a verdict
 *     (ACHAT / VENDRE / À SURVEILLER) and the reasons behind it.
 *  5. Optional collection: what you see (rarity, prices, card fields) is sent
 *     to your API to learn the game's rarity rule and score the signals.
 *
 * It never clicks, bids, buys or sells anything.
 */

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  //  Configuration
  // ---------------------------------------------------------------------------

  const CONFIG = {
    // Set from the Tampermonkey menu, e.g. https://<machine>.<tailnet>.ts.net
    apiBase: GM_getValue('apiBase', 'http://localhost:3000'),
    debug: GM_getValue('debug', false),
    collect: GM_getValue('collect', true), // send observations to your API
    readGameApi: GM_getValue('readGameApi', true), // average sale prices from the game

    api: {
      batchDelayMs: 150,
      maxBatch: 60,
      cacheTtlMs: 60_000,
      pendingRetryMs: 20_000,
      unknownTtlMs: 10 * 60_000,
      offlineBackoffMs: 30_000,
      timeoutMs: 8_000,
    },
    game: {
      concurrency: 2, // gentle: this is someone else's server
      minDelayMs: 400,
      maxDelayMs: 10_000,
      pauseAfter429: 5, // consecutive 429 before pausing game calls
      pauseMs: 10 * 60_000,
      salesTtlMs: 6 * 60 * 60_000,
      listingTtlMs: 2 * 60_000,
    },
    observations: { flushMs: 5_000, maxBatch: 50, dedupeMs: 10 * 60_000 },
    refreshEveryMs: 10 * 60_000,

    // Markup of wiki-masters.com (see also the KKH / Instant Price Check scripts).
    selectors: {
      auction: '[id^="marketplace-auction-"]',
      cardRoots: ['button:has([class*="glow-"])', '[class*="aspect-[5/7]"]'],
      ignore: '.wm-belt-card', // decorative carousel on the landing page
      name: 'h3',
      statsIcon: 'svg.lucide-swords',
      sellerRow: 'p.truncate',
      kkhAverage: '.marketplace-avg-price, .card-avg-price-tag', // KKH script, if installed
      rarityBadge: '[style*="--color-rarity"]',
    },
  };

  const RARITIES = ['C', 'PC', 'R', 'SR', 'UR', 'L'];
  const PREFIX = 'wmhud';
  const log = (...args) => CONFIG.debug && console.debug('[WikiMasters HUD]', ...args);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const normRarity = (v) => {
    const code = String(v ?? '').trim().toUpperCase();
    return RARITIES.includes(code) ? code : null;
  };
  const detailListingId = () => location.pathname.match(/^\/marketplace\/([0-9a-f-]{8,})$/i)?.[1] ?? null;

  // ---------------------------------------------------------------------------
  //  Styles
  // ---------------------------------------------------------------------------

  const STYLE = `
    .${PREFIX}-badge {
      display: flex; align-items: center; gap: .35em; flex-wrap: wrap;
      width: fit-content; max-width: 100%; box-sizing: border-box;
      margin: .3rem auto; padding: .2rem .5rem;
      border-radius: 999px; border: 1px solid transparent;
      font: 600 11px/1.3 system-ui, -apple-system, "Segoe UI", sans-serif;
      font-variant-numeric: tabular-nums;
      color: #fff; background: #4b5563; box-shadow: 0 1px 3px rgba(0,0,0,.3);
      cursor: help; user-select: none; position: relative; z-index: 40;
    }
    .${PREFIX}-verdict { font-weight: 800; letter-spacing: .03em; }
    .${PREFIX}-reason  { font-weight: 500; }
    .${PREFIX}-more    { opacity: .7; font-weight: 500; }
    .${PREFIX}-buy     { background: #15803d; border-color: #22c55e; }
    .${PREFIX}-sell    { background: #b91c1c; border-color: #ef4444; }
    .${PREFIX}-watch   { background: #b45309; border-color: #f59e0b; }
    .${PREFIX}-neutral { background: #4b5563; border-color: #6b7280; }
    .${PREFIX}-muted   { background: #374151; color: #d1d5db; font-weight: 500; }
    .${PREFIX}-loading { background: #374151; color: #d1d5db; font-weight: 500; opacity: .75; }
    .${PREFIX}-loading::before {
      content: ""; width: .7em; height: .7em; border-radius: 50%;
      border: 2px solid currentColor; border-right-color: transparent;
      animation: ${PREFIX}-spin .8s linear infinite;
    }
    @keyframes ${PREFIX}-spin { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .${PREFIX}-loading::before { animation: none; } }
  `;

  function injectStyle() {
    if (document.getElementById(`${PREFIX}-style`)) return;
    const style = document.createElement('style');
    style.id = `${PREFIX}-style`;
    style.textContent = STYLE;
    document.head.appendChild(style);
  }

  // ---------------------------------------------------------------------------
  //  Reading the game's data (React props first, DOM as fallback)
  // ---------------------------------------------------------------------------

  const React = {
    fiberOf(node) {
      const key = Object.keys(node).find((k) => k.startsWith('__reactFiber$'));
      return key ? node[key] : null;
    },

    /** First object prop `name` found up the tree (few levels) then down it. */
    findProp(node, name, { up = 6, down = 5 } = {}) {
      const fiber = this.fiberOf(node);
      if (!fiber) return null;
      const hit = (f) => {
        const value = f?.memoizedProps?.[name];
        return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
      };
      let current = fiber;
      for (let i = 0; i <= up && current; i += 1, current = current.return) {
        const value = hit(current);
        if (value) return value;
      }
      const queue = [[fiber.child, 0]];
      for (let visited = 0; queue.length && visited < 80;) {
        const [f, depth] = queue.shift();
        if (!f) continue;
        visited += 1;
        const value = hit(f);
        if (value) return value;
        if (depth < down) queue.push([f.child, depth + 1]);
        queue.push([f.sibling, depth]);
      }
      return null;
    },
  };

  /** Primitive fields of an object (for observations: views, Q-Score... whatever the game exposes). */
  function primitives(obj, max = 40) {
    const out = {};
    if (!obj || typeof obj !== 'object') return out;
    for (const [key, value] of Object.entries(obj)) {
      if (Object.keys(out).length >= max) break;
      if ((typeof value === 'number' && Number.isFinite(value)) || typeof value === 'boolean') out[key] = value;
      else if (typeof value === 'string' && value.length <= 200) out[key] = value;
    }
    return out;
  }

  function fromCardObject(card) {
    if (!card || typeof card !== 'object') return {};
    return {
      cardId: card.id ?? card.card_id ?? card.cardId ?? null,
      title: card.wikipedia_title ?? card.wikipediaTitle ?? null,
      rarity: normRarity(card.rarity ?? card.grade),
      fields: primitives(card),
    };
  }

  function rarityFromDom(root) {
    const badge = root.querySelector(CONFIG.selectors.rarityBadge);
    const fromBadge = normRarity(badge?.textContent);
    if (fromBadge) return fromBadge;
    const glow = root.matches?.('[class*="glow-"]') ? root : root.querySelector('[class*="glow-"]');
    const match = glow?.className?.toString().match(/\bglow-(l|ur|sr|r|pc|c)\b/i);
    return match ? normRarity(match[1]) : null;
  }

  const nameFromDom = (root) => root.querySelector(CONFIG.selectors.name)?.textContent.replace(/\s+/g, ' ').trim() || null;

  function priceFromAuction(auction) {
    if (!auction) return null;
    const price = {
      current: num(auction.current_bid),
      effective: num(auction.effective_bid),
      base: num(auction.base_amount),
    };
    return Object.values(price).some((v) => v !== null) ? price : null;
  }

  const priceValue = (price) => price?.effective ?? price?.current ?? price?.base ?? null;

  // ---------------------------------------------------------------------------
  //  Game API (read-only, same origin, your session): throttled + cached
  // ---------------------------------------------------------------------------

  const Game = (() => {
    const SALES_KEY = `${PREFIX}-sales-v1`;
    const listings = new Map(); // listingId -> { expires, promise }
    const inflight = new Map();
    const queue = [];
    let active = 0;
    let delay = CONFIG.game.minDelayMs;
    let limited = 0;
    let pausedUntil = 0;

    let sales = {};
    try {
      sales = JSON.parse(localStorage.getItem(SALES_KEY) || '{}');
    } catch {
      sales = {};
    }
    const saveSales = () => {
      try {
        localStorage.setItem(SALES_KEY, JSON.stringify(sales));
      } catch {
        // storage full: the in-memory cache still works
      }
    };

    function pump() {
      while (active < CONFIG.game.concurrency && queue.length) {
        const job = queue.shift();
        active += 1;
        job().finally(async () => {
          await sleep(delay);
          active -= 1;
          pump();
        });
      }
    }

    function call(path) {
      if (!CONFIG.readGameApi || Date.now() < pausedUntil) return Promise.resolve(null);
      return new Promise((resolve) => {
        queue.push(async () => {
          try {
            const res = await fetch(path, { credentials: 'include', headers: { accept: 'application/json' } });
            if (res.status === 429) {
              delay = Math.min(delay * 2, CONFIG.game.maxDelayMs);
              limited += 1;
              if (limited >= CONFIG.game.pauseAfter429) {
                pausedUntil = Date.now() + CONFIG.game.pauseMs;
                limited = 0;
                log('game API rate limited, pausing');
              }
              resolve(null);
              return;
            }
            limited = 0;
            delay = Math.max(CONFIG.game.minDelayMs, delay * 0.9);
            resolve(res.ok ? await res.json() : null);
          } catch (err) {
            log('game API error', path, err);
            resolve(null);
          }
        });
        pump();
      });
    }

    async function salesSummary(cardId) {
      if (!cardId) return null;
      const cached = sales[cardId];
      if (cached && Date.now() - cached.t < CONFIG.game.salesTtlMs) return cached.summary;
      if (!inflight.has(cardId)) {
        inflight.set(cardId, call(`/api/marketplace/cards/${encodeURIComponent(cardId)}/sales?scope=summary`).then((data) => {
          inflight.delete(cardId);
          if (data?.summary) {
            sales[cardId] = { t: Date.now(), summary: data.summary };
            saveSales();
          }
          return data?.summary ?? null;
        }));
      }
      return inflight.get(cardId);
    }

    function listing(listingId) {
      const hit = listings.get(listingId);
      if (hit && hit.expires > Date.now()) return hit.promise;
      const promise = call(`/api/marketplace/${encodeURIComponent(listingId)}`).then((data) => data?.auction ?? null);
      listings.set(listingId, { promise, expires: Date.now() + CONFIG.game.listingTtlMs });
      return promise;
    }

    return { salesSummary, listing };
  })();

  // ---------------------------------------------------------------------------
  //  WikiMasters API client: batched, cached, back-off when offline
  // ---------------------------------------------------------------------------

  const Api = (() => {
    const cache = new Map(); // key -> { expires, promise }
    const pending = [];
    let timer = null;
    let offlineUntil = 0;
    const base = () => CONFIG.apiBase.replace(/\/+$/, '');

    const kindOf = (status) => ({ 200: 'ok', 202: 'pending', 404: 'unknown' }[status] ?? 'error');
    const ttlFor = (kind) => ({
      ok: CONFIG.api.cacheTtlMs,
      pending: CONFIG.api.pendingRetryMs,
      unknown: CONFIG.api.unknownTtlMs,
    }[kind] ?? CONFIG.api.offlineBackoffMs);

    function post(path, body, extra = {}) {
      return fetch(`${base()}${path}`, {
        method: 'POST',
        mode: 'cors',
        credentials: 'omit',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(CONFIG.api.timeoutMs),
        ...extra,
      });
    }

    async function flush() {
      timer = null;
      const batch = pending.splice(0, CONFIG.api.maxBatch);
      if (pending.length) timer = setTimeout(flush, 0);
      if (!batch.length) return;
      if (Date.now() < offlineUntil) {
        batch.forEach((job) => job.resolve({ kind: 'offline' }));
        return;
      }
      try {
        const res = await post('/api/cards', { cards: batch.map((job) => job.ctx) });
        if (!res.ok) {
          const message = res.status === 429 ? 'Trop de requêtes, réessai bientôt' : `API ${res.status}`;
          batch.forEach((job) => job.resolve({ kind: 'error', message }));
          return;
        }
        const { results = [] } = await res.json();
        batch.forEach((job, i) => {
          const data = results[i] ?? {};
          job.resolve({ kind: kindOf(data.http_status), data });
        });
      } catch (err) {
        offlineUntil = Date.now() + CONFIG.api.offlineBackoffMs;
        log('API unreachable', err);
        batch.forEach((job) => job.resolve({ kind: 'offline' }));
      }
    }

    /** ctx = { name, title, rarity, price, avg_price } */
    function getCard(ctx) {
      const clean = Object.fromEntries(Object.entries(ctx).filter(([, v]) => v !== null && v !== undefined && v !== ''));
      const key = JSON.stringify(clean);
      const hit = cache.get(key);
      if (hit && hit.expires > Date.now()) return hit.promise;
      const promise = new Promise((resolve) => {
        pending.push({ ctx: clean, resolve });
        if (pending.length >= CONFIG.api.maxBatch) flush();
        else if (!timer) timer = setTimeout(flush, CONFIG.api.batchDelayMs);
      }).then((result) => {
        cache.set(key, { promise, expires: Date.now() + ttlFor(result.kind) });
        return result;
      });
      cache.set(key, { promise, expires: Date.now() + CONFIG.api.timeoutMs * 2 });
      return promise;
    }

    async function health() {
      const res = await fetch(`${base()}/health`, { credentials: 'omit', signal: AbortSignal.timeout(CONFIG.api.timeoutMs) });
      return res.json();
    }

    function clear() {
      cache.clear();
      offlineUntil = 0;
    }

    return { getCard, health, clear, post };
  })();

  // ---------------------------------------------------------------------------
  //  Observations (what you saw), batched and de-duplicated
  // ---------------------------------------------------------------------------

  const Collector = (() => {
    const seen = new Map(); // dedupe key -> timestamp
    const queue = [];

    function push(obs) {
      if (!CONFIG.collect || (!obs.title && !obs.name)) return;
      const key = [obs.card_id ?? obs.title ?? obs.name, obs.listing_id ?? '', priceValue(obs.price) ?? '', obs.rarity ?? ''].join('|');
      const last = seen.get(key);
      if (last && Date.now() - last < CONFIG.observations.dedupeMs) return;
      seen.set(key, Date.now());
      queue.push(Object.fromEntries(Object.entries(obs).filter(([, v]) => v !== null && v !== undefined)));
    }

    async function flush({ keepalive = false } = {}) {
      while (queue.length) {
        const observations = queue.splice(0, CONFIG.observations.maxBatch);
        try {
          await Api.post('/api/observations', { observations }, keepalive ? { keepalive: true } : {});
        } catch (err) {
          log('observations not sent', err);
          return; // best-effort telemetry: dropped on failure
        }
      }
    }

    setInterval(flush, CONFIG.observations.flushMs);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') flush({ keepalive: true });
    });
    return { push, flush };
  })();

  // ---------------------------------------------------------------------------
  //  Finding targets and placing badges
  // ---------------------------------------------------------------------------

  const isOurs = (node) => node instanceof Element && Boolean(node.closest(`.${PREFIX}-badge`));

  /** querySelectorAll that survives selectors an engine doesn't support (e.g. :has). */
  function queryAll(selector) {
    try {
      return document.querySelectorAll(selector);
    } catch {
      return [];
    }
  }

  function findTargets() {
    const { auction, cardRoots, ignore } = CONFIG.selectors;
    const targets = [];
    for (const node of queryAll(auction)) targets.push({ el: node, kind: 'auction' });

    const candidates = new Set();
    for (const selector of cardRoots) {
      for (const node of queryAll(selector)) {
        if (node.closest(auction) || node.closest(ignore) || isOurs(node)) continue;
        if (!node.querySelector(CONFIG.selectors.name) && !React.fiberOf(node)) continue;
        candidates.add(node);
      }
    }
    // Keep the outermost element of nested candidates (button > aspect box).
    for (const node of candidates) {
      let parent = node.parentElement;
      let nested = false;
      while (parent && !nested) {
        nested = candidates.has(parent);
        parent = parent.parentElement;
      }
      if (!nested) targets.push({ el: node, kind: 'card' });
    }
    return targets;
  }

  function placeBadge(target, badge) {
    const { el: node, kind } = target;
    const { kkhAverage, sellerRow, statsIcon } = CONFIG.selectors;
    const kkh = node.querySelector(kkhAverage);
    if (kkh) {
      kkh.insertAdjacentElement('afterend', badge);
      return;
    }
    if (kind === 'auction') {
      const seller = node.querySelector(sellerRow);
      if (seller) seller.insertAdjacentElement('beforebegin', badge);
      else node.appendChild(badge);
      return;
    }
    // Inside the card, just above its attack/defense row (like KKH does).
    const statsRow = node.querySelector(statsIcon)?.closest('div.flex');
    if (statsRow?.parentElement && node.contains(statsRow)) statsRow.insertAdjacentElement('beforebegin', badge);
    else node.insertAdjacentElement('afterend', badge);
  }

  /** Everything the HUD can learn about a target (no network call when React props are there). */
  async function describe(target) {
    const { el: node, kind } = target;
    const info = { kind, name: nameFromDom(node) };

    if (kind === 'auction') {
      info.listingId = node.id.replace('marketplace-auction-', '') || null;
      let auction = React.findProp(node, 'auction', { up: 2 });
      if (!auction && info.listingId) auction = await Game.listing(info.listingId);
      Object.assign(info, fromCardObject(auction?.card));
      info.cardId = auction?.card_id ?? info.cardId ?? null;
      info.price = priceFromAuction(auction);
    } else {
      Object.assign(info, fromCardObject(React.findProp(node, 'card')));
      const listingId = detailListingId();
      if (listingId) { // auction detail page: the card on screen is the one on sale
        const auction = await Game.listing(listingId);
        if (auction && (!info.cardId || auction.card_id === info.cardId)) {
          info.listingId = listingId;
          info.price = priceFromAuction(auction);
          if (!info.title) Object.assign(info, fromCardObject(auction.card));
        }
      }
    }
    info.rarity = info.rarity || rarityFromDom(node);
    info.name = info.name || info.title?.replace(/_/g, ' ') || null;

    if (info.cardId) {
      const summary = await Game.salesSummary(info.cardId);
      if (summary) {
        info.salesSummary = summary;
        info.avgPrice = num(summary[info.rarity]?.average);
      }
    }
    return info;
  }

  // ---------------------------------------------------------------------------
  //  Rendering
  // ---------------------------------------------------------------------------

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const fmt = (n, digits = 0) => (n === null || n === undefined ? '—'
    : Number(n).toLocaleString('fr-FR', { minimumFractionDigits: digits, maximumFractionDigits: digits }));
  const signed = (n, digits = 0) => (n > 0 ? '+' : n < 0 ? '−' : '±') + fmt(Math.abs(n), digits);

  const VERDICTS = { buy: 'ACHAT', sell: 'VENDRE', watch: 'À SURVEILLER', neutral: '—' };

  function renderOk(badge, data, info) {
    const signals = data.signals ?? { verdict: 'neutral', reasons: [] };
    const [top, ...rest] = signals.reasons;
    const period = data.raw?.window === '24h' ? '24 h' : '1 j';
    badge.className = `${PREFIX}-badge ${PREFIX}-${signals.verdict}`;
    badge.replaceChildren(el('span', `${PREFIX}-verdict`, VERDICTS[signals.verdict] ?? '—'));
    if (top) badge.append(el('span', `${PREFIX}-reason`, top.text));
    else if (typeof data.evolution_pct === 'number') badge.append(el('span', `${PREFIX}-reason`, `${signed(data.evolution_pct)} % ${period}`));
    if (rest.length) badge.append(el('span', `${PREFIX}-more`, `+${rest.length}`));

    const r = data.rarity;
    const lines = [`${(data.article ?? info.title ?? info.name ?? '').replace(/_/g, ' ')} — WikiMasters HUD`];
    for (const reason of signals.reasons) lines.push(`• ${reason.text}`);
    if (r) {
      lines.push(`Rareté : affichée ${info.rarity ?? '?'} · trafic 30 j ${r.estimated_30d} (${fmt(r.views_30d)} vues)`
        + ` · mois précédent ${r.estimated_prev_month ?? '?'} (${fmt(r.views_prev_month)})`);
      if (r.next) {
        lines.push(`Prochain palier ${r.next} : encore ${fmt(r.views_to_next)} vues`
          + (r.eta_days !== null ? ` (~${r.eta_days} j au rythme actuel)` : ' (hors de portée à ce rythme)'));
      }
      if (r.downgrade_eta_days !== null) lines.push(`Risque de perdre ${r.estimated_30d} dans ~${r.downgrade_eta_days} j`);
    }
    if (typeof data.evolution_pct === 'number') {
      lines.push(`Trafic ${period} : ${signed(data.evolution_pct, 1)} %`
        + ` (${fmt(data.raw?.views_current)} vues vs ${fmt(data.raw?.views_previous)})`);
    }
    const price = priceValue(info.price);
    if (price !== null || num(info.avgPrice) !== null) {
      const delta = signals.price_delta_pct;
      lines.push(`Prix : ${fmt(price)} · moyenne des ventes ${info.rarity ?? ''} : ${fmt(info.avgPrice)}`
        + (typeof delta === 'number' ? ` (${signed(delta)} %)` : ''));
    }
    if (typeof data.trend_score === 'number') {
      lines.push(`Modèle ML (expérimental) : ${signed(data.trend_score)}`
        + (typeof data.spike_probability === 'number' ? ` · pic sous 7 j ${Math.round(data.spike_probability * 100)} %` : ''));
    }
    if (data.raw?.updated_at) lines.push(`Données du ${new Date(data.raw.updated_at).toLocaleString('fr-FR')}`);
    badge.title = lines.join('\n');
  }

  function renderMuted(badge, text, title) {
    badge.className = `${PREFIX}-badge ${PREFIX}-muted`;
    badge.replaceChildren(el('span', '', text));
    badge.title = title;
  }

  function render(badge, result, info) {
    if (!badge.isConnected) return;
    switch (result.kind) {
      case 'ok':
        renderOk(badge, result.data, info);
        break;
      case 'pending':
        renderMuted(badge, '⏳ Suivi en cours', 'Carte ajoutée au tracker : données dans une minute environ.');
        break;
      case 'unknown':
        renderMuted(badge, "∅ Pas d'article", 'Aucun article Wikipédia trouvé pour cette carte.');
        break;
      case 'offline':
        renderMuted(badge, '⚠ HUD hors ligne', `API injoignable (${CONFIG.apiBase}). Menu Tampermonkey → URL de l'API.`);
        break;
      default:
        renderMuted(badge, '⚠ Erreur', result.message || 'Erreur inconnue');
    }
  }

  // ---------------------------------------------------------------------------
  //  Orchestration
  // ---------------------------------------------------------------------------

  const attached = new WeakMap(); // target element -> { signature, badge, target }
  const badges = new Set(); // for refreshAll

  const signatureOf = ({ el: node, kind }) => (kind === 'auction' ? node.id : `${nameFromDom(node)}|${rarityFromDom(node)}`);

  async function fill(badge, target) {
    let info;
    try {
      info = await describe(target);
    } catch (err) {
      console.error('[WikiMasters HUD] describe failed', err);
      return;
    }
    if (!info.title && !info.name) {
      renderMuted(badge, '? Carte illisible', 'Nom et titre introuvables dans la page.');
      return;
    }

    Collector.push({
      seen_at: new Date().toISOString(),
      page: location.pathname.split('/')[1] || 'home',
      card_id: info.cardId != null ? String(info.cardId) : null,
      listing_id: info.listingId,
      title: info.title,
      name: info.name,
      rarity: info.rarity,
      price: info.price,
      avg_price: info.avgPrice,
      sales_summary: info.salesSummary,
      fields: info.fields,
    });

    const result = await Api.getCard({
      name: info.name,
      title: info.title,
      rarity: info.rarity,
      price: priceValue(info.price),
      avg_price: info.avgPrice,
    });
    render(badge, result, info);
    if (result.kind === 'pending' || result.kind === 'offline') {
      const delay = result.kind === 'pending' ? CONFIG.api.pendingRetryMs : CONFIG.api.offlineBackoffMs;
      setTimeout(() => badge.isConnected && fill(badge, target), delay + 250);
    }
  }

  function attach(target) {
    const signature = signatureOf(target);
    const state = attached.get(target.el);
    if (state && state.signature === signature && state.badge.isConnected) return;
    if (state) {
      state.badge.remove(); // React recycled the node for another card
      badges.delete(state);
    }
    const badge = el('div', `${PREFIX}-badge ${PREFIX}-loading`, 'Signal…');
    badge.setAttribute('role', 'status');
    placeBadge(target, badge);
    const entry = { signature, badge, target };
    attached.set(target.el, entry);
    badges.add(entry);
    log('badge attached', target.kind, signature);
    fill(badge, target);
  }

  function scan() {
    try {
      for (const target of findTargets()) attach(target);
    } catch (err) {
      console.error('[WikiMasters HUD] scan failed', err);
    }
  }

  let scheduled = false;
  function scheduleScan() {
    if (scheduled) return;
    scheduled = true;
    const run = () => {
      scheduled = false;
      scan();
    };
    if ('requestIdleCallback' in window) requestIdleCallback(run, { timeout: 500 });
    else setTimeout(run, 150);
  }

  const isBadge = (node) => node instanceof Element && node.classList.contains(`${PREFIX}-badge`);
  const observer = new MutationObserver((mutations) => {
    const relevant = mutations.some((m) => {
      const target = m.target instanceof Element ? m.target : m.target.parentElement;
      if (!target || isOurs(target)) return false;
      if (m.type === 'characterData') return true; // React swapped a card name
      return [...m.addedNodes, ...m.removedNodes].some((n) => !isBadge(n));
    });
    if (relevant) scheduleScan();
  });

  function refreshAll() {
    Api.clear();
    for (const entry of [...badges]) {
      if (entry.badge.isConnected) fill(entry.badge, entry.target);
      else badges.delete(entry);
    }
  }

  // ---------------------------------------------------------------------------
  //  Tampermonkey menu
  // ---------------------------------------------------------------------------

  function toggle(key, label) {
    GM_registerMenuCommand(`${label} : ${CONFIG[key] ? 'ON' : 'OFF'} (recharger)`, () => GM_setValue(key, !CONFIG[key]));
  }

  function registerMenu() {
    GM_registerMenuCommand("⚙️ URL de l'API…", () => {
      const value = window.prompt('URL de votre API WikiMasters (api_bridge) :', CONFIG.apiBase);
      if (!value) return;
      try {
        const url = new URL(value.trim());
        CONFIG.apiBase = url.origin + url.pathname.replace(/\/+$/, '');
        GM_setValue('apiBase', CONFIG.apiBase);
        refreshAll();
      } catch {
        window.alert('URL invalide');
      }
    });
    GM_registerMenuCommand('🩺 Tester la connexion', async () => {
      try {
        const h = await Api.health();
        window.alert(`API OK\nRedis : ${h.redis ? 'ok' : 'KO'}\nML : ${h.ml?.model ?? h.ml?.status ?? '?'}`);
      } catch (err) {
        window.alert(`API injoignable (${CONFIG.apiBase})\n${err}`);
      }
    });
    GM_registerMenuCommand('🔄 Rafraîchir les badges', refreshAll);
    toggle('collect', '📥 Collecte des observations');
    toggle('readGameApi', '💰 Prix moyens du jeu');
    toggle('debug', '🐞 Debug');
  }

  // ---------------------------------------------------------------------------
  //  Boot
  // ---------------------------------------------------------------------------

  injectStyle();
  registerMenu();
  scan();
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  setInterval(refreshAll, CONFIG.refreshEveryMs);
  log('ready, API =', CONFIG.apiBase);
})();
