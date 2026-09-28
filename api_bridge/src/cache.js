/** Tiny TTL + LRU cache (Map keeps insertion order). */
export class TtlCache {
  constructor({ maxEntries = 5000, now = () => Date.now() } = {}) {
    this.maxEntries = maxEntries;
    this.now = now;
    this.map = new Map();
  }

  get(key) {
    const entry = this.map.get(key);
    if (!entry) return undefined;
    if (entry.expires <= this.now()) {
      this.map.delete(key);
      return undefined;
    }
    this.map.delete(key); // refresh LRU position
    this.map.set(key, entry);
    return entry.value;
  }

  set(key, value, ttlSeconds) {
    if (ttlSeconds <= 0) return;
    this.map.delete(key);
    this.map.set(key, { value, expires: this.now() + ttlSeconds * 1000 });
    while (this.map.size > this.maxEntries) {
      this.map.delete(this.map.keys().next().value);
    }
  }
}
