"""Rule-based cleanup of extracted PDF text into light Markdown.

Third-party books don't follow any one layout, so instead of trusting the raw text
stream we use PyMuPDF's font and position data to:

- drop running headers/footers and page numbers that repeat across pages
- detect headings from font size (and short all-bold lines)
- keep bullet lists as list items
- reflow wrapped lines into paragraphs, re-joining words hyphenated across lines
- keep line breaks for "short line" blocks (tables, stat lines) instead of mashing them

The output is the baseline the reader shows; the optional AI pass (ai_format.py)
starts from it.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# Bump whenever the cleanup output changes: stored pages made by an older version are
# rebuilt (pages + search chunks) the next time their book is opened in the reader.
FORMATTER_VERSION = 5

MARGIN_FRACTION = 0.08  # top/bottom band of the page where headers/footers live
REPEAT_FRACTION = 0.3  # a margin line on >= this share of pages is boilerplate
HEADING_RATIO = 1.1  # font size vs body size that counts as a heading
COMMON_FONT_SHARE = 0.02  # a font with >= 2% of a book's text is a body style, never a heading by size
COMMON_FONT_MIN_CHARS = 20_000  # ...but only judged on documents with enough text
EDGE_CLIP = 25.0  # points: lines starting this close to the right page edge are clipped overflow

LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
BULLET_RE = re.compile(r"^\s*([\u2022\u25e6\u25aa\u25cf\u2023\u2043\u2219\u00b7*\-\u2013])\s+")
NUMBERED_RE = re.compile(r"^\s*(\d{1,3}[.)])\s+")
# A margin page number: "12", "Page 12", or a lowercase roman numeral ("iv", front matter).
# Only well-formed numerals, and never uppercase: "MIMIC", "DM" or "I" in a margin is text.
PAGE_NUMBER_RE = re.compile(r"^(?:[Pp]age\s*)?\d{1,4}$|^(?=[ivxlcdm])m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$")
# Markers that also start ordinary sentences ("- and then", "15. While..."): a list item only
# where a new item can begin (see _starts_item). Real bullet glyphs (•, ◦, ...) always are.
AMBIGUOUS_MARKERS = {"*", "-", "\u2013"}
ABILITIES = ("STR", "DEX", "CON", "INT", "WIS", "CHA")
ABILITY_TOKEN_RE = re.compile(r"\b(STR|DEX|CON|INT|WIS|CHA)\b|(\d{1,2}\s*\([+\-\u2212\u2013]?\d{1,2}\))")


@dataclass
class Span:
    text: str
    size: float
    bold: bool
    font: str = ""


@dataclass
class Line:
    spans: list[Span]
    y0: float
    y1: float
    x0: float = 0.0

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans).strip()

    @property
    def size(self) -> float:
        """Char-weighted dominant font size of the line."""
        weights: Counter[float] = Counter()
        for s in self.spans:
            weights[round(s.size, 1)] += len(s.text.strip())
        return weights.most_common(1)[0][0] if weights else 0.0

    @property
    def all_bold(self) -> bool:
        chars = [s for s in self.spans if s.text.strip()]
        return bool(chars) and all(s.bold for s in chars)


@dataclass
class PageLayout:
    height: float
    blocks: list[list[Line]] = field(default_factory=list)
    raw_text: str = ""  # untouched PyMuPDF text, kept as the last-resort fallback


# ---- extraction ------------------------------------------------------------

def _clean_text(text: str) -> str:
    for lig, rep in LIGATURES.items():
        text = text.replace(lig, rep)
    # U+FFFD between letters is almost always a curly apostrophe the font couldn't map.
    text = re.sub(r"(?<=[A-Za-z])\ufffd(?=[A-Za-z]|\s|$)", "'", text)  # "creatures\ufffd spaces" -> "creatures' spaces"
    text = re.sub(r"(?<=\d)\ufffd(?=\d)", "\u2013", text)  # "Recharge 5\ufffd6" -> "5-6" (en dash)
    return text.replace("\u00ad", "").replace("\u00a0", " ").replace("\t", " ")


def layout_from_pymupdf(page) -> PageLayout:
    """Convert a pymupdf.Page into our layout model (kept separate so tests need no PDF)."""
    import pymupdf

    # TEXTFLAGS_TEXT skips image data: ~17x faster on art-heavy books.
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
    layout = PageLayout(
        height=float(data.get("height") or page.rect.height),
        raw_text=page.get_text("text", flags=pymupdf.TEXTFLAGS_TEXT),
    )
    width = float(data.get("width") or page.rect.width)
    seen: list[tuple[str, float, float]] = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:  # images
            continue
        lines: list[Line] = []
        for raw in block.get("lines", []):
            spans = [
                Span(
                    text=_clean_text(s.get("text", "")),
                    size=float(s.get("size", 0)),
                    bold=bool(s.get("flags", 0) & 16) or "bold" in str(s.get("font", "")).lower(),
                    font=str(s.get("font", "")),
                )
                for s in raw.get("spans", [])
            ]
            x0, y0, x1, y1 = raw["bbox"]
            line = Line(spans=spans, y0=float(y0), y1=float(y1), x0=float(x0))
            if not line.text:
                continue
            # Text hanging off the page edge: some exporters (GM Binder) leave clipped
            # fragments of the next column there ("When", "an up", "of rio").
            if x0 >= width - EDGE_CLIP or x1 <= EDGE_CLIP:
                continue
            # Some exporters (e.g. GM Binder) draw headings twice, stacked, for an outline effect.
            if any(t == line.text and abs(x - line.x0) < 2 and abs(y - line.y0) < 2 for t, x, y in seen):
                continue
            seen.append((line.text, line.x0, line.y0))
            lines.append(line)
        if lines:
            layout.blocks.append(lines)
    return layout


# ---- document-level analysis ------------------------------------------------

def _margin_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.lower())).strip()


def _in_margin(line: Line, height: float) -> bool:
    band = height * MARGIN_FRACTION
    return line.y1 <= band or line.y0 >= height - band


def find_boilerplate(pages: list[PageLayout]) -> set[str]:
    """Margin lines (normalised, digits wildcarded) that repeat across many pages."""
    if len(pages) < 4:
        return set()
    seen: Counter[str] = Counter()
    for page in pages:
        keys = {_margin_key(line.text) for block in page.blocks for line in block if _in_margin(line, page.height)}
        seen.update(keys)
    threshold = max(3, int(len(pages) * REPEAT_FRACTION))
    return {key for key, count in seen.items() if count >= threshold}


def mark_runin_labels(pages: list[PageLayout]) -> None:
    """Infer bold for run-in labels ("Armor Class 15", "Darkvision. You can see...").

    Type3/embedded fonts often carry no bold flag or font name, but a label is still
    set in a different font from the text that follows it on the same line.
    """
    for page in pages:
        for block in page.blocks:
            for line in block:
                spans = [s for s in line.spans if s.text.strip()]
                if len(spans) < 2 or spans[0].bold or not spans[0].font:
                    continue
                first, nxt = spans[0], spans[1]
                label = first.text.strip()
                if (
                    len(label) <= 60
                    and (label[:1].isupper() or label[:1].isdigit())  # "Armor Class", "3/day each:"
                    # A label is short ("Armor Class"), ends or splits like one ("Radiant Soul.",
                    # "Monk: Way of the Primal Forces") or is all caps; a font switch at the start
                    # of a wrapped prose line ("transformation ends, you have a") is none of these.
                    and (len(label.split()) <= 4 or label.endswith((".", ":")) or ":" in label or label.isupper())
                    and nxt.font != first.font
                    # Same size, or a smaller small-caps label ("Armor Class" 8.5pt before a 10pt value).
                    and nxt.size * 0.75 <= first.size <= nxt.size + 0.6
                ):
                    first.bold = True


def body_font_size(pages: list[PageLayout]) -> float:
    weights: Counter[float] = Counter()
    for page in pages:
        for block in page.blocks:
            for line in block:
                for s in line.spans:
                    weights[round(s.size * 2) / 2] += len(s.text.strip())
    return weights.most_common(1)[0][0] if weights else 10.0


# ---- page rendering ---------------------------------------------------------

def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("*", "\\*")


def _line_markdown(line: Line) -> str:
    """Line text with bold spans wrapped in ** (adjacent bold spans merged)."""
    out: list[str] = []
    bold_run: list[str] = []

    def flush() -> None:
        if bold_run:
            raw = "".join(bold_run)
            stripped = raw.strip()
            if stripped:
                lead = raw[: len(raw) - len(raw.lstrip())]
                trail = raw[len(raw.rstrip()):]
                out.append(f"{lead}**{_escape(stripped)}**{trail}")
            else:
                out.append(raw)
            bold_run.clear()

    for s in line.spans:
        if s.bold and s.text.strip():
            bold_run.append(s.text)
        elif s.bold:
            bold_run.append(s.text)  # whitespace inside a bold run
        else:
            flush()
            out.append(_escape(s.text))
    flush()
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _drop_chars(spans: list[Span], start: int, count: int) -> list[Span]:
    """Remove chars [start, start+count) of the joined span text (e.g. a bullet glyph)."""
    out: list[Span] = []
    pos = 0
    for s in spans:
        a, b = pos, pos + len(s.text)
        pos = b
        lo, hi = max(a, start), min(b, start + count)
        text = s.text[: lo - a] + s.text[hi - a:] if lo < hi else s.text
        out.append(Span(text, s.size, s.bold))
    return out


def font_key(span: Span) -> tuple[str, float]:
    # "Type3 (1113 0 R)" -> "Type3": embedded fonts get a new object id per subset.
    return span.font.split(" (")[0], round(span.size * 2) / 2


def common_fonts(pages: list[PageLayout]) -> set[tuple[str, float]]:
    """Fonts (family, size) carrying a sizeable share of the book's text.

    Those are body styles, even when larger than the main body text: GM Binder sets
    stat-block values in 10pt ScalySans next to 9pt body text. Real heading fonts carry
    well under 1% of a book's text.
    """
    chars: Counter[tuple[str, float]] = Counter()
    for page in pages:
        for block in page.blocks:
            for line in block:
                for s in line.spans:
                    chars[font_key(s)] += len(s.text.strip())
    total = sum(chars.values())
    if total < COMMON_FONT_MIN_CHARS:
        return set()  # too little text for shares to mean anything (a one-page handout's heading is >2%)
    return {key for key, n in chars.items() if n / total >= COMMON_FONT_SHARE}


def _dominant_font(line: Line) -> tuple[str, float]:
    weights: Counter[tuple[str, float]] = Counter()
    for s in line.spans:
        weights[font_key(s)] += len(s.text.strip())
    return weights.most_common(1)[0][0] if weights else ("", 0.0)


def _heading_level(line: Line, body_size: float, common: set[tuple[str, float]] = frozenset()) -> int | None:
    text = line.text
    if len(text) > 120 or not any(ch.isalpha() for ch in text):
        return None
    if not ABILITY_TOKEN_RE.sub("", text).strip():
        return None  # "STR DEX CON..." is a stat table row, not a heading
    ratio = line.size / body_size if body_size else 1.0
    if ratio >= HEADING_RATIO and _dominant_font(line) in common:
        ratio = 1.0  # larger, but a body style used throughout the book: not a heading
    if ratio >= 1.8:
        return 1
    if ratio >= 1.35:
        return 2
    if ratio >= HEADING_RATIO:
        return 3
    if line.all_bold and len(text) <= 60 and not text.endswith((".", ",", ":", ";")) and ratio >= 0.95:
        return 4
    return None


HYPHENATED_WORD_RE = re.compile(r"\b([A-Za-z]+)-([A-Za-z]+)\b")


def hyphenated_vocabulary(pages: list[PageLayout]) -> set[str]:
    """Hyphenated compounds that appear mid-line somewhere in the document ("well-known").

    A compound broken at a line end looks exactly like a word split for wrapping; if the
    book also spells it with a hyphen mid-line, the hyphen is real and is kept.
    """
    vocab: set[str] = set()
    for page in pages:
        for block in page.blocks:
            for line in block:
                for a, b in HYPHENATED_WORD_RE.findall(line.text):
                    vocab.add(f"{a}-{b}".lower())
    return vocab


def join_lines(lines: list[str], keep_hyphen: set[str] | frozenset[str] = frozenset()) -> str:
    """Reflow wrapped lines, re-joining words hyphenated across a line break."""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
        elif re.search(r"[^\W\d_]-$", out) and (line[:1].isupper() or line[:1].isdigit()):
            out = out + line  # "Half-" / "Orc", "pre-" / "1990": a compound, never "Half- Orc"
        elif re.search(r"[A-Za-z]-$", out) and line[:1].islower():
            head = re.search(r"([A-Za-z]+)-$", out).group(1)
            tail = re.match(r"[A-Za-z]+", line)
            compound = f"{head}-{tail.group(0)}".lower() if tail else ""
            out = out + line if compound in keep_hyphen else out[:-1] + line
        else:
            out = f"{out} {line}"
    return out


def _paragraph(lines: list[Line], keep_hyphen: set[str]) -> str:
    texts = [_line_markdown(line) for line in lines]
    labelled = [t.startswith("**") for t in texts]
    if sum(labelled) >= 2 and sum(labelled) * 2 >= len(texts):
        # Stat-block style ("**Armor Class** 16" / "**Hit Points** 72"): one line per label;
        # an unlabelled line continues the one before it.
        rows: list[list[str]] = []
        for text, is_label in zip(texts, labelled):
            if is_label or not rows:
                rows.append([text])
            else:
                rows[-1].append(text)
        return "\n".join(join_lines(row, keep_hyphen) for row in rows)
    avg = sum(len(t) for t in texts) / len(texts)
    # Wrapped prose continues on a lowercase letter (or after a line-end hyphen);
    # table cells and stat lines mostly don't.
    wrapped = sum(1 for a, b in zip(texts, texts[1:]) if b[:1].islower() or a.endswith("-"))
    if len(texts) >= 3 and avg < 30 and wrapped < (len(texts) - 1) / 2:
        # Short-line block: table cells, stat lines, spell components. Keep the breaks.
        return "\n".join(texts)
    return join_lines(texts, keep_hyphen)


def render_page(
    page: PageLayout,
    body_size: float,
    boilerplate: set[str],
    keep_hyphen: set[str] | None = None,
    common: set[tuple[str, float]] | None = None,
) -> str:
    keep_hyphen = keep_hyphen or set()
    common = common or set()
    parts: list[str] = []
    for block in page.blocks:
        lines = [
            line
            for line in block
            if not (
                _in_margin(line, page.height)
                and (_margin_key(line.text) in boilerplate or PAGE_NUMBER_RE.match(line.text))
            )
        ]
        pending: list[Line] = []  # body lines of the current paragraph
        item: list[Line] | None = None  # lines of the current list item
        item_marker = "-"
        previous_heading: int | None = None  # level of the heading on the line just before, in this block

        def flush() -> None:
            nonlocal item
            if pending:
                parts.append(_paragraph(pending, keep_hyphen))
                pending.clear()
            if item:
                parts.append(f"{item_marker} {join_lines([_line_markdown(x) for x in item], keep_hyphen)}")
                item = None

        for line in lines:
            level = _heading_level(line, body_size, common)
            bullet = BULLET_RE.match(line.text)
            numbered = NUMBERED_RE.match(line.text)
            if (numbered or (bullet and bullet.group(1) in AMBIGUOUS_MARKERS)) and not _starts_item(pending, item):
                bullet = numbered = None  # "15. While wearing it..." continues the sentence before it
            if level:
                flush()
                heading = _escape(re.sub(r"\s+", " ", line.text))
                if previous_heading == level and level <= 3:
                    parts[-1] += " " + heading  # a large heading wrapped onto a second line
                else:
                    parts.append(f"{'#' * level} {heading}")
                previous_heading = level
                continue
            previous_heading = None
            if bullet or numbered:
                flush()
                marker = bullet or numbered
                item_marker = "-" if bullet else re.sub(r"\)$", ".", marker.group(1))
                raw = "".join(s.text for s in line.spans)
                lead = len(raw) - len(raw.lstrip())
                item = [Line(spans=_drop_chars(line.spans, lead, marker.end()), y0=line.y0, y1=line.y1)]
            elif item is not None:
                item.append(line)
            else:
                pending.append(line)
        flush()
    return "\n\n".join(p for p in _ability_tables(parts) if p.strip())


def _starts_item(pending: list[Line], item: list[Line] | None) -> bool:
    """Whether a line here can begin a list item: at the start of a block, right after another
    item, or after a line that ends a sentence. Otherwise it's a wrapped line of prose."""
    if item is not None or not pending:
        return True
    return pending[-1].text.rstrip().endswith((".", ":", ";", "!", "?"))


