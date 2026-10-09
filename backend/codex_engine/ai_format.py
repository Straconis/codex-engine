"""Optional AI formatting pass, run locally through Ollama.

This is a formatting/restoration agent, not an editor. It takes the deterministic
cleanup (formatting.py) of one page, splits it into manageable sections, and asks a
local model to restore presentation structure (paragraphs, headings, lists, quotes,
section breaks, tables) as Markdown. It must keep the author's words exactly.

Every section's output is validated (`check_faithful`); anything that drops, adds,
reorders or rewrites text is rejected and that section keeps its cleaned version.
Accepted output is cached by hash(prompt version, model, input), so the same text
is never sent to the model twice.

Book text never leaves the machine. The model and Ollama settings come from
`AIConfig` (environment variables), and the HTTP client is swappable, so switching
models or backends later doesn't touch the pipeline.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol

# Bump when SYSTEM_PROMPT changes meaningfully, so cached output from the old prompt isn't reused.
PROMPT_VERSION = 2

SYSTEM_PROMPT = """You restore the formatting of text extracted from a book or document (one section at a time) so it displays properly in a reader.

You are a formatter, not an editor. Keep every word of the input exactly as written and in the same order.
Keep capitalisation, spelling and punctuation exactly as written: do not change ALL-CAPS headings to Title Case.
Never summarise, paraphrase, shorten, translate, censor, modernise, correct, explain or add content. Do not "improve" the writing.
Never remove any line or word, even if it looks repeated or unimportant. The only thing you may remove is a bare page number.

Fix presentation only:
- Rebuild paragraphs: join lines that were broken mid-sentence; separate paragraphs with a blank line.
- Re-join words that were split across a line break with a hyphen (e.g. "imag- ination" -> "imagination").
- Mark headings with #, ## or ### when they are clearly headings.
- Use - for bulleted lists and 1. for numbered lists.
- Use > for block quotations and epigraphs.
- Use --- for clear section breaks (e.g. a line of *** or a centred ornament).
- Lay out clearly tabular data as Markdown pipe tables; use **bold** only where the source uses run-in labels.
If the text is already well formatted, return it unchanged.
Output only the formatted Markdown: no preamble, no notes, no code fences."""

# Validation thresholds. The model may not add a single word; markup is not words.
MIN_KEPT = 0.95  # of the input's words that must appear, in order, in the output
# The deterministic cleanup already strips running headers/footers, so the model may drop
# nothing but a stray page number: a line of its own holding at most this many tokens, all digits.
MAX_DROPPED_NUMBER_RUN = 3
MIN_LENGTH_RATIO = 0.8  # output chars / input chars
MAX_LENGTH_RATIO = 1.6


class AIUnavailable(RuntimeError):
    """Ollama isn't installed/running or has no usable model. Not an app failure."""


class AIFormatError(RuntimeError):
    """The model ran but nothing usable came back for this page."""


class AICancelled(RuntimeError):
    """The user cancelled formatting. Sections already accepted stay cached."""


@dataclass(frozen=True)
class AIConfig:
    url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:1.5b"  # small and light; any installed Ollama model works
    temperature: float = 0.0
    num_ctx: int = 8192
    timeout: float = 300.0
    section_chars: int = 3000  # max input per model call
    max_attempts: int = 4  # per section: 1 try + 3 automatic retries before asking the user
    retry_temperature: float = 0.3  # a little variation on retries, so a retry isn't the same answer

    @classmethod
    def from_env(cls) -> "AIConfig":
        env = os.environ.get
        default = cls()
        return cls(
            url=env("CODEX_ENGINE_OLLAMA_URL", default.url).rstrip("/"),
            model=env("CODEX_ENGINE_OLLAMA_MODEL", default.model),
            temperature=float(env("CODEX_ENGINE_AI_TEMPERATURE", default.temperature)),
            num_ctx=int(env("CODEX_ENGINE_AI_NUM_CTX", default.num_ctx)),
            timeout=float(env("CODEX_ENGINE_AI_TIMEOUT", default.timeout)),
            section_chars=int(env("CODEX_ENGINE_AI_SECTION_CHARS", default.section_chars)),
            max_attempts=max(1, int(env("CODEX_ENGINE_AI_ATTEMPTS", default.max_attempts))),
            retry_temperature=float(env("CODEX_ENGINE_AI_RETRY_TEMPERATURE", default.retry_temperature)),
        )


