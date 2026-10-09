import type { ReactNode } from "react";

// Search-term highlighting for result snippets and the page reader.

// The backend wraps matched terms in snippets with these (see db.MATCH_START/MATCH_END).
const MATCH_START = "\u0002";
const MATCH_END = "\u0003";

// Length of a snippet as shown, without the match markers.
function visibleLength(text: string): number {
  return text.replaceAll(MATCH_START, "").replaceAll(MATCH_END, "").length;
}

/** Snippet text with matched terms as <mark>, trimmed to ~max chars around the first match. */
export function markedSnippet(snippet: string, max = 280): ReactNode[] {
  let text = (snippet ?? "").replace(/\s+/g, " ").trim();
  const first = text.indexOf(MATCH_START);
  if (visibleLength(text) > max && first > max / 2) {
    text = "…" + text.slice(first - Math.floor(max / 3));
  }
  if (visibleLength(text) > max) {
    // Cut on plain-text length, keeping marker pairs balanced.
    let visible = 0;
    let cut = text.length;
    for (let i = 0; i < text.length; i++) {
      if (text[i] !== MATCH_START && text[i] !== MATCH_END && ++visible > max - 1) {
        cut = i;
        break;
      }
    }
    text = text.slice(0, cut) + (text.lastIndexOf(MATCH_START, cut) > text.lastIndexOf(MATCH_END, cut) ? MATCH_END : "") + "…";
  }
  const out: ReactNode[] = [];
  text.split(MATCH_START).forEach((chunk, i) => {
    if (i === 0) {
      out.push(chunk);
      return;
    }
    const [hit, rest = ""] = chunk.split(MATCH_END);
    out.push(<mark key={i}>{hit}</mark>, rest);
  });
  return out;
}

/**
 * A regex matching the query's terms the way the search does: whole words, "quoted phrases",
 * and a trailing * for prefixes (necro*). Null for an empty query.
 */
export function queryPattern(query: string): RegExp | null {
  const terms: string[] = [];
  for (const token of query.match(/"[^"]*"|\S+/g) ?? []) {
    let phrase = token.startsWith('"') && token.endsWith('"') && token.length >= 2 ? token.slice(1, -1) : token.replace(/"/g, "");
    let prefix = false;
    if (phrase.endsWith("*")) {
      phrase = phrase.replace(/\*+$/, "");
      prefix = true;
    }
    const words = phrase.match(/[\p{L}\p{N}]+/gu);
    if (!words) continue;
    // Words of a term may be separated by any punctuation/space, like the search tokenizer.
    // (Words are letters and digits only, so they need no regex escaping.)
    const body = words.join("[^\\p{L}\\p{N}]+");
    terms.push(`(?<![\\p{L}\\p{N}])${body}${prefix ? "[\\p{L}\\p{N}]*" : "(?![\\p{L}\\p{N}])"}`);
  }
  return terms.length ? new RegExp(terms.join("|"), "giu") : null;
}

/** Plain text with every match of `pattern` wrapped in <mark>. */
export function highlightText(text: string, pattern: RegExp | null, keyPrefix = "hl"): ReactNode[] {
  if (!pattern || !text) return [text];
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(pattern)) {
    const at = m.index ?? 0;
    if (!m[0]) continue;
    if (at > last) out.push(text.slice(last, at));
    out.push(<mark key={`${keyPrefix}-${n++}`}>{m[0]}</mark>);
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
