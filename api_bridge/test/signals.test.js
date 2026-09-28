import assert from 'node:assert/strict';
import { test } from 'node:test';

import { buildSignals } from '../src/signals.js';

const codes = (s) => s.reasons.map((r) => r.code);

test('traffic above the displayed rarity is a buy signal', () => {
  const s = buildSignals({ rarity: { estimated_30d: 'UR', next: 'L', eta_days: null }, displayedRarity: 'SR' });
  assert.equal(s.verdict, 'buy');
  assert.deepEqual(codes(s), ['rarity_upgrade']);
});

test('approaching the next tier and hype add up', () => {
  const s = buildSignals({
    rarity: { estimated_30d: 'SR', next: 'UR', eta_days: 3 },
    raw: { window: '24h', views_current: 900 },
    evolutionPct: 240,
    displayedRarity: 'SR',
  });
  assert.equal(s.verdict, 'buy');
  assert.deepEqual(codes(s), ['near_upgrade', 'hype']);
  assert.match(s.reasons[0].text, /UR dans ~3 j/);
});

test('an overpriced listing cancels a weak buy signal', () => {
  const s = buildSignals({
    rarity: { estimated_30d: 'SR', next: 'UR', eta_days: 5 },
    displayedRarity: 'SR',
    price: 1500,
    avgPrice: 1000,
  });
  assert.equal(s.price_delta_pct, 50);
  assert.equal(s.verdict, 'watch');
  assert.ok(codes(s).includes('expensive'));
});

test('rarity about to drop after a spike is a sell signal', () => {
  const s = buildSignals({
    rarity: { estimated_30d: 'UR', next: 'L', downgrade_eta_days: 4 },
    raw: { window: '24h', views_current: 100 },
    evolutionPct: -70,
    displayedRarity: 'UR',
  });
  assert.equal(s.verdict, 'sell');
  assert.deepEqual(codes(s), ['near_downgrade', 'cooling']);
});

test('nothing notable stays neutral, small hype on tiny volume is ignored', () => {
  const s = buildSignals({ rarity: { estimated_30d: 'R', next: 'SR', eta_days: null }, raw: { views_current: 20 }, evolutionPct: 300, displayedRarity: 'R' });
  assert.equal(s.verdict, 'neutral');
  assert.deepEqual(s.reasons, []);
});
