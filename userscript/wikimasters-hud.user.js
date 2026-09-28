// ==UserScript==
// @name         WikiMasters HUD — Trend Score
// @namespace    https://github.com/MaKSiiMe/WikiMasters
// @version      0.1.0
// @description  Affiche sous le prix de chaque carte un badge « Trend Score » (évolution du trafic Wikipédia + prédiction ML) servi par votre propre API WikiMasters. Lecture seule : aucune action automatisée.
// @author       MaKSiiMe
// @license      MIT
// @homepageURL  https://github.com/MaKSiiMe/WikiMasters
// @supportURL   https://github.com/MaKSiiMe/WikiMasters/issues
// @updateURL    https://raw.githubusercontent.com/MaKSiiMe/WikiMasters/main/userscript/wikimasters-hud.user.js
// @downloadURL  https://raw.githubusercontent.com/MaKSiiMe/WikiMasters/main/userscript/wikimasters-hud.user.js
// @match        https://www.wiki-masters.com/*
// @run-at       document-idle
// @noframes
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// ==/UserScript==

/*
 * How it works
 *  1. A MutationObserver watches the (Next.js / React) page for card nodes.
 *  2. For each card, the name is read from its <h3> and sent with fetch() to
 *     the WikiMasters API bridge (GET /api/card/:name). The API only accepts
 *     requests whose Origin is https://www.wiki-masters.com.
 *  3. A badge is injected right under the card's price: green when the trend
 *     is bullish, red when bearish, grey when flat.
 *
 * The script is strictly read-only: it never clicks, buys, sells or talks to
 * wiki-masters.com servers. The only network calls go to YOUR API.
 */

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  //  Configuration
  // ---------------------------------------------------------------------------

  const CONFIG = {
    // Change it from the Tampermonkey menu ("⚙️ URL de l'API").
    apiBase: GM_getValue('apiBase', 'http://localhost:3000'),
    debug: GM_getValue('debug', false),

    cacheTtlMs: 60_000,
    pendingRetryMs: 20_000, // card just queued for tracking: ask again later
    unknownTtlMs: 10 * 60_000,
    offlineBackoffMs: 30_000,
    refreshEveryMs: 10 * 60_000,
    requestTimeoutMs: 6_000,
    maxConcurrent: 4,

    neutralBand: 5, // |score| below this -> grey badge
    flameAt: 0.5, // spike probability that adds a 🔥

    // Markup observed on wiki-masters.com (Tailwind). Adjust here if the site changes.
    selectors: {
      cardRoot: '[class*="aspect-[5/7]"]',
      cardName: 'h3',
      ignore: '.wm-belt-card', // decorative carousel on the landing page
      price: [
        '[data-price]',
        '[class*="price" i]',
        'svg[class*="lucide-coins"]',
        'svg[class*="lucide-coin"]',
        'svg[class*="lucide-gem"]',
        'svg[class*="lucide-euro"]',
        'img[alt*="coin" i]',
        'img[alt*="pièce" i]',
      ],
    },
    // Text fallback: "1 250 💰", "980 pièces", "12 000 WM"...
    priceText: /\d[\d\s  .,]*\s*(?:₩|WM\b|💰|🪙|💎|pi[eè]ces?\b|coins?\b|cr[ée]dits?\b|gemmes?\b)/i,
    listingDepth: 4, // ancestors of the card searched for its price
  };

  const PREFIX = 'wmhud';
  const log = (...args) => CONFIG.debug && console.debug('[WikiMasters HUD]', ...args);

  // ---------------------------------------------------------------------------
  //  Styles
  // ---------------------------------------------------------------------------

  const STYLE = `
    .${PREFIX}-badge {
      display: flex; align-items: center; gap: .35em; flex-wrap: wrap;
      width: fit-content; max-width: 100%; box-sizing: border-box;
      margin: .35rem auto 0; padding: .2rem .55rem;
      border-radius: 999px; border: 1px solid transparent;
      font: 600 11px/1.3 system-ui, -apple-system, "Segoe UI", sans-serif;
      font-variant-numeric: tabular-nums; letter-spacing: .01em;
      color: #fff; background: #4b5563; box-shadow: 0 1px 3px rgba(0,0,0,.25);
      cursor: help; user-select: none; position: relative; z-index: 40;
      transition: background-color .2s ease;
    }
    .${PREFIX}-badge a { color: inherit; text-decoration: none; }
    .${PREFIX}-up      { background: #15803d; border-color: #22c55e; }
    .${PREFIX}-down    { background: #b91c1c; border-color: #ef4444; }
    .${PREFIX}-flat    { background: #4b5563; border-color: #6b7280; }
    .${PREFIX}-muted   { background: #374151; color: #d1d5db; font-weight: 500; }
    .${PREFIX}-loading { background: #374151; color: #d1d5db; font-weight: 500; opacity: .75; }
    .${PREFIX}-sep     { opacity: .6; }
    .${PREFIX}-evo     { font-weight: 500; opacity: .95; }
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
  //  API client: cache, de-duplication, concurrency limit
  // ---------------------------------------------------------------------------

  const Api = (() => {
    const cache = new Map(); // name -> { expires, promise }
    const queue = [];
    let active = 0;
    let offlineUntil = 0;

    const base = () => CONFIG.apiBase.replace(/\/+$/, '');

    function pump() {
      while (active < CONFIG.maxConcurrent && queue.length) {
        const job = queue.shift();
        active += 1;
        job().finally(() => {
          active -= 1;
          pump();
        });
      }
    }

    function schedule(task) {
      return new Promise((resolve) => {
        queue.push(() => task().then(resolve));
        pump();
      });
    }

    async function request(name) {
      if (Date.now() < offlineUntil) return { kind: 'offline' };
      const url = `${base()}/api/card/${encodeURIComponent(name)}`;
      try {
        const res = await fetch(url, {
          method: 'GET',
          mode: 'cors',
          credentials: 'omit',
          cache: 'no-store',
          headers: { Accept: 'application/json' },
          signal: AbortSignal.timeout(CONFIG.requestTimeoutMs),
        });
        const body = await res.json().catch(() => ({}));
        if (res.status === 200) return { kind: 'ok', data: body };
        if (res.status === 202) return { kind: 'pending', data: body };
        if (res.status === 404) return { kind: 'unknown', data: body };
        if (res.status === 429) return { kind: 'error', message: 'Trop de requêtes, réessai bientôt' };
        return { kind: 'error', message: `API ${res.status}` };
      } catch (err) {
        offlineUntil = Date.now() + CONFIG.offlineBackoffMs;
        log('API unreachable', err);
        return { kind: 'offline' };
      }
    }

    function ttlFor(result) {
      switch (result.kind) {
        case 'ok': return CONFIG.cacheTtlMs;
        case 'pending': return CONFIG.pendingRetryMs;
        case 'unknown': return CONFIG.unknownTtlMs;
        default: return CONFIG.offlineBackoffMs;
      }
    }

    function getCard(name) {
      const hit = cache.get(name);
      if (hit && hit.expires > Date.now()) return hit.promise;
      const promise = schedule(() => request(name)).then((result) => {
        cache.set(name, { promise, expires: Date.now() + ttlFor(result) });
        return result;
      });
      cache.set(name, { promise, expires: Date.now() + CONFIG.requestTimeoutMs * 2 });
      return promise;
    }

    async function health() {
      const res = await fetch(`${base()}/health`, { credentials: 'omit', signal: AbortSignal.timeout(CONFIG.requestTimeoutMs) });
      return res.json();
    }

    function clear() {
      cache.clear();
      offlineUntil = 0;
    }

    return { getCard, health, clear };
  })();

  // ---------------------------------------------------------------------------
  //  DOM helpers
  // ---------------------------------------------------------------------------

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const isOurs = (node) => node instanceof Element && Boolean(node.closest(`.${PREFIX}-badge`));

  function findCards() {
    const { cardRoot, cardName, ignore } = CONFIG.selectors;
    const cards = [];
    for (const heading of document.querySelectorAll(`${cardRoot} ${cardName}`)) {
      const card = heading.closest(cardRoot);
      if (!card || card.closest(ignore)) continue;
      const name = heading.textContent.replace(/\s+/g, ' ').trim();
      if (name) cards.push({ card, name });
    }
    return cards;
  }

  function priceBySelector(scope, card) {
    for (const selector of CONFIG.selectors.price) {
      for (const node of scope.querySelectorAll(selector)) {
        if (isOurs(node) || (scope !== card && card.contains(node))) continue;
        const tag = node.tagName.toLowerCase();
        return tag === 'svg' || tag === 'img' ? node.parentElement : node;
      }
    }
    return null;
  }

  function priceByText(scope, card) {
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode(); text; text = walker.nextNode()) {
      const parent = text.parentElement;
      if (!parent || card.contains(parent) || isOurs(parent)) continue;
      if (CONFIG.priceText.test(text.textContent)) return parent;
    }
    return null;
  }

  /** Element under which the badge goes: the card's price, or the card itself. */
  function findAnchor(card) {
    let scope = card;
    for (let depth = 0; depth <= CONFIG.listingDepth && scope && scope !== document.body; depth += 1) {
      // Stop once the scope holds several cards: we've left this card's listing.
      if (depth > 0 && scope.querySelectorAll(CONFIG.selectors.cardRoot).length > 1) break;
      const price = priceBySelector(scope, card) || (depth > 0 ? priceByText(scope, card) : null);
      if (price) return blockOf(price, scope);
      scope = scope.parentElement;
    }
    return card;
  }

  /** If the price sits inside a horizontal flex row, put the badge under the whole row. */
  function blockOf(node, limit) {
    const parent = node.parentElement;
    if (parent && parent !== limit) {
      const style = getComputedStyle(parent);
      if (style.display.includes('flex') && style.flexDirection.startsWith('row')) return parent;
    }
    return node;
  }

  // ---------------------------------------------------------------------------
  //  Badge rendering
  // ---------------------------------------------------------------------------

  const fmtInt = (n) => (n === null || n === undefined ? '—' : Math.round(n).toLocaleString('fr-FR'));
  const fmtSigned = (n, digits = 0) => (n > 0 ? '+' : n < 0 ? '−' : '±')
    + Math.abs(n).toLocaleString('fr-FR', { minimumFractionDigits: digits, maximumFractionDigits: digits });

  function createBadge(name) {
    const badge = el('div', `${PREFIX}-badge ${PREFIX}-loading`, 'Trend…');
    badge.dataset.wmhudName = name;
    badge.setAttribute('role', 'status');
    badge.title = `WikiMasters HUD — chargement de « ${name} »`;
    return badge;
  }

  function renderOk(badge, data) {
    const score = data.trend_score;
    const evo = data.evolution_pct;
    const period = data.raw?.window === '24h' ? '24 h' : data.raw?.window === '1d' ? '1 j' : '';

    let tone = 'flat';
    if (typeof score === 'number') {
      if (score >= CONFIG.neutralBand) tone = 'up';
      else if (score <= -CONFIG.neutralBand) tone = 'down';
    } else if (typeof evo === 'number') {
      tone = evo >= 10 ? 'up' : evo <= -10 ? 'down' : 'flat'; // ML down: raw evolution only
    }
    const arrow = { up: '▲', down: '▼', flat: '■' }[tone];

    badge.className = `${PREFIX}-badge ${PREFIX}-${tone}`;
    badge.replaceChildren(
      el('span', '', `${arrow} Trend ${typeof score === 'number' ? fmtSigned(score) : 'n/d'}`),
    );
    if (typeof evo === 'number') {
      badge.append(el('span', `${PREFIX}-sep`, '·'), el('span', `${PREFIX}-evo`, `${fmtSigned(evo)} % ${period}`.trim()));
    }
    if (typeof data.spike_probability === 'number' && data.spike_probability >= CONFIG.flameAt) {
      badge.append(el('span', '', '🔥'));
    }

    const lines = [
      `${data.article?.replace(/_/g, ' ') ?? data.name} — WikiMasters HUD`,
      `Trend Score : ${typeof score === 'number' ? fmtSigned(score) : 'indisponible'} (−100 → +100, prédiction 7 j)`,
      `Évolution brute ${period} : ${typeof evo === 'number' ? `${fmtSigned(evo, 1)} %` : 'n/d'}`
        + ` (${fmtInt(data.raw?.views_current)} vues vs ${fmtInt(data.raw?.views_previous)})`,
    ];
    if (typeof data.spike_probability === 'number') {
      lines.push(`Probabilité de pic +300 % sous 7 j : ${Math.round(data.spike_probability * 100)} %`);
    }
    if (data.prediction_error) lines.push(`Prédiction indisponible (${data.prediction_error})`);
    if (data.model?.version) lines.push(`Modèle : ${data.model.version}`);
    if (data.raw?.updated_at) lines.push(`Mis à jour : ${new Date(data.raw.updated_at).toLocaleString('fr-FR')}`);
    badge.title = lines.join('\n');
  }

  function renderMuted(badge, text, title) {
    badge.className = `${PREFIX}-badge ${PREFIX}-muted`;
    badge.replaceChildren(el('span', '', text));
    badge.title = title;
  }

  function render(badge, result) {
    if (!badge.isConnected) return;
    switch (result.kind) {
      case 'ok':
        renderOk(badge, result.data);
        break;
      case 'pending':
        renderMuted(badge, '⏳ Suivi en cours', 'Carte ajoutée au tracker : les données arrivent dans une minute.');
        break;
      case 'unknown':
        renderMuted(badge, '∅ Pas d\'article', 'Aucun article Wikipédia trouvé pour cette carte.');
        break;
      case 'offline':
        renderMuted(badge, '⚠ HUD hors ligne', `API injoignable (${CONFIG.apiBase}). Vérifiez l'URL dans le menu Tampermonkey.`);
        break;
      default:
        renderMuted(badge, '⚠ Erreur', result.message || 'Erreur inconnue');
    }
  }

  async function fill(badge, name) {
    const result = await Api.getCard(name);
    render(badge, result);
    if (result.kind === 'pending' || result.kind === 'offline') {
      const delay = result.kind === 'pending' ? CONFIG.pendingRetryMs : CONFIG.offlineBackoffMs;
      setTimeout(() => badge.isConnected && fill(badge, name), delay + 250);
    }
  }

  // ---------------------------------------------------------------------------
  //  Scanner + MutationObserver
  // ---------------------------------------------------------------------------

  const attached = new WeakMap(); // card element -> { name, badge }

  function attach(card, name) {
    const state = attached.get(card);
    if (state && state.name === name && state.badge.isConnected) return;
    state?.badge.remove(); // React recycled the node for another card

    const badge = createBadge(name);
    findAnchor(card).insertAdjacentElement('afterend', badge);
    attached.set(card, { name, badge });
    log('badge attached', name);
    fill(badge, name);
  }

  function scan() {
    try {
      for (const { card, name } of findCards()) attach(card, name);
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
    // Ignore the mutations caused by our own badges.
    const relevant = mutations.some((m) => {
      const target = m.target instanceof Element ? m.target : m.target.parentElement;
      if (!target || isOurs(target)) return false;
      if (m.type === 'characterData') return true; // e.g. React swapped a card name
      return [...m.addedNodes, ...m.removedNodes].some((n) => !isBadge(n));
    });
    if (relevant) scheduleScan();
  });

  function refreshAll() {
    Api.clear();
    for (const badge of document.querySelectorAll(`.${PREFIX}-badge`)) fill(badge, badge.dataset.wmhudName);
  }

  // ---------------------------------------------------------------------------
  //  Tampermonkey menu
  // ---------------------------------------------------------------------------

  function registerMenu() {
    GM_registerMenuCommand('⚙️ URL de l\'API…', () => {
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
    GM_registerMenuCommand(`🐞 Debug : ${CONFIG.debug ? 'ON' : 'OFF'} (recharger)`, () => {
      GM_setValue('debug', !CONFIG.debug);
    });
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