class ChatClient(Protocol):
    """What the pipeline needs from a local model backend."""

    def list_models(self) -> list[str]: ...

    def chat(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_text: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        """The model's reply. on_text(chars_so_far) reports progress as it streams in;
        should_stop() is polled while it does, and raises AICancelled when true."""
        ...


class OllamaClient:
    def __init__(self, config: AIConfig):
        self.config = config

    def _request(self, path: str, payload: dict | None, timeout: float) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.config.url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AIUnavailable(f"Ollama not reachable at {self.config.url} ({exc})") from exc

    def list_models(self) -> list[str]:
        return sorted(m["name"] for m in self._request("/api/tags", None, 2.0).get("models", []))

    def chat(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_text: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        # Streamed, so the reader can show real progress and Cancel can stop the model:
        # closing the connection makes Ollama stop generating.
        payload = {
            "model": model,
            "stream": True,
            "messages": messages,
            "options": {
                "temperature": self.config.temperature if temperature is None else temperature,
                "num_ctx": self.config.num_ctx,
                **({"num_predict": max_tokens} if max_tokens else {}),
            },
        }
        req = urllib.request.Request(
            self.config.url + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        parts: list[str] = []
        written = 0
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                for raw in resp:
                    if should_stop and should_stop():
                        raise AICancelled("Cancelled")
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError as exc:
                        raise AIUnavailable(f"Ollama sent a reply Codex Engine couldn't read ({exc})") from exc
                    if event.get("error"):
                        raise AIUnavailable(f"Ollama error: {event['error']}")
                    piece = event.get("message", {}).get("content", "")
                    if piece:
                        parts.append(piece)
                        written += len(piece)
                        if on_text:
                            on_text(written)
                    if event.get("done"):
                        break
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AIUnavailable(f"Ollama not reachable at {self.config.url} ({exc})") from exc
        return "".join(parts)


def default_client() -> ChatClient:
    return OllamaClient(AIConfig.from_env())


def pick_model(models: list[str], preferred: str | None) -> str | None:
    """Exact name, then name without tag ("llama3.2" -> "llama3.2:3b"), else first installed."""
    if preferred:
        for name in models:
            if name == preferred or name.split(":")[0] == preferred:
                return name
    return models[0] if models else None


def status(client: ChatClient | None = None, config: AIConfig | None = None) -> dict:
    """Whether AI formatting is usable right now. Never raises."""
    config = config or AIConfig.from_env()
    client = client or OllamaClient(config)
    try:
        models = client.list_models()
    except AIUnavailable as exc:
        return {"available": False, "models": [], "model": None, "error": str(exc)}
    if not models:
        return {
            "available": False,
            "models": [],
            "model": None,
            "error": f"Ollama is running but has no models. Run: ollama pull {config.model}",
        }
    return {"available": True, "models": models, "model": pick_model(models, config.model), "error": None}


# ---- validation ---------------------------------------------------------------

def _words(text: str) -> list[str]:
    # Case-sensitive on purpose: "OGRE BOLT LAUNCHER" -> "Ogre Bolt Launcher" changes the author's text.
    return re.findall(r"[^\W_]+", text)


def _page_number_words(text: str) -> set[int]:
    """Indices (into _words(text)) of words on a line that is nothing but a short number."""
    found: set[int] = set()
    index = 0
    for line in text.splitlines():
        words = _words(line)
        if words and len(words) <= MAX_DROPPED_NUMBER_RUN and all(w.isdigit() for w in words):
            found.update(range(index, index + len(words)))
        index += len(words)
    return found


def check_faithful(source: str, output: str) -> None:
    """Raise AIFormatError unless `output` is `source` with only layout changes."""
    if not output.strip():
        raise AIFormatError("the model returned nothing")
    src, out = _words(source), _words(output)
    if not src:
        return
    ratio = len(output) / max(1, len(source))
    if not MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO:
        raise AIFormatError(f"output length changed too much ({ratio:.0%} of the input)")
    page_numbers = _page_number_words(source)
    matcher = difflib.SequenceMatcher(None, src, out, autojunk=False)
    kept_src = 0  # words matched in order (re-joined split words count as kept)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            kept_src += i2 - i1
        elif tag == "replace":
            # Only splitting/merging the same letters is allowed ("imag ination" -> "imagination").
            # Never digits: "1 0" -> "10" changes a number.
            before, after = src[i1:i2], out[j1:j2]
            if "".join(before) != "".join(after) or any(ch.isdigit() for ch in "".join(before)):
                raise AIFormatError(f"output changed words ({' '.join(before)!r} -> {' '.join(after)!r})")
            kept_src += i2 - i1
        elif tag == "delete":
            if not all(i in page_numbers for i in range(i1, i2)):
                dropped = src[i1:i2]
                raise AIFormatError(f"output dropped text ({' '.join(dropped[:8])!r}{'...' if len(dropped) > 8 else ''})")
        elif tag == "insert":
            added = out[j1:j2]
            context = f" after {src[i1 - 1]!r}" if i1 else ""
            raise AIFormatError(f"output added text that isn't in the source ({' '.join(added[:8])!r}{context})")
    kept = kept_src / len(src)
    if kept < MIN_KEPT:
        raise AIFormatError(f"output dropped, reordered or reworded text ({kept:.0%} of words kept in order)")
    _check_punctuation(source, output)


# Characters that carry no text: zero-width spaces/joiners, BOM, soft hyphen.
INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
_TABLE_RULE_RE = re.compile(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*")
_THEMATIC_BREAK_RE = re.compile(r"\s*([-*_])(\s*\1){2,}\s*")
_LINE_MARKER_RE = re.compile(r"^\s*(?:>\s*)*(?:[-+]\s+|\d{1,3}[.)]\s+)?")
# Typographic variants of the same mark count as equal.
_SAME_MARK = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "..."})
# A hyphen after a letter at a line end or before a space ("imag-" / "imag- ination") may be a
# word split across lines, which the formatter may re-join. Any other hyphen ("ten-foot", "1-2")
# belongs to the author.
SPLIT_HYPHEN = "-\n"
_TOKEN_RE = re.compile(r"(?P<split>(?<=[^\W\d_])-(?=\s|$))|[^\W_]+|[^\w\s]")


def _text_tokens(markdown: str) -> list[str]:
    """Words and punctuation marks of the *text*, with Markdown layout removed.

    Layout the formatter may add or remove: headings (#), emphasis (*, _), code (`),
    tables (| and rule lines), quotes (>), list markers, thematic breaks, escapes and
    invisible characters. Everything else, punctuation included, belongs to the author.
    """
    tokens: list[str] = []
    for line in INVISIBLE_RE.sub("", markdown).translate(_SAME_MARK).splitlines():
        if _TABLE_RULE_RE.fullmatch(line) or _THEMATIC_BREAK_RE.fullmatch(line):
            continue
        line = re.sub(r"[*#`|\\]", " ", line)
        line = re.sub(r"(?<![^\W_])_|_(?![^\W_])", " ", line)  # emphasis underscores, not snake_case
        line = _LINE_MARKER_RE.sub("", line)
        tokens.extend(SPLIT_HYPHEN if m.group("split") else m.group() for m in _TOKEN_RE.finditer(line))
    return tokens


def _rejoined(before: list[str], after: list[str]) -> bool:
    """`after` is `before` with split-hyphen words re-joined (hyphen dropped or kept)."""
    pattern = "".join("-?" if t == SPLIT_HYPHEN else re.escape(t) for t in before)
    return SPLIT_HYPHEN in before and re.fullmatch(pattern, "".join(after)) is not None


def _check_punctuation(source: str, output: str) -> None:
    """Reject added, removed or changed punctuation (the word check can't see it)."""
    src, out = _text_tokens(source), _text_tokens(output)
    matcher = difflib.SequenceMatcher(None, src, out, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before, after = src[i1:i2], out[j1:j2]
        if tag == "replace" and _rejoined(before, after):
            continue  # a word split across a line re-joined ("imag- ination" -> "imagination")
        if tag == "delete" and len(before) <= MAX_DROPPED_NUMBER_RUN and all(t.isdigit() for t in before):
            continue  # a stray page number
        marks_before = [t.strip() for t in before if not t[0].isalnum()]
        marks_after = [t.strip() for t in after if not t[0].isalnum()]
        if marks_before == marks_after:
            continue  # only words differ here; the word check has already judged those
        context = next((t for t in reversed(src[:i1]) if t[0].isalnum()), "")
        where = f" after {context!r}" if context else ""
        if marks_before and not marks_after:
            change = f"removed {' '.join(marks_before)!r}{where}"
        elif marks_after and not marks_before:
            change = f"added {' '.join(marks_after)!r}{where}"
        else:
            change = f"changed {' '.join(marks_before)!r} to {' '.join(marks_after)!r}{where}"
        raise AIFormatError(f"output changed punctuation ({change})")


def describe_changes(source: str, output: str) -> dict:
    """What an accepted AI version changed, for the progress window and reader.

    Returns counts plus `meaningful`: False when the output differs from the source only by
    invisible characters and spacing, i.e. the page was already well formatted.
    """
    invisible = len(INVISIBLE_RE.findall(source)) - len(INVISIBLE_RE.findall(output))
    squash = lambda t: re.sub(r"[ \t]+", " ", re.sub(r"[ \t]+\n", "\n", INVISIBLE_RE.sub("", t))).strip()
    meaningful = squash(source) != squash(output)
    markup = 0
    line_breaks = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, source, output, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        changed = source[i1:i2] + output[j1:j2]
        markup += sum(changed.count(ch) for ch in "#*|>`_")
        line_breaks += changed.count("\n")
    # A line diff, so one inserted line doesn't make every later line count as changed.
    lines_changed = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, source.splitlines(), output.splitlines(), autojunk=False).get_opcodes()
        if tag != "equal"
    )
    parts = []
    if markup:
        parts.append(f"{markup} formatting mark{'s' if markup != 1 else ''} (headings, bold, tables, lists)")
    if line_breaks:
        parts.append(f"{line_breaks} line break{'s' if line_breaks != 1 else ''}")
    if invisible > 0:
        parts.append(f"{invisible} invisible character{'s' if invisible != 1 else ''} removed")
    if not meaningful:
        summary = "This page was already well formatted; the AI found nothing to improve."
        if invisible > 0:
            summary += f" (It only removed {invisible} invisible character{'s' if invisible != 1 else ''}.)"
    else:
        summary = f"Layout changed on {lines_changed} line{'s' if lines_changed != 1 else ''}: " + ", ".join(parts) + ". The words are unchanged."
    return {
        "meaningful": meaningful,
        "lines_changed": lines_changed,
        "markup": markup,
        "line_breaks": line_breaks,
        "invisible_removed": max(0, invisible),
        "summary": summary,
    }


def strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, flags=re.DOTALL)
    return match.group(1).strip() if match else text


