/**
 * MediaWiki-style title normalization, identical to tracker/storage.py and
 * ml_predictor/features.py: "  saturne  (planète) " -> "Saturne_(planète)".
 */
export function normalizeTitle(name) {
  const text = String(name ?? '').replace(/_/g, ' ').split(/\s+/).filter(Boolean).join(' ');
  if (!text) return '';
  const first = String.fromCodePoint(text.codePointAt(0));
  return (first.toUpperCase() + text.slice(first.length)).replace(/ /g, '_');
}
