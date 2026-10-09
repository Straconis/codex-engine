import { memo, useMemo, type ReactNode } from "react";

// "What did the AI change?" A word-level diff of the AI version against the cleaned text,
// shown as Markdown source so layout changes (headings, bold, tables, line breaks) are
// visible. Removed text is struck through, added text highlighted, invisible characters
// labelled. For a current AI version the words never differ (the AI check guarantees it);
// an outdated one (made from older cleaned text, or failing the current check) can differ.

const INVISIBLE: Record<string, string> = {
  "\u200b": "zero-width space",
  "\u200c": "zero-width non-joiner",
  "\u200d": "zero-width joiner",
  "\u2060": "word joiner",
  "\ufeff": "byte-order mark",
  "\u00ad": "soft hyphen",
};
// Invisible characters are listed as alternatives, not a [class]: a joiner inside a class reads
// like a joined emoji sequence.
const TOKEN_RE = /\n|[ \t]+|[\p{L}\p{N}]+|\u200b|\u200c|\u200d|\u2060|\ufeff|\u00ad|./gsu;

type Op = { kind: "same" | "del" | "ins"; text: string };

function tokenize(text: string): string[] {
  return text.match(TOKEN_RE) ?? [];
}

/** Longest-common-subsequence diff of two token lists (pages are small enough for O(n*m)). */
function diffTokens(a: string[], b: string[]): Op[] {
  // Trim the common prefix/suffix first: most of a page is usually unchanged.
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);
  const n = midA.length;
  const m = midB.length;
  const lcs: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = midA[i] === midB[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const ops: Op[] = a.slice(0, start).map((text) => ({ kind: "same", text }));
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && midA[i] === midB[j]) {
      ops.push({ kind: "same", text: midA[i++] });
      j++;
    } else if (j < m && (i >= n || lcs[i][j + 1] >= lcs[i + 1][j])) {
      ops.push({ kind: "ins", text: midB[j++] });
    } else {
      ops.push({ kind: "del", text: midA[i++] });
    }
  }
  for (const text of a.slice(endA)) ops.push({ kind: "same", text });
  // Merge neighbours of the same kind so the output reads as runs, not single tokens.
  const merged: Op[] = [];
  for (const op of ops) {
    const last = merged[merged.length - 1];
    if (last && last.kind === op.kind) last.text += op.text;
    else merged.push({ ...op });
  }
  return merged;
}

function describe(ops: Op[]) {
  let invisible = 0;
  let marks = 0;
  let breaks = 0;
  let other = 0;
  for (const op of ops) {
    if (op.kind === "same") continue;
    for (const ch of op.text) {
      if (ch in INVISIBLE) invisible++;
      else if ("#*|>`_-".includes(ch)) marks++;
      else if (ch === "\n") breaks++;
      else if (ch.trim()) other++;
    }
  }
  return { invisible, marks, breaks, other };
}

function renderText(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let buffer = "";
  let n = 0;
  for (const ch of text) {
    if (ch in INVISIBLE) {
      if (buffer) out.push(buffer);
      buffer = "";
      out.push(
        <span className="invisibleChar" key={`${keyPrefix}-${n++}`} title={INVISIBLE[ch]}>
          {INVISIBLE[ch]}
        </span>
      );
    } else {
      buffer += ch;
    }
  }
  if (buffer) out.push(buffer);
  return out;
}

type Props = {
  before: string;
  after: string;
  aiStale?: boolean; // the AI version was made from older cleaned text (or fails the current check)
  aiCheckFailed?: boolean; // the AI version fails the current, stricter text check
};

function ChangesView({ before, after, aiStale = false, aiCheckFailed = false }: Props) {
  const ops = useMemo(() => diffTokens(tokenize(before), tokenize(after)), [before, after]);
  const { invisible, marks, breaks, other } = useMemo(() => describe(ops), [ops]);
  // Only a current AI version that passes the check is guaranteed to keep every word.
  const wordsSame = !aiStale && !aiCheckFailed;
  const nothing = !marks && !breaks && !other;
  const parts = [
    marks ? `${marks} formatting mark${marks === 1 ? "" : "s"} (# headings, ** bold, | tables, - lists)` : "",
    breaks ? `${breaks} line break${breaks === 1 ? "" : "s"}` : "",
    invisible ? `${invisible} invisible character${invisible === 1 ? "" : "s"} removed` : "",
    other
      ? `${other} other character${other === 1 ? "" : "s"} (${wordsSame ? "spacing or re-joined words" : "spacing, re-joined or changed words"})`
      : "",
  ].filter(Boolean);

  return (
    <div className="changesView">
      <div className="changesSummary">
        {nothing ? (
          <b>No layout changes: this page was already well formatted.</b>
        ) : aiCheckFailed ? (
          <b>This AI version no longer passes the current text check, so changed words are shown too.</b>
        ) : aiStale ? (
          <b>This AI version was made from older cleaned text, so word differences are shown too.</b>
        ) : (
          <b>The AI changed the layout only; every word is the same.</b>
        )}
        {parts.length > 0 && <div className="hint">{parts.join(" \u00b7 ")}</div>}
        <div className="hint">
          Shown as the page's Markdown source. <del>Struck through</del> = removed, <ins>highlighted</ins> = added,
          compared with the Cleaned version.
        </div>
      </div>
      <pre className="changesText">
        {ops.map((op, i) =>
          op.kind === "same" ? (
            <span key={i}>{renderText(op.text, `s${i}`)}</span>
          ) : op.kind === "del" ? (
            <del key={i}>{renderText(op.text.replace(/\n/g, "\u21b5\n"), `d${i}`)}</del>
          ) : (
            <ins key={i}>{renderText(op.text.replace(/\n/g, "\u21b5\n"), `i${i}`)}</ins>
          )
        )}
      </pre>
    </div>
  );
}

// Memoized: the diff is O(n*m) and the reader re-renders on every progress tick.
export default memo(ChangesView);