# ---- sections + caching -----------------------------------------------------------

def _split_long(paragraph: str, max_chars: int) -> list[tuple[str, str]]:
    """Pieces of an over-long paragraph, each with the separator that preceded it.

    Split at line breaks first, then (for a single huge line) after sentence ends, so a long
    stat block or table can't overflow the model's context. Pieces are re-joined with the
    same separator, so the page's layout is unchanged.
    """
    if len(paragraph) <= max_chars:
        return [(paragraph, "")]
    for separator, units in (("\n", paragraph.split("\n")), (" ", re.split(r"(?<=[.!?:;])\s+", paragraph))):
        if len(units) > 1:
            break
    else:
        return [(paragraph, "")]  # one unbreakable run of text: send it whole
    pieces: list[tuple[str, str]] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if current and size + len(unit) > max_chars:
            pieces.append((separator.join(current), separator))
            current, size = [], 0
        current.append(unit)
        size += len(unit) + 1
    pieces.append((separator.join(current), separator))
    # A single line can still be too long; split it by sentences. The first piece has no separator.
    out: list[tuple[str, str]] = []
    for piece, sep in pieces:
        sub = _split_long(piece, max_chars) if separator == "\n" and len(piece) > max_chars else [(piece, "")]
        out.append((sub[0][0], sep if out else ""))
        out.extend(sub[1:])
    return out


