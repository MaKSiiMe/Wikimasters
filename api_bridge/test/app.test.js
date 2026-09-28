import assert from 'node:assert/strict';
import { beforeEach, describe, test } from 'node:test';

import { buildApp } from '../src/app.js';
import { loadConfig } from '../src/config.js';
import { normalizeTitle } from '../src/normalize.js';
import { FakeRedis, fakeMlFetch } from './fakes.js';

const ORIGIN = 'https://www.wiki-masters.com';

async function setup({ predictions = {}, mlFail = false, env = {} } = {}) {
  const redis = new FakeRedis();
  const fetch = fakeMlFetch(predictions, { fail: mlFail });
  const config = loadConfig({ ML_API_URL: 'http://ml:8000', ...env });
  const app = await buildApp({ config, redis, fetch, logger: false });
  return { app, redis, fetch };
}

function seedSaturne(redis) {
  redis.hset('wm:alias', { 'Saturne_(planète)': 'Saturne_(planète)' });
  redis.hset('wm:stats:Saturne_(planète)', {
    window: '24h', views_current: '1200', views_previous: '300', evolution_pct: '300.0',
    is_spike: '1', as_of: '2026092811', updated_at: '2026-09-28T12:05:00+00:00',
    rarity_as_of: '20260927', views_30d: '4800', rarity_30d: 'SR', views_prev_month: '4100',
    rarity_prev_month: 'SR', next_rarity: 'UR', views_to_next: '200', eta_days: '2', downgrade_eta_days: '',
  });
}

const SATURNE_PRED = {
  'Saturne_(planète)': {
    trend_score: 42, spike_probability: 0.61, direction: 'up', model: 'lstm-test', as_of: '2026-09-27', horizon_days: 7,
  },
};

describe('normalizeTitle', () => {
  test('matches the Python implementation', () => {
    assert.equal(normalizeTitle('  saturne   (planète) '), 'Saturne_(planète)');
    assert.equal(normalizeTitle('éléphant'), 'Éléphant');
    assert.equal(normalizeTitle('Mont_Fuji'), 'Mont_Fuji');
    assert.equal(normalizeTitle('   '), '');
  });
});

describe('GET /api/card/:name', () => {
  let ctx;
  beforeEach(async () => {
    ctx = await setup({ predictions: SATURNE_PRED });
    seedSaturne(ctx.redis);
  });

  test('returns raw evolution from Redis and the ML score', async () => {
    const res = await ctx.app.inject({ url: `/api/card/${encodeURIComponent('saturne (planète)')}`, headers: { origin: ORIGIN } });
    assert.equal(res.statusCode, 200);
    const body = res.json();
    assert.equal(body.status, 'ok');
    assert.equal(body.name, 'saturne (planète)');
    assert.equal(body.article, 'Saturne_(planète)');
    assert.equal(body.evolution_pct, 300);
    assert.equal(body.trend_score, 42);
    assert.equal(body.spike_probability, 0.61);
    assert.deepEqual(body.raw, {
      window: '24h', views_current: 1200, views_previous: 300, is_spike: true,
      as_of: '2026092811', updated_at: '2026-09-28T12:05:00+00:00',
    });
    assert.equal(body.wiki_url, 'https://fr.wikipedia.org/wiki/Saturne_(plan%C3%A8te)');
    assert.equal(res.headers['access-control-allow-origin'], ORIGIN);
    assert.equal(ctx.fetch.calls[0], 'http://ml:8000/predict?article=Saturne_(plan%C3%A8te)');
  });

  test('caches responses and logs every request to a Redis stream', async () => {
    await ctx.app.inject({ url: '/api/card/Saturne_(planète)' });
    await ctx.app.inject({ url: '/api/card/Saturne_(planète)' });
    assert.equal(ctx.fetch.calls.length, 1);
    await new Promise((r) => setImmediate(r));
    const log = ctx.redis.streams.get('wm:bridge:requests');
    assert.deepEqual(log.map((e) => e.cache), ['miss', 'hit']);
  });

  test('unknown card is queued for the tracker (202)', async () => {
    const res = await ctx.app.inject({ url: '/api/card/Mont%20Fuji' });
    assert.equal(res.statusCode, 202);
    assert.equal(res.json().status, 'pending');
    assert.ok(ctx.redis.sets.get('wm:articles:pending').has('Mont_Fuji'));
  });

  test('pending queue is bounded', async () => {
    const small = await setup({ env: { MAX_PENDING: '0' } });
    const res = await small.app.inject({ url: '/api/card/Chat' });
    assert.equal(res.json().queued, false);
    assert.equal(small.redis.sets.get('wm:articles:pending'), undefined);
  });

  test('names without Wikipedia article are 404', async () => {
    ctx.redis.sets.set('wm:articles:invalid', new Set(['Pas_une_carte']));
    const res = await ctx.app.inject({ url: '/api/card/Pas%20une%20carte' });
    assert.equal(res.statusCode, 404);
    assert.equal(res.json().status, 'unknown');
  });

  test('ML outage degrades gracefully', async () => {
    const down = await setup({ mlFail: true });
    seedSaturne(down.redis);
    const body = (await down.app.inject({ url: '/api/card/Saturne_(planète)' })).json();
    assert.equal(body.evolution_pct, 300);
    assert.equal(body.trend_score, null);
    assert.equal(body.prediction_error, 'ml_unavailable');
  });

  test('Redis outage is 503', async () => {
    ctx.redis.down = true;
    const res = await ctx.app.inject({ url: '/api/card/Chat' });
    assert.equal(res.statusCode, 503);
  });

  test('rejects over-long names', async () => {
    const res = await ctx.app.inject({ url: `/api/card/${'a'.repeat(250)}` });
    assert.equal(res.statusCode, 400);
  });
});

