/** Minimal in-memory stand-in for the node-redis commands used by the app. */
export class FakeRedis {
  constructor() {
    this.hashes = new Map();
    this.sets = new Map();
    this.streams = new Map();
    this.down = false;
  }

  #check() {
    if (this.down) throw new Error('ECONNREFUSED');
  }

  hset(key, obj) {
    this.hashes.set(key, { ...(this.hashes.get(key) ?? {}), ...obj });
  }

  async hGet(key, field) {
    this.#check();
    return this.hashes.get(key)?.[field] ?? null;
  }

  async hSet(key, obj) {
    this.#check();
    this.hset(key, obj);
    return Object.keys(obj).length;
  }

  async hGetAll(key) {
    this.#check();
    return { ...(this.hashes.get(key) ?? {}) };
  }

  async sIsMember(key, member) {
    this.#check();
    return this.sets.get(key)?.has(member) ? 1 : 0;
  }

  async sCard(key) {
    this.#check();
    return this.sets.get(key)?.size ?? 0;
  }

  async sAdd(key, member) {
    this.#check();
    if (!this.sets.has(key)) this.sets.set(key, new Set());
    this.sets.get(key).add(member);
    return 1;
  }

  async xAdd(key, id, message) {
    this.#check();
    if (!this.streams.has(key)) this.streams.set(key, []);
    this.streams.get(key).push(message);
    return '0-1';
  }

  async ping() {
    this.#check();
    return 'PONG';
  }
}

/** fetch() double for the ML inference API. */
export function fakeMlFetch(predictions, { fail = false } = {}) {
  const calls = [];
  const fn = async (url) => {
    calls.push(url);
    if (fail) throw new Error('connect ECONNREFUSED');
    const u = new URL(url);
    if (u.pathname === '/health') return Response.json({ status: 'ok', model: 'test' });
    const article = u.searchParams.get('article');
    if (!(article in predictions)) return new Response('{}', { status: 404 });
    return Response.json(predictions[article]);
  };
  fn.calls = calls;
  return fn;
}