def section_parts(markdown: str, max_chars: int) -> list[tuple[str, str]]:
    """(section, separator before it) pairs; "".join(sep + section) rebuilds the text."""
    parts: list[tuple[str, str]] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        if current:
            parts.append(("\n\n".join(current), "\n\n" if parts else ""))

    for para in (p for p in markdown.split("\n\n") if p.strip()):
        if len(para) > max_chars:
            flush()
            current, size = [], 0
            for piece, sep in _split_long(para, max_chars):
                parts.append((piece, (sep or "\n\n") if parts else ""))
            continue
        if current and size + len(para) > max_chars:
            flush()
            current, size = [], 0
        current.append(para)
        size += len(para) + 2
    flush()
    return parts


def split_sections(markdown: str, max_chars: int) -> list[str]:
    """Group paragraphs into sections of at most ~max_chars; an over-long paragraph is split
    at line breaks, then at sentence ends."""
    return [section for section, _ in section_parts(markdown, max_chars)]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(model: str, text: str) -> str:
    return content_hash(f"v{PROMPT_VERSION}\0{model}\0{text}")


class Cache(Protocol):
    def get(self, key: str) -> str | None: ...

    def put(self, key: str, model: str, output: str) -> None: ...


def is_faithful(source: str, output: str) -> bool:
    try:
        check_faithful(source, output)
    except AIFormatError:
        return False
    return True