def _ability_pairs(part: str) -> list[tuple[str, str]] | None:
    """(ability, score) pairs if this paragraph is nothing but ability scores.

    Handles both "STR 18 (+4) DEX 11 (+0)" and "STR DEX CON / 18 (+4) 11 (+0) 14 (+2)".
    """
    plain = part.replace("**", "")
    if ABILITY_TOKEN_RE.sub("", plain).strip():
        return None
    matches = list(ABILITY_TOKEN_RE.finditer(plain))
    names = [m.group(1) for m in matches if m.group(1)]
    scores = [re.sub(r"\s+", " ", m.group(2)) for m in matches if m.group(2)]
    if not names or len(names) != len(scores):
        return None
    return list(zip(names, scores))


def _ability_tables(parts: list[str]) -> list[str]:
    """Merge consecutive ability-score paragraphs into one Markdown table."""
    out: list[str] = []
    run: list[tuple[str, str]] = []

    def flush() -> None:
        if len(run) >= 3:
            out.append(
                "| " + " | ".join(n for n, _ in run) + " |\n"
                + "|" + "---|" * len(run) + "\n"
                + "| " + " | ".join(v for _, v in run) + " |"
            )
        else:
            out.extend(f"{n} {v}" for n, v in run)
        run.clear()

    for part in parts:
        pairs = _ability_pairs(part)
        if pairs and len(run) + len(pairs) <= len(ABILITIES):
            run.extend(pairs)
        else:
            flush()
            if pairs:
                run.extend(pairs)  # the next stat block's scores start a new table
            else:
                out.append(part)
    flush()
    return out


