/** Case/diacritic-insensitive fold used by every filter in the app. */
export function fold(s: string): string {
  return s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

/**
 * Subsequence fuzzy score: every query character must appear in order. Contiguous runs,
 * word starts, and a prefix match score higher; returns null when the query does not match.
 */
export function fuzzyScore(query: string, text: string): number | null {
  const q = fold(query).replace(/\s+/g, "");
  if (!q) return 0;
  const t = fold(text);
  let score = 0;
  let ti = 0;
  let run = 0;
  for (const ch of q) {
    const found = t.indexOf(ch, ti);
    if (found < 0) return null;
    const wordStart = found === 0 || /[\s\-_.:/(]/.test(t[found - 1]);
    run = found === ti ? run + 1 : 1;
    score += 1 + run * 2 + (wordStart ? 6 : 0) - Math.min(found - ti, 6) * 0.5;
    ti = found + 1;
  }
  if (t.startsWith(q)) score += 10;
  return score - t.length * 0.02;
}
