/**
 * Turn raw numbers into a trading signal the HUD can show: a verdict plus the
 * reasons behind it. Rules are deliberately simple and transparent; the ML
 * score is attached for information until it beats these rules in backtests.
 */

export const RARITIES = ['C', 'PC', 'R', 'SR', 'UR', 'L'];
const rank = (code) => RARITIES.indexOf(code);

export const DEFAULTS = {
  etaBuyDays: 7, // next tier reachable within a week
  hypePct: 100, // +100 % over 24h
  hypeMinViews: 200,
  coolingPct: -40,
  expensivePct: 25, // bid more than 25 % above the average sale price
  cheapPct: -15,
};

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/**
 * @param {object} p
 * @param {object} p.rarity      tracker outlook (estimated_30d, next, eta_days, ...)
 * @param {object} p.raw         tracker evolution (window, views_current, ...)
 * @param {number|null} p.evolutionPct
 * @param {string|null} p.displayedRarity  rarity shown by the game for this card
 * @param {number|null} p.price            current bid / asking price seen in the game
 * @param {number|null} p.avgPrice         game's average sale price for that rarity
 */
export function buildSignals({ rarity, raw, evolutionPct, displayedRarity, price, avgPrice }, rules = DEFAULTS) {
  const reasons = [];
  const add = (code, kind, weight, text) => reasons.push({ code, kind, weight, text });

  const shown = RARITIES.includes(displayedRarity) ? displayedRarity : null;
  const estimated = rarity?.estimated_30d ?? null;

  if (shown && estimated && rank(estimated) > rank(shown)) {
    add('rarity_upgrade', 'buy', 3, `⭐ Trafic de ${estimated} (affichée ${shown})`);
  } else if (shown && estimated && rank(estimated) < rank(shown)) {
    add('rarity_downgrade', 'sell', 3, `⚠ Trafic de ${estimated} seulement (affichée ${shown})`);
  }

  const eta = num(rarity?.eta_days);
  if (rarity?.next && eta !== null && eta <= rules.etaBuyDays) {
    add('near_upgrade', 'buy', 2, `📈 ${rarity.next} dans ~${eta} j`);
  }
  const down = num(rarity?.downgrade_eta_days);
  if (down !== null && down <= rules.etaBuyDays) {
    add('near_downgrade', 'sell', 2, `📉 Perd ${estimated ?? 'sa rareté'} dans ~${down} j`);
  }

  const evo = num(evolutionPct);
  const current = num(raw?.views_current);
  const period = raw?.window === '24h' ? '24 h' : '1 j';
  if (evo !== null && evo >= rules.hypePct && (current ?? 0) >= rules.hypeMinViews) {
    add('hype', 'buy', 2, `🚀 +${Math.round(evo)} % en ${period}`);
  } else if (evo !== null && evo <= rules.coolingPct) {
    add('cooling', 'sell', 1, `⬇️ −${Math.abs(Math.round(evo))} % en ${period}`);
  }

  const p = num(price);
  const avg = num(avgPrice);
  let priceDeltaPct = null;
  if (p !== null && avg) {
    priceDeltaPct = Math.round(((p - avg) / avg) * 100);
    if (priceDeltaPct <= rules.cheapPct) add('cheap', 'price', 1, `💰 −${Math.abs(priceDeltaPct)} % vs moy.`);
    else if (priceDeltaPct >= rules.expensivePct) add('expensive', 'price', -2, `💸 +${priceDeltaPct} % vs moy.`);
  }

  const score = (kind) => reasons.filter((r) => r.kind === kind).reduce((s, r) => s + r.weight, 0);
  const buy = score('buy');
  const sell = score('sell');
  const priceAdj = score('price');

  let verdict = 'neutral';
  if (buy > sell && buy + Math.min(priceAdj, 0) >= 2) verdict = 'buy';
  else if (sell > buy && sell >= 2) verdict = 'sell';
  else if (buy > 0 || sell > 0) verdict = 'watch';

  reasons.sort((a, b) => Math.abs(b.weight) - Math.abs(a.weight));
  return { verdict, reasons, price_delta_pct: priceDeltaPct };
}