def cached_output(cache: Cache | None, model: str, section: str) -> str | None:
    """The cached AI output for this section, if it still passes check_faithful.

    Checked on every hit, so output accepted by an older, looser check is never served again.
    """
    hit = None if cache is None else cache.get(cache_key(model, section))
    return hit if hit is not None and is_faithful(section, hit) else None


@dataclass
class FailedSection:
    index: int  # 1-based position in the page
    reason: str  # why the last attempt was rejected
    attempts: int


@dataclass
class FormatResult:
    markdown: str  # failed sections hold their cleaned text
    model: str
    sections: int
    formatted: int  # sections whose AI output was accepted (fresh or cached)
    cached: int
    failed: list[FailedSection] = field(default_factory=list)

    @property
    def note(self) -> str | None:
        if not self.failed:
            return None
        head = f"{len(self.failed)} of {self.sections} section(s) kept as cleaned text"
        reasons = sorted({f.reason for f in self.failed if f.attempts})  # attempts=0: the user's choice
        return f"{head}: " + "; ".join(reasons) if reasons else f"{head}, as you chose."


def max_output_tokens(section: str) -> int:
    """Cap on the model's reply: about twice what a faithful re-layout needs.

    Small models sometimes loop, repeating text until the context fills up (seen with
    qwen2.5:1.5b: 3,600+ tokens for a ~500-token page, minutes at ~24 tokens/s). A capped
    reply is cut short, fails check_faithful, and that section keeps its cleaned text.
    (~4 characters per token; markup adds a little.)
    """
    return max(256, len(section) // 2)


def reply_token_cap(section: str, messages: list[dict], num_ctx: int) -> int:
    """max_output_tokens, but never more than the context has left after the prompt.

    If prompt + reply overflow num_ctx, Ollama silently drops the start of the prompt: the
    system prompt with the rules. (~3 characters per token, on the safe side.)
    """
    prompt_tokens = sum(len(m["content"]) for m in messages) // 3
    return max(64, min(max_output_tokens(section), num_ctx - prompt_tokens))


RETRY_PROMPT = (
    "That output was rejected: {reason}. Format the same text again. Keep every word exactly as written, "
    "with the same capitalisation, spelling and order, and remove nothing. Change only the layout "
    "(line breaks, headings, lists, tables). Output only the Markdown."
)


class _Hooks:
    """Progress/log/cancel callbacks for one page (all optional)."""

    def __init__(self, on_progress=None, on_log=None, on_written=None, should_stop=None):
        self.progress = on_progress or (lambda done, total, attempt: None)
        self.log = on_log or (lambda message: None)
        self.written = on_written or (lambda chars, expected: None)
        self._should_stop = should_stop or (lambda: False)

    def should_stop(self) -> bool:
        return self._should_stop()

    def check(self) -> None:
        if self._should_stop():
            raise AICancelled("Cancelled")


def _format_section(
    section: str,
    index: int,
    total: int,
    model: str,
    client: ChatClient,
    config: AIConfig,
    cache: Cache | None,
    force: bool,
    result: FormatResult,
    hooks: _Hooks,
) -> str:
    """Cache hit, or up to config.max_attempts model calls; each retry is told what was wrong.

    Returns the accepted Markdown, or the section's cleaned text (recorded in result.failed).
    """
    label = f"Part {index} of {total}" if total > 1 else "This page"
    key = cache_key(model, section)
    hit = None if force else cached_output(cache, model, section)
    if hit is not None:
        hooks.log(f"{label}: using the result accepted earlier.")
        result.formatted += 1
        result.cached += 1
        return hit
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": section}]
    reason = ""
    for attempt in range(1, config.max_attempts + 1):
        hooks.check()
        hooks.progress(index - 1, total, attempt)
        hooks.written(0, len(section))
        hooks.log(
            f"{label}: formatting (attempt 1 of {config.max_attempts})..."
            if attempt == 1
            else f"{label}: retrying with the correction (retry {attempt - 1} of {config.max_attempts - 1})..."
        )
        reply = strip_fences(
            client.chat(
                model,
                messages,
                reply_token_cap(section, messages, config.num_ctx),
                None if attempt == 1 else max(config.temperature, config.retry_temperature),
                on_text=lambda chars: hooks.written(chars, len(section)),
                should_stop=hooks.should_stop,
            )
        )
        try:
            check_faithful(section, reply)
        except AIFormatError as exc:
            reason = str(exc)
            hooks.log(f"{label}: attempt {attempt} rejected: {reason}.")
            # Show the model its rejected answer and exactly what was wrong with it.
            messages = messages[:2] + [
                {"role": "assistant", "content": reply[:6000]},
                {"role": "user", "content": RETRY_PROMPT.format(reason=reason)},
            ]
            continue
        hooks.log(f"{label}: accepted on attempt {attempt}; every word checked against the original.")
        if cache is not None:
            cache.put(key, model, reply)
        result.formatted += 1
        return reply
    hooks.log(f"{label}: still not right after {config.max_attempts} attempts; nothing changed yet.")
    result.failed.append(FailedSection(index=index, reason=reason, attempts=config.max_attempts))
    return section  # the deterministic cleanup, until the user decides


def format_markdown(
    text: str,
    *,
    client: ChatClient | None = None,
    config: AIConfig | None = None,
    cache: Cache | None = None,
    model: str | None = None,
    force: bool = False,
    use_clean_for_failed: bool = False,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_written: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> FormatResult:
    """AI-format one page, section by section. Raises AIUnavailable if there's no Ollama/model,
    AICancelled if should_stop() turns true (sections accepted so far stay cached).

    Sections that still fail after config.max_attempts are listed in result.failed (holding their
    cleaned text) so the caller can ask the user: retry, keep the cleaned text, or edit by hand.
    Accepted sections are cached, so a retry only re-runs the failed ones.

    use_clean_for_failed: the user chose "use cleaned text": sections not in the cache (the ones
    that failed before) keep their cleaned text without calling the model again.

    Progress for a UI: on_progress(sections_done, sections_total, attempt) (attempt 0 = finished),
    on_written(chars_written, chars_expected) as the reply streams in, on_log(message) per step.
    """
    config = config or AIConfig.from_env()
    client = client or OllamaClient(config)
    hooks = _Hooks(on_progress, on_log, on_written, should_stop)
    models = client.list_models()
    chosen = pick_model(models, model or config.model)
    if not chosen:
        raise AIUnavailable(f"Ollama has no models installed. Run: ollama pull {config.model}")

    parts = section_parts(text, config.section_chars)
    sections = [section for section, _ in parts]
    total = len(sections)
    result = FormatResult(markdown=text, model=chosen, sections=total, formatted=0, cached=0)
    hooks.log(f"Using {chosen}; the page is split into {total} part{'s' if total != 1 else ''}.")
    out: list[str] = []
    for index, section in enumerate(sections, start=1):
        if use_clean_for_failed and cache is not None and cached_output(cache, chosen, section) is None:
            hooks.log(f"Part {index} of {total}: keeping the cleaned text, as you chose.")
            result.failed.append(FailedSection(index=index, reason="kept as cleaned text", attempts=0))
            out.append(section)
            continue
        out.append(_format_section(section, index, total, chosen, client, config, cache, force, result, hooks))
    hooks.progress(total, total, 0)
    result.markdown = "".join(sep + section for (_, sep), section in zip(parts, out))
    return result