describe('POST /api/cards', () => {
  test('answers every card of the page in one request, with signals', async () => {
    const { app, redis, fetch } = await setup({ predictions: SATURNE_PRED });
    seedSaturne(redis);
    const res = await app.inject({
      method: 'POST',
      url: '/api/cards',
      headers: { origin: ORIGIN },
      payload: { cards: [
        { title: 'Saturne (planète)', name: 'Saturne', rarity: 'SR', price: 800, avg_price: 1000 },
        { name: 'Mont Fuji' },
      ] },
    });
    assert.equal(res.statusCode, 200);
    const [saturne, fuji] = res.json().results;
    assert.equal(saturne.http_status, 200);
    assert.equal(saturne.name, 'Saturne');
    assert.equal(saturne.rarity.estimated_30d, 'SR');
    assert.equal(saturne.rarity.eta_days, 2);
    assert.equal(saturne.rarity.downgrade_eta_days, null);
    assert.equal(saturne.signals.verdict, 'buy');
    assert.equal(saturne.signals.price_delta_pct, -20);
    assert.deepEqual(saturne.signals.reasons.map((r) => r.code), ['near_upgrade', 'hype', 'cheap']);
    assert.equal(fuji.http_status, 202);
    assert.equal(fetch.calls.length, 1);
  });

  test('rejects oversized or malformed batches', async () => {
    const { app } = await setup();
    const tooMany = Array.from({ length: 61 }, (_, i) => ({ name: `c${i}` }));
    assert.equal((await app.inject({ method: 'POST', url: '/api/cards', payload: { cards: tooMany } })).statusCode, 400);
    assert.equal((await app.inject({ method: 'POST', url: '/api/cards', payload: { cards: [{ rarity: 'SR' }] } })).statusCode, 400);
    assert.equal((await app.inject({ method: 'POST', url: '/api/cards', payload: { cards: [{ name: 'x', rarity: 'XX' }] } })).statusCode, 400);
  });
});

describe('POST /api/observations', () => {
  test('stores observations and the latest state per card', async () => {
    const { app, redis } = await setup();
    const res = await app.inject({
      method: 'POST',
      url: '/api/observations',
      payload: { observations: [{
        seen_at: '2026-09-28T06:00:00Z', page: 'marketplace', card_id: 'c-1', listing_id: 'l-1',
        title: 'Chat', rarity: 'L', price: { current: 1200, base: 900 }, avg_price: 1100,
        fields: { views_30d: 22227, q_score: 98.11 },
      }] },
    });
    assert.equal(res.statusCode, 202);
    assert.equal(res.json().stored, 1);
    const [entry] = redis.streams.get('wm:observations');
    assert.equal(entry.key, 'Chat');
    assert.equal(JSON.parse(entry.data).fields.q_score, 98.11);
    assert.deepEqual(await redis.hGetAll('wm:obs:latest:Chat'), {
      seen_at: '2026-09-28T06:00:00Z', card_id: 'c-1', rarity_displayed: 'L', avg_price: '1100',
      price_current: '1200', page: 'marketplace',
    });
  });
});

describe('CORS', () => {
  test('only https://www.wiki-masters.com is allowed', async () => {
    const { app, redis } = await setup({ predictions: SATURNE_PRED });
    seedSaturne(redis);
    const evil = await app.inject({ url: '/api/card/Chat', headers: { origin: 'https://evil.example' } });
    assert.equal(evil.statusCode, 403);
    assert.equal(evil.headers['access-control-allow-origin'], undefined);
    const lookalike = await app.inject({ url: '/api/card/Chat', headers: { origin: 'https://www.wiki-masters.com.evil.example' } });
    assert.equal(lookalike.statusCode, 403);
    const http = await app.inject({ url: '/api/card/Chat', headers: { origin: 'http://www.wiki-masters.com' } });
    assert.equal(http.statusCode, 403);
  });

  test('preflight from the game allows GET and private network access', async () => {
    const { app } = await setup();
    const res = await app.inject({
      method: 'OPTIONS',
      url: '/api/card/Chat',
      headers: {
        origin: ORIGIN,
        'access-control-request-method': 'GET',
        'access-control-request-private-network': 'true',
      },
    });
    assert.equal(res.statusCode, 204);
    assert.equal(res.headers['access-control-allow-origin'], ORIGIN);
    assert.match(res.headers['access-control-allow-methods'], /GET/);
    assert.match(res.headers['access-control-allow-methods'], /POST/);
    assert.equal(res.headers['access-control-allow-private-network'], 'true');
    assert.equal(res.headers['access-control-allow-credentials'], undefined);
  });
});

describe('GET /health', () => {
  test('reports redis and ML status', async () => {
    const { app } = await setup();
    const body = (await app.inject({ url: '/health' })).json();
    assert.deepEqual(body, { status: 'ok', redis: true, ml: { status: 'ok', model: 'test' } });
  });
});