def format_document(pages: list[PageLayout]) -> list[str]:
    """Clean every page of a document; returns one Markdown string per page."""
    boilerplate = find_boilerplate(pages)
    body_size = body_font_size(pages)
    mark_runin_labels(pages)
    keep_hyphen = hyphenated_vocabulary(pages)
    common = common_fonts(pages)
    return [render_page(page, body_size, boilerplate, keep_hyphen, common) for page in pages]


# ---- helpers for search -----------------------------------------------------

def markdown_to_plain(md: str) -> str:
    """Strip our Markdown markers so chunks/snippets read as plain text."""
    text = re.sub(r"^#{1,6}\s+", "", md, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:[-*+]|\d{1,3}\.)\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", "", text, flags=re.MULTILINE)  # table rules
    text = re.sub(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", "", text, flags=re.MULTILINE)  # section breaks
    text = re.sub(r"^[ \t]*(?:>[ \t]?)+", "", text, flags=re.MULTILINE)  # quotes
    text = text.replace("**", "").replace("|", " ")
    text = re.sub(r"(?<![\\*])\*(?=\S)(.+?)(?<=[^\s\\])\*", r"\1", text)  # *emphasis*
    text = re.sub(r"(?<![\w\\])_(?=\S)(.+?)(?<=\S)_(?!\w)", r"\1", text)  # _emphasis_
    return re.sub(r"\\([\\*_`#|>])", r"\1", text)


def first_heading(md: str) -> str | None:
    for match in re.finditer(r"^#{1,6}\s+(.+)$", md, flags=re.MULTILINE):
        heading = match.group(1).strip()
        if heading and len(heading) <= 90:
            return heading
    return None
