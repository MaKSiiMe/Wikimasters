const int = (value, fallback) => {
  const n = Number.parseInt(value ?? '', 10);
  return Number.isFinite(n) ? n : fallback;
};

export function loadConfig(env = process.env) {
  return {
    host: env.HOST ?? '0.0.0.0',
    port: int(env.PORT, 3000),
    logLevel: env.LOG_LEVEL ?? 'info',
    redisUrl: env.REDIS_URL ?? 'redis://localhost:6379/0',
    mlApiUrl: (env.ML_API_URL ?? 'http://localhost:8000').replace(/\/+$/, ''),
    mlTimeoutMs: int(env.ML_TIMEOUT_MS, 2000),
    wikiProject: env.WIKI_PROJECT ?? 'fr.wikipedia.org',
    // Exact origins only: no wildcard, no regex, no credentials.
    corsOrigins: (env.CORS_ORIGINS ?? 'https://www.wiki-masters.com')
      .split(',')
      .map((o) => o.trim().replace(/\/+$/, ''))
      .filter(Boolean),
    cacheTtlSeconds: int(env.CACHE_TTL_SECONDS, 60),
    cacheMaxEntries: int(env.CACHE_MAX_ENTRIES, 5000),
    rateLimitPerMinute: int(env.RATE_LIMIT_PER_MINUTE, 300),
    maxPending: int(env.MAX_PENDING, 500),
    trustProxy: ['1', 'true', 'yes'].includes(String(env.TRUST_PROXY ?? '').toLowerCase()),
  };
}
