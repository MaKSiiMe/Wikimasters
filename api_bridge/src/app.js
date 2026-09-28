import Fastify from 'fastify';
import cors from '@fastify/cors';
import rateLimit from '@fastify/rate-limit';

import { TtlCache } from './cache.js';
import { normalizeTitle } from './normalize.js';

// Redis schema shared with the tracker (see tracker/storage.py).
export const KEYS = {
  alias: 'wm:alias',
  pending: 'wm:articles:pending',
  invalid: 'wm:articles:invalid',
  stats: (title) => `wm:stats:${title}`,
  requests: 'wm:bridge:requests',
};

const REQUEST_LOG_MAXLEN = 10000;
const PENDING_TTL_SECONDS = 15;
const UNKNOWN_TTL_SECONDS = 300;

const toNumber = (value) => {
  if (value === undefined || value === null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

const cardSchema = {
  params: {
    type: 'object',
    required: ['name'],
    properties: { name: { type: 'string', minLength: 1, maxLength: 200 } },
  },
};

/**
 * Build the Fastify app. Dependencies are injected so tests can use fakes:
 *   redis   — connected node-redis client (hGet, hGetAll, sIsMember, sCard, sAdd, xAdd, ping)
 *   fetch   — WHATWG fetch used to reach the ML inference API
 */
export async function buildApp({ config, redis, fetch = globalThis.fetch, logger = true }) {
  const app = Fastify({
    logger: logger === true ? { level: config.logLevel } : logger,
    trustProxy: config.trustProxy,
    // Names like "Saturne (planète)" arrive percent-encoded; keep them readable in logs.
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
    methods: ['GET'],
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
    // localhost / the LAN must be explicitly allowed by the preflight.
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

  function logRequest(fields) {
    const message = Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, String(v ?? '')]));
    redis
      .xAdd(KEYS.requests, '*', message, {
        TRIM: { strategy: 'MAXLEN', strategyModifier: '~', threshold: REQUEST_LOG_MAXLEN },
      })
      .catch((err) => app.log.debug({ err: err.message }, 'request log failed'));
  }

  async function buildCard(name, key) {
    const title = await redis.hGet(KEYS.alias, key);
    if (!title) {
      if (await redis.sIsMember(KEYS.invalid, key)) {
        return { code: 404, ttl: UNKNOWN_TTL_SECONDS, body: { status: 'unknown', name, error: 'no_wikipedia_article' } };
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
        body: { status: 'pending', name, article: key, queued, message: 'tracking will start within a minute' },
      };
    }

    const [stats, prediction] = await Promise.all([redis.hGetAll(KEYS.stats(title)), fetchPrediction(title)]);
    const ml = prediction.data;
    return {
      code: 200,
      ttl: config.cacheTtlSeconds,
      body: {
        status: 'ok',
        name,
        article: title,
        wiki_url: wikiUrl(title),
        // Raw evolution computed by the tracker from Redis history.
        evolution_pct: toNumber(stats?.evolution_pct),
        // Predictive score from the ML inference API (-100..100).
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
        model: ml ? { version: ml.model, as_of: ml.as_of, horizon_days: ml.horizon_days } : null,
        ...(prediction.error ? { prediction_error: prediction.error } : {}),
      },
    };
  }

  // --- routes ----------------------------------------------------------------

  app.get('/api/card/:name', { schema: cardSchema }, async (request, reply) => {
    const started = performance.now();
    const name = request.params.name.trim();
    const key = normalizeTitle(name);
    if (!key) return reply.code(400).send({ status: 'error', error: 'empty_name' });

    let result = cache.get(key);
    const hit = Boolean(result);
    if (!result) {
      result = await buildCard(name, key);
      cache.set(key, result, result.ttl);
    }
    logRequest({ name, key, code: result.code, cache: hit ? 'hit' : 'miss', ms: Math.round(performance.now() - started) });
    reply.header('Cache-Control', `private, max-age=${Math.min(result.ttl, config.cacheTtlSeconds)}`);
    return reply.code(result.code).send({ ...result.body, name });
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
    request.log.error({ err }, 'request failed');
    return reply.code(503).send({ status: 'error', error: 'backend_unavailable' });
  });

  return app;
}
