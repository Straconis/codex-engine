import type { ReactNode } from "react";
import { highlightText } from "./highlight";

// Small Markdown renderer for the page reader. It covers what the backend formatter
// and the AI pass emit: headings, paragraphs (single newlines = line breaks), lists,
// block quotes, pipe tables, rules, **bold**, *italic*, `code` and backslash escapes. It builds
// React elements directly (no innerHTML), so text from a PDF can never inject HTML.

type Block =
  | { kind: "h"; level: number; text: string }
  | { kind: "p"; lines: string[] }
  | { kind: "ul" | "ol"; items: string[]; start: number }
  | { kind: "table"; header: string[]; rows: string[][] }
  | { kind: "quote"; text: string }
  | { kind: "hr" };

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*(\d{1,3})[.)]\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const RULE = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE = /^\s*> ?/;

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "");
  return trimmed.split(/(?<!\\)\|/).map((cell) => cell.trim());
}

function parseBlocks(md: string): Block[] {
  const lines = md.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: Extract<Block, { kind: "ul" | "ol" }> | null = null;

  const flush = () => {
    if (para.length) blocks.push({ kind: "p", lines: para });
    para = [];
    list = null;
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) {
      flush();
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      blocks.push({ kind: "h", level: heading[1].length, text: heading[2].trim() });
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      flush();
      const header = splitRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
      i--;
      blocks.push({ kind: "table", header, rows });
      continue;
    }
    if (QUOTE.test(line)) {
      flush();
      const quoted: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i])) quoted.push(lines[i++].replace(QUOTE, ""));
      i--;
      blocks.push({ kind: "quote", text: quoted.join("\n") });
      continue;
    }
    if (RULE.test(line)) {
      flush();
      blocks.push({ kind: "hr" });
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = NUMBERED.exec(line);
    if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      const text = bullet ? bullet[1] : numbered![2];
      if (para.length) flush();
      if (!list || list.kind !== kind) {
        if (list) flush();
        list = { kind, items: [], start: numbered ? Number(numbered[1]) : 1 };
        blocks.push(list);
      }
      list.items.push(text);
      continue;
    }
    if (list) {
      // Continuation of the previous list item.
      list.items[list.items.length - 1] += " " + line.trim();
      continue;
    }
    para.push(line);
  }
  flush();
  return blocks;
}

const INLINE =
  /\\([\\`*_#|])|`([^`]+)`|\*\*(.+?)\*\*(?!\*)|(?<![\w*])\*(?![\s*])(.+?)(?<!\s)\*(?![\w*])|(?<![A-Za-z0-9])_(?!\s)(.+?)(?<!\s)_(?![A-Za-z0-9])/g;

function inline(text: string, keyPrefix = "i", hl: RegExp | null = null): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(...highlightText(text.slice(last, at), hl, `${keyPrefix}-t${n}`));
    const key = `${keyPrefix}-${n++}`;
    if (m[1] !== undefined) out.push(...highlightText(m[1], hl, key));
    else if (m[2] !== undefined) out.push(<code key={key}>{highlightText(m[2], hl, key)}</code>);
    else if (m[3] !== undefined) out.push(<strong key={key}>{inline(m[3], key, hl)}</strong>);
    else out.push(<em key={key}>{inline(m[4] ?? m[5], key, hl)}</em>);
    last = at + m[0].length;
  }
  if (last < text.length) out.push(...highlightText(text.slice(last), hl, `${keyPrefix}-end`));
  return out;
}

// `highlight` marks search terms (see highlight.tsx) in the rendered text.
export default function Markdown({ text, highlight = null }: { text: string; highlight?: RegExp | null }) {
  const blocks = parseBlocks(text);
  if (!blocks.length) return <p className="mdEmpty">This page has no text. It may be an image-only page.</p>;
  return <div className="md">{renderBlocks(blocks, "b", highlight)}</div>;
}

function renderBlocks(blocks: Block[], prefix: string, hl: RegExp | null): ReactNode[] {
  return blocks.map((b, index) => {
    const bi = `${prefix}${index}`;
    switch (b.kind) {
      case "quote":
        return <blockquote key={bi}>{renderBlocks(parseBlocks(b.text), `${bi}q`, hl)}</blockquote>;
      case "h": {
        const Tag = `h${Math.min(6, b.level + 1)}` as "h2"; // h1 is the app title
        return <Tag key={bi}>{inline(b.text, `h${bi}`, hl)}</Tag>;
      }
      case "p":
        return (
          <p key={bi}>
            {b.lines.map((line, li) => (
              <span key={li}>
                {li > 0 && <br />}
                {inline(line, `p${bi}-${li}`, hl)}
              </span>
            ))}
          </p>
        );
      case "ul":
        return (
          <ul key={bi}>
            {b.items.map((item, ii) => (
              <li key={ii}>{inline(item, `u${bi}-${ii}`, hl)}</li>
            ))}
          </ul>
        );
      case "ol":
        return (
          <ol key={bi} start={b.start}>
            {b.items.map((item, ii) => (
              <li key={ii}>{inline(item, `o${bi}-${ii}`, hl)}</li>
            ))}
          </ol>
        );
      case "table":
        return (
          <div className="mdTableWrap" key={bi}>
            <table>
              <thead>
                <tr>
                  {b.header.map((cell, ci) => (
                    <th key={ci}>{inline(cell, `t${bi}-h${ci}`, hl)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {b.rows.map((row, ri) => (
                  <tr key={ri}>
                    {row.map((cell, ci) => (
                      <td key={ci}>{inline(cell, `t${bi}-${ri}-${ci}`, hl)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      case "hr":
        return <hr key={bi} />;
    }
  });
}
