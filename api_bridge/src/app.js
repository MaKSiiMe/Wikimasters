import Fastify from 'fastify';
import cors from '@fastify/cors';
import rateLimit from '@fastify/rate-limit';

import { TtlCache } from './cache.js';
import { normalizeTitle } from './normalize.js';
import { RARITIES, buildSignals } from './signals.js';

// Redis schema shared with the tracker (see tracker/storage.py).
export const KEYS = {
  alias: 'wm:alias',
  pending: 'wm:articles:pending',
  invalid: 'wm:articles:invalid',
  stats: (title) => `wm:stats:${title}`,
  requests: 'wm:bridge:requests',
  observations: 'wm:observations',
  observedLatest: (title) => `wm:obs:latest:${title}`,
};

const REQUEST_LOG_MAXLEN = 10000;
const OBSERVATIONS_MAXLEN = 50000;
const OBSERVATION_MAX_BYTES = 4000;
const PENDING_TTL_SECONDS = 15;
const UNKNOWN_TTL_SECONDS = 300;
const MAX_BATCH = 60;

const toNumber = (value) => {
  if (value === undefined || value === null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

// --- JSON schemas ------------------------------------------------------------

const cardContext = {
  name: { type: 'string', minLength: 1, maxLength: 200 },
  title: { type: 'string', minLength: 1, maxLength: 300 },
  rarity: { type: 'string', enum: RARITIES },
  price: { type: 'number', minimum: 0 },
  avg_price: { type: 'number', minimum: 0 },
};

const getCardSchema = {
  params: {
    type: 'object',
    required: ['name'],
    properties: { name: cardContext.name },
  },
  querystring: {
    type: 'object',
    properties: { title: cardContext.title, rarity: cardContext.rarity, price: cardContext.price, avg_price: cardContext.avg_price },
  },
};

const batchSchema = {
  body: {
    type: 'object',
    required: ['cards'],
    properties: {
      cards: {
        type: 'array',
        minItems: 1,
        maxItems: MAX_BATCH,
        items: { type: 'object', properties: cardContext, anyOf: [{ required: ['name'] }, { required: ['title'] }] },
      },
    },
  },
};

const observationSchema = {
  body: {
    type: 'object',
    required: ['observations'],
    properties: {
      observations: {
        type: 'array',
        minItems: 1,
        maxItems: 100,
        items: {
          type: 'object',
          properties: {
            seen_at: { type: 'string', maxLength: 40 },
            page: { type: 'string', maxLength: 40 },
            card_id: { type: 'string', maxLength: 64 },
            listing_id: { type: 'string', maxLength: 64 },
            title: cardContext.title,
            name: cardContext.name,
            rarity: cardContext.rarity,
            price: { type: 'object' },
            avg_price: { type: 'number' },
            sales_summary: { type: 'object' },
            fields: { type: 'object' },
          },
          anyOf: [{ required: ['name'] }, { required: ['title'] }],
        },
      },
    },
  },
};

/**
 * Build the Fastify app. Dependencies are injected so tests can use fakes:
 *   redis   — connected node-redis client (hGet, hGetAll, hSet, sIsMember, sCard, sAdd, xAdd, ping)
 *   fetch   — WHATWG fetch used to reach the ML inference API
 */
export async function buildApp({ config, redis, fetch = globalThis.fetch, logger = true }) {
  const app = Fastify({
    logger: logger === true ? { level: config.logLevel } : logger,
    trustProxy: config.trustProxy,
    bodyLimit: 256 * 1024,
    routerOptions: { maxParamLength: 300 },
  });
  const allowed = new Set(config.corsOrigins);
  const cache = new TtlCache({ maxEntries: config.cacheMaxEntries });

  // --- CORS: only https://www.wiki-masters.com (configurable, exact match) ---
  // Browsers enforce CORS, but a request from any other *browser* origin is
  // refused outright instead of being processed and merely hidden.
  app.addHook('onRequest', async (request, reply) => {
    const origin = request.headers.origin;
    if (origin && !allowed.has(origin)) {
      return reply.code(403).send({ status: 'error', error: 'origin_not_allowed' });
    }
  });
  await app.register(cors, {
    origin: [...allowed],
    methods: ['GET', 'POST'],
    allowedHeaders: ['Content-Type'],
    credentials: false,
    maxAge: 600,
    strictPreflight: true,
  });
  app.addHook('onSend', async (request, reply, payload) => {
    reply.header('X-Content-Type-Options', 'nosniff');
    reply.header('Referrer-Policy', 'no-referrer');
    reply.header('Cross-Origin-Resource-Policy', 'cross-origin');
    // Chrome Private Network Access: a public HTTPS page calling an API on
    // localhost / the LAN / a tailnet must be explicitly allowed by the preflight.
    if (request.method === 'OPTIONS' && request.headers['access-control-request-private-network'] === 'true'
        && allowed.has(request.headers.origin)) {
      reply.header('Access-Control-Allow-Private-Network', 'true');
    }
    return payload;
  });

  await app.register(rateLimit, {
    max: config.rateLimitPerMinute,
    timeWindow: '1 minute',
    allowList: (request) => request.url === '/health',
  });

  // --- helpers ---------------------------------------------------------------

  const wikiUrl = (title) => `https://${config.wikiProject}/wiki/${encodeURIComponent(title)}`;

  async function fetchPrediction(title) {
    try {
      const res = await fetch(`${config.mlApiUrl}/predict?article=${encodeURIComponent(title)}`, {
        signal: AbortSignal.timeout(config.mlTimeoutMs),
        headers: { accept: 'application/json' },
      });
      if (res.status === 404) return { error: 'no_history' };
      if (!res.ok) return { error: `ml_http_${res.status}` };
      return { data: await res.json() };
    } catch (err) {
      app.log.warn({ err: err.message, title }, 'ML inference unreachable');
      return { error: 'ml_unavailable' };
    }
  }

  const stringFields = (fields) => Object.fromEntries(
    Object.entries(fields).map(([k, v]) => [k, v === undefined || v === null ? '' : String(v)]),
  );

  function streamAdd(key, fields, maxlen) {
    return redis.xAdd(key, '*', stringFields(fields), {
      TRIM: { strategy: 'MAXLEN', strategyModifier: '~', threshold: maxlen },
    });
  }

  function logRequest(fields) {
    streamAdd(KEYS.requests, fields, REQUEST_LOG_MAXLEN)
      .catch((err) => app.log.debug({ err: err.message }, 'request log failed'));
  }

  function rarityFromStats(stats) {
    if (!stats?.rarity_30d) return null;
    return {
      estimated_30d: stats.rarity_30d,
      estimated_prev_month: stats.rarity_prev_month || null,
      views_30d: toNumber(stats.views_30d),
      views_prev_month: toNumber(stats.views_prev_month),
      next: stats.next_rarity || null,
      views_to_next: toNumber(stats.views_to_next),
      eta_days: toNumber(stats.eta_days),
      downgrade_eta_days: toNumber(stats.downgrade_eta_days),
      as_of: stats.rarity_as_of || null,
    };
  }

  /** Everything that only depends on the article (cacheable). */
  async function loadBase(key) {
    const title = await redis.hGet(KEYS.alias, key);
    if (!title) {
      if (await redis.sIsMember(KEYS.invalid, key)) {
        return { code: 404, ttl: UNKNOWN_TTL_SECONDS, body: { status: 'unknown', error: 'no_wikipedia_article' } };
      }
      // Unknown card: ask the tracker to start following it (bounded queue).
      let queued = false;
      if ((await redis.sCard(KEYS.pending)) < config.maxPending) {
        await redis.sAdd(KEYS.pending, key);
        queued = true;
      }
      return {
        code: 202,
        ttl: PENDING_TTL_SECONDS,
        body: { status: 'pending', article: key, queued, message: 'tracking will start within a minute' },
      };
    }

    const [stats, prediction] = await Promise.all([redis.hGetAll(KEYS.stats(title)), fetchPrediction(title)]);
    const ml = prediction.data;
    return {
      code: 200,
      ttl: config.cacheTtlSeconds,
      body: {
        status: 'ok',
        article: title,
        wiki_url: wikiUrl(title),
        // Raw evolution computed by the tracker from Redis history.
        evolution_pct: toNumber(stats?.evolution_pct),
        // Predictive score from the ML inference API (-100..100), experimental.
        trend_score: ml ? toNumber(ml.trend_score) : null,
        spike_probability: ml ? toNumber(ml.spike_probability) : null,
        direction: ml?.direction ?? null,
        raw: {
          window: stats?.window || null,
          views_current: toNumber(stats?.views_current),
          views_previous: toNumber(stats?.views_previous),
          is_spike: stats?.is_spike === '1',
          as_of: stats?.as_of || null,
          updated_at: stats?.updated_at || null,
        },
        rarity: rarityFromStats(stats),
        model: ml ? { version: ml.model, as_of: ml.as_of, horizon_days: ml.horizon_days } : null,
        ...(prediction.error ? { prediction_error: prediction.error } : {}),
      },
    };
  }

  async function getBase(key) {
    let result = cache.get(key);
    const hit = Boolean(result);
    if (!result) {
      result = await loadBase(key);
      cache.set(key, result, result.ttl);
    }
    return { result, hit };
  }

  /** Card response = cached article data + what the HUD saw in the game. */
  async function cardResponse(ctx) {
    const key = normalizeTitle(ctx.title || ctx.name);
    if (!key) return { code: 400, body: { status: 'error', error: 'empty_name' }, hit: false };
    const { result, hit } = await getBase(key);
    const body = { ...result.body, name: ctx.name ?? ctx.title ?? key };
    if (result.code === 200) {
      body.displayed_rarity = ctx.rarity ?? null;
      body.signals = buildSignals({
        rarity: body.rarity,
        raw: body.raw,
        evolutionPct: body.evolution_pct,
        displayedRarity: ctx.rarity,
        price: ctx.price,
        avgPrice: ctx.avg_price,
      });
    }
    return { code: result.code, body, hit, ttl: result.ttl };
  }

  // --- routes ----------------------------------------------------------------

  app.get('/api/card/:name', { schema: getCardSchema }, async (request, reply) => {
    const started = performance.now();
    const ctx = { ...request.query, name: request.params.name.trim() };
    const res = await cardResponse(ctx);
    logRequest({ route: 'card', name: ctx.name, code: res.code, cache: res.hit ? 'hit' : 'miss', ms: Math.round(performance.now() - started) });
    if (res.ttl) reply.header('Cache-Control', `private, max-age=${Math.min(res.ttl, config.cacheTtlSeconds)}`);
    return reply.code(res.code).send(res.body);
  });

  // One request for every card on screen (the marketplace shows dozens).
  app.post('/api/cards', { schema: batchSchema }, async (request, reply) => {
    const started = performance.now();
    const results = await Promise.all(request.body.cards.map(async (ctx) => {
      const res = await cardResponse(ctx);
      return { http_status: res.code, ...res.body };
    }));
    logRequest({ route: 'cards', count: results.length, ms: Math.round(performance.now() - started) });
    return reply.send({ results });
  });

  // What the HUD saw in the game (rarity, prices...): raw material to learn the
  // game's rarity rule and to score our signals against real prices.
  app.post('/api/observations', { schema: observationSchema }, async (request, reply) => {
    let stored = 0;
    for (const obs of request.body.observations) {
      const json = JSON.stringify(obs);
      if (json.length > OBSERVATION_MAX_BYTES) continue;
      const key = normalizeTitle(obs.title || obs.name);
      await streamAdd(KEYS.observations, { key, data: json }, OBSERVATIONS_MAXLEN);
      const latest = {
        seen_at: obs.seen_at || new Date().toISOString(),
        card_id: obs.card_id,
        rarity_displayed: obs.rarity,
        avg_price: obs.avg_price,
        price_current: obs.price?.current ?? obs.price?.effective ?? obs.price?.base,
        page: obs.page,
      };
      await redis.hSet(KEYS.observedLatest(key), stringFields(Object.fromEntries(
        Object.entries(latest).filter(([, v]) => v !== undefined && v !== null),
      )));
      stored += 1;
    }
    return reply.code(202).send({ status: 'ok', stored });
  });

  app.get('/health', async (request, reply) => {
    let redisOk = false;
    try {
      redisOk = (await redis.ping()) === 'PONG';
    } catch {
      redisOk = false;
    }
    let ml = { status: 'unreachable' };
    try {
      const res = await fetch(`${config.mlApiUrl}/health`, { signal: AbortSignal.timeout(config.mlTimeoutMs) });
      if (res.ok) ml = await res.json();
    } catch {
      // keep "unreachable"
    }
    return reply.code(redisOk ? 200 : 503).send({ status: redisOk ? 'ok' : 'degraded', redis: redisOk, ml });
  });

  app.setErrorHandler((err, request, reply) => {
    if (err.validation) {
      return reply.code(400).send({ status: 'error', error: 'invalid_request', message: err.message });
    }
    if (err.statusCode === 429) return reply.code(429).send({ status: 'error', error: 'rate_limited' });
    if (err.statusCode === 413) return reply.code(413).send({ status: 'error', error: 'payload_too_large' });
    request.log.error({ err }, 'request failed');
    return reply.code(503).send({ status: 'error', error: 'backend_unavailable' });
  });

  return app;
}
