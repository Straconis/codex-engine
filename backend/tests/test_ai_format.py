"""AI formatting pass: validation, per-section fallback, caching, config. Ollama is mocked."""
from __future__ import annotations

import re

import pytest

from codex_engine import ai_format
from codex_engine.ai_format import AIConfig, AIFormatError, AIUnavailable, check_faithful, describe_changes, format_markdown


class FakeClient:
    """Stands in for Ollama. `respond(text)` or `respond(text, attempt)` gives the model's reply."""

    def __init__(self, respond=lambda text: text, models=("qwen2.5:1.5b",)):
        self.respond = respond
        self.models = list(models)
        self.calls: list[dict] = []
        self.max_tokens = None

    def list_models(self):
        return self.models

    def chat(self, model, messages, max_tokens=None, temperature=None, on_text=None, should_stop=None):
        if should_stop and should_stop():
            raise ai_format.AICancelled("Cancelled")
        section = messages[1]["content"]
        attempt = 1 + sum(1 for c in self.calls if c["section"] == section)  # nth call for this section
        self.calls.append({"model": model, "section": section, "attempt": attempt, "temperature": temperature, "messages": messages})
        self.max_tokens = max_tokens
        try:
            reply = self.respond(section, attempt)
        except TypeError:
            reply = self.respond(section)
        if on_text:
            on_text(len(reply))
        return reply


class DictCache:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, model, output):
        self.data[key] = output


CONFIG = AIConfig(section_chars=200)
PROSE = (
    "It was a dark and stormy night; the rain fell in torrents, except at occasional intervals, "
    "when it was checked by a violent gust of wind which swept up the streets."
)


# ---- validation -----------------------------------------------------------------

def test_layout_only_changes_are_accepted():
    src = "CHAPTER ONE The Storm\n\nIt was a dark and stormy night;\nthe rain fell in torrents."
    out = "# CHAPTER ONE\n\n## The Storm\n\nIt was a dark and stormy night; the rain fell in torrents."
    check_faithful(src, out)


def test_quote_list_and_section_break_markup_is_accepted():
    src = "As the poet said: all that glitters is not gold. Bring: rope, torch, rations. * * * Morning came."
    out = "As the poet said:\n\n> all that glitters is not gold.\n\nBring:\n\n- rope,\n- torch,\n- rations.\n\n---\n\nMorning came."
    check_faithful(src, out)


@pytest.mark.parametrize(
    "bad, reason",
    [
        ("It was a dark night.", "length"),  # summarised
        (PROSE + " Thunder roared and the hero, filled with dread, drew his sword in the gloom.", "length|added"),
        ("Here is the formatted text:\n\n" + PROSE, "added"),  # chatty preamble
        (PROSE.replace("violent gust of wind", "strong breeze"), "changed words"),  # reworded
        ("the rain fell in torrents, except at occasional intervals, It was a dark and stormy night; "
         "when it was checked by a violent gust of wind which swept up the streets.", "dropped"),  # reordered
        ("", "nothing"),
    ],
)
def test_destructive_output_is_rejected(bad, reason):
    with pytest.raises(AIFormatError, match=reason):
        check_faithful(PROSE, bad)


def test_changing_a_single_word_in_a_long_section_is_rejected():
    src = " ".join([PROSE] * 4) + " The guard swore a damned oath and spat."
    with pytest.raises(AIFormatError, match="changed words .*damned"):
        check_faithful(src, src.replace("damned", "darned"))
    with pytest.raises(AIFormatError, match="changed words"):
        check_faithful(src, src.replace("torrents", "torrent", 1))  # "modernised"/corrected


def test_rejoining_split_words_is_allowed():
    check_faithful("a sense of imag ination and won der " + PROSE, "a sense of imagination and wonder " + PROSE)


def test_only_a_bare_page_number_may_be_dropped():
    body = " ".join([PROSE] * 4)
    check_faithful("12\n" + body, body)  # stray page number on its own line
    check_faithful(body + "\n\n12", body)
    with pytest.raises(AIFormatError, match="dropped"):
        check_faithful("THE TOME OF STORMS " + body, body)  # cleanup already removes real headers
    sentence = "Then the old wizard opened the door and walked slowly out into the pouring rain alone."
    with pytest.raises(AIFormatError, match="dropped"):
        check_faithful(body + " " + sentence + " " + body, body + " " + body)


# Real output from qwen2.5:1.5b on Monsters of the Multiverse p. 276 that the old check accepted.
STAT_BLOCK = """### OGRE BOLT LAUNCHER

Large Giant, Typically Chaotic Evil

**Armor Class** 13 (hide armor)

**Hit Points** 59 (7d10 + 21)

**Speed** 40 ft.

**Senses** darkvision 60 ft., passive Perception 8

**Languages** Common, Giant

**Challenge** 2 (450 XP) **Proficiency Bonus** +2"""


# Edits a small model can make that change what a rule says. All of these got through before 0.3.8.
FOREST = "The party travels through the dark forest toward the ruined keep on the hill. " * 4


@pytest.mark.parametrize(
    "src, bad, reason",
    [
        (FOREST + "The door is locked.", FOREST + "The door is not locked.", "added text .*'not' after 'is'"),
        (FOREST + "The ogre deals 10 damage on a hit.", FOREST + "The ogre deals damage on a hit.", "dropped text .*'10'"),
        (FOREST + "Roll 12 dice.", FOREST + "Roll dice.", "dropped text .*'12'"),  # a number mid-sentence is not a page number
        (FOREST + "On a roll of 1-2 the spell fails.", FOREST + "On a roll of 12 the spell fails.", "changed words .*'1 2' -> '12'"),
        (FOREST + "Roll 1 0 dice.", FOREST + "Roll 10 dice.", "changed words"),
        (FOREST + "Carry a ten-foot pole.", FOREST + "Carry a tenfoot pole.", "removed '-'"),
        (FOREST + "Apply a -2 penalty.", FOREST + "Apply a 2 penalty.", "removed '-'"),
    ],
)
def test_meaning_changing_edits_are_rejected(src, bad, reason):
    with pytest.raises(AIFormatError, match=reason):
        check_faithful(src, bad)


def test_words_split_across_lines_may_be_rejoined():
    check_faithful(FOREST + "a sense of imag-\nination.", FOREST + "a sense of imagination.")
    check_faithful(FOREST + "a sense of imag- ination.", FOREST + "a sense of imagination.")
    check_faithful(FOREST + "a ten-\nfoot pole.", FOREST + "a ten-foot pole.")  # hyphen kept is fine too


def test_dropping_a_short_stat_block_line_is_rejected():
    bad = STAT_BLOCK.replace("**Senses** darkvision 60 ft., passive Perception 8\n\n", "")
    with pytest.raises(AIFormatError, match="dropped text .*Senses darkvision"):
        check_faithful(STAT_BLOCK, bad)


def test_changing_capitalisation_is_rejected():
    with pytest.raises(AIFormatError, match="changed words .*OGRE"):
        check_faithful(STAT_BLOCK, STAT_BLOCK.replace("OGRE BOLT LAUNCHER", "Ogre Bolt Launcher"))
    check_faithful(STAT_BLOCK, STAT_BLOCK.replace("### OGRE BOLT LAUNCHER", "## OGRE BOLT LAUNCHER"))  # layout only


def test_prompt_demands_exact_capitalisation_and_no_removal():
    from codex_engine.ai_format import SYSTEM_PROMPT

    assert "capitalisation" in SYSTEM_PROMPT and "Never remove" in SYSTEM_PROMPT


# ---- pipeline -------------------------------------------------------------------

def test_sections_are_formatted_and_joined():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 30 for i in range(4))
    client = FakeClient(lambda t: t.replace("Paragraph", "## Paragraph", 1))
    result = format_markdown(text, client=client, config=CONFIG)
    assert len(client.calls) == result.sections > 1
    assert result.formatted == result.sections and not result.failed
    assert result.markdown.count("## Paragraph") == result.sections


def test_retry_tells_the_model_what_was_wrong_and_can_succeed():
    # Wrong (capitalisation changed) twice, right on the third attempt.
    src = "### OGRE BOLT LAUNCHER\n\n" + PROSE
    client = FakeClient(lambda t, attempt: t.replace("OGRE BOLT LAUNCHER", "Ogre Bolt Launcher") if attempt < 3 else "## " + t.lstrip("# "))
    result = format_markdown(src, client=client, config=AIConfig())
    assert result.formatted == 1 and not result.failed
    assert [c["attempt"] for c in client.calls] == [1, 2, 3]
    retry = client.calls[1]["messages"]
    assert retry[2]["role"] == "assistant" and "Ogre Bolt Launcher" in retry[2]["content"]
    assert retry[3]["role"] == "user" and "OGRE BOLT LAUNCHER" in retry[3]["content"] and "capitalisation" in retry[3]["content"]
    assert client.calls[0]["temperature"] is None and client.calls[1]["temperature"] == 0.3


def test_section_failing_every_attempt_is_reported_not_silently_dropped():
    text = "\n\n".join(["First section " + "alpha " * 40, "Second section " + "beta " * 40])
    client = FakeClient(lambda t: "A short summary." if "Second" in t else "# " + t)
    result = format_markdown(text, client=client, config=CONFIG)
    assert result.formatted == 1
    assert [(f.index, f.attempts) for f in result.failed] == [(2, CONFIG.max_attempts)]
    assert "length" in result.failed[0].reason
    assert len(client.calls) == 1 + CONFIG.max_attempts  # 1 try + 3 retries for the failing one
    assert ("Second section " + "beta " * 40).strip() in result.markdown  # cleaned text stands in
    assert "1 of 2 section(s) kept as cleaned text" in result.note


def test_retrying_after_failure_only_reruns_the_failed_sections():
    text = "\n\n".join(["First section " + "alpha " * 40, "Second section " + "beta " * 40])
    cache = DictCache()
    format_markdown(text, client=FakeClient(lambda t: "nope" if "Second" in t else "# " + t), config=CONFIG, cache=cache)
    second = FakeClient(lambda t: "# " + t)
    result = format_markdown(text, client=second, config=CONFIG, cache=cache)
    assert [c["section"].split()[0] for c in second.calls] == ["Second"]  # first came from the cache
    assert result.formatted == 2 and not result.failed


def test_use_cleaned_text_for_failed_sections_does_not_call_the_model_again():
    text = "\n\n".join(["First section " + "alpha " * 40, "Second section " + "beta " * 40])
    cache = DictCache()
    format_markdown(text, client=FakeClient(lambda t: "nope" if "Second" in t else "# " + t), config=CONFIG, cache=cache)
    again = FakeClient(lambda t: "# " + t)
    result = format_markdown(text, client=again, config=CONFIG, cache=cache, use_clean_for_failed=True)
    assert again.calls == []
    assert result.formatted == 1 and [f.index for f in result.failed] == [2]
    assert result.markdown.startswith("# First section")


def test_all_sections_failing_reports_them_instead_of_raising():
    result = format_markdown(PROSE, client=FakeClient(lambda t: "Sure! Here's a summary."), config=CONFIG)
    assert result.formatted == 0 and len(result.failed) == result.sections
    assert result.markdown == PROSE


def test_code_fences_are_stripped():
    client = FakeClient(lambda t: "```markdown\n" + t + "\n```")
    assert format_markdown(PROSE, client=client, config=CONFIG).markdown == PROSE


def test_cache_prevents_repeat_calls_and_force_bypasses_it():
    client = FakeClient(lambda t: "# " + t)
    cache = DictCache()
    first = format_markdown(PROSE, client=client, config=CONFIG, cache=cache)
    second = format_markdown(PROSE, client=client, config=CONFIG, cache=cache)
    assert len(client.calls) == 1
    assert second.markdown == first.markdown and second.cached == 1
    format_markdown(PROSE, client=client, config=CONFIG, cache=cache, force=True)
    assert len(client.calls) == 2


def test_cache_is_keyed_by_model_and_rejections_are_not_cached():
    cache = DictCache()
    format_markdown(PROSE, client=FakeClient(lambda t: "# " + t, models=["a:1b", "b:1b"]), config=CONFIG, cache=cache, model="a:1b")
    other = FakeClient(lambda t: "# " + t, models=["a:1b", "b:1b"])
    format_markdown(PROSE, client=other, config=CONFIG, cache=cache, model="b:1b")
    assert len(other.calls) == 1  # different model -> not served from a:1b's cache

    bad_cache = DictCache()
    format_markdown(PROSE, client=FakeClient(lambda t: "nope"), config=CONFIG, cache=bad_cache)
    assert bad_cache.data == {}


def test_cached_output_that_fails_the_current_check_is_formatted_again():
    cache = DictCache()
    loose = PROSE.replace("violent gust", "not violent gust")  # what the pre-0.3.8 check let through
    cache.put(ai_format.cache_key("qwen2.5:1.5b", PROSE), "qwen2.5:1.5b", loose)
    client = FakeClient(lambda t: "# " + t)
    result = format_markdown(PROSE, client=client, config=CONFIG, cache=cache)
    assert len(client.calls) == 1 and result.cached == 0
    assert result.markdown == "# " + PROSE


# ---- availability + config ----------------------------------------------------------

def test_no_models_is_unavailable_not_a_crash():
    with pytest.raises(AIUnavailable, match="ollama pull"):
        format_markdown(PROSE, client=FakeClient(models=[]), config=CONFIG)
    info = ai_format.status(client=FakeClient(models=[]), config=CONFIG)
    assert info["available"] is False and "ollama pull" in info["error"]


def test_status_when_ollama_is_not_running():
    info = ai_format.status(config=AIConfig(url="http://127.0.0.1:9"))
    assert info["available"] is False and "not reachable" in info["error"]


def test_model_selection():
    assert ai_format.pick_model(["llama3.2:3b", "qwen2.5:1.5b"], "qwen2.5:1.5b") == "qwen2.5:1.5b"
    assert ai_format.pick_model(["llama3.2:3b"], "llama3.2") == "llama3.2:3b"
    assert ai_format.pick_model(["llama3.2:3b"], "missing") == "llama3.2:3b"  # falls back to installed
    assert ai_format.pick_model([], "x") is None


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("CODEX_ENGINE_OLLAMA_MODEL", "gemma3:1b")
    monkeypatch.setenv("CODEX_ENGINE_OLLAMA_URL", "http://box:11434/")
    monkeypatch.setenv("CODEX_ENGINE_AI_SECTION_CHARS", "1500")
    monkeypatch.setenv("CODEX_ENGINE_AI_ATTEMPTS", "6")
    monkeypatch.setenv("CODEX_ENGINE_AI_RETRY_TEMPERATURE", "0.5")
    cfg = AIConfig.from_env()
    assert cfg.retry_temperature == 0.5
    assert (cfg.model, cfg.url, cfg.section_chars, cfg.max_attempts) == ("gemma3:1b", "http://box:11434", 1500, 6)
    assert AIConfig().max_attempts == 4  # 1 try + 3 automatic retries


def test_split_sections_keeps_paragraphs_whole_when_they_fit():
    paras = ["a" * 150, "b" * 150, "c" * 190]
    assert ai_format.split_sections("\n\n".join(paras), 200) == paras


def test_an_over_long_paragraph_is_split_at_lines_then_sentences_and_rebuilt_exactly():
    table = "\n".join(f"| Row {i} | {i * 3} gp |" for i in range(40))  # one paragraph, ~700 chars
    sentences = " ".join(f"Sentence number {i} ends here." for i in range(30))  # one long line
    text = PROSE + "\n\n" + table + "\n\n" + sentences
    parts = ai_format.section_parts(text, 200)
    assert all(len(section) <= 200 for section, _ in parts)
    assert "".join(sep + section for section, sep in parts) == text
    assert format_markdown(text, client=FakeClient(), config=CONFIG).markdown == text


def test_reply_cap_leaves_room_for_the_prompt_in_the_context():
    section = "x" * 3000
    messages = [{"content": ai_format.SYSTEM_PROMPT}, {"content": section}]
    assert ai_format.reply_token_cap(section, messages, 8192) == ai_format.max_output_tokens(section)
    assert ai_format.reply_token_cap(section, messages, 2048) < 2048 - 3000 // 3


def test_progress_reports_sections_and_attempts():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 30 for i in range(3))
    seen: list[tuple[int, int, int]] = []
    client = FakeClient(lambda t, attempt: "nope" if attempt == 1 and t.startswith("Paragraph 1") else t)
    result = format_markdown(text, client=client, config=CONFIG, on_progress=lambda d, t, a: seen.append((d, t, a)))
    n = result.sections
    assert seen == [(0, n, 1), (1, n, 1), (1, n, 2), (2, n, 1), (n, n, 0)]


def test_output_is_capped_so_a_looping_model_cannot_run_for_minutes():
    client = FakeClient(lambda t: t + " " + "and so on " * 500)  # runaway repetition
    result = format_markdown(PROSE, client=client, config=CONFIG)
    assert result.formatted == 0 and result.failed
    assert client.max_tokens == ai_format.max_output_tokens(PROSE)
    assert 256 <= ai_format.max_output_tokens("x" * 3000) <= 2000


class FakeStream:
    """A streamed /api/chat response: one JSON object per line, like Ollama."""

    def __init__(self, pieces):
        import json as _json

        self.lines = [(_json.dumps({"message": {"content": p}, "done": False}) + "\n").encode() for p in pieces]
        self.lines.append(b'{"done": true}\n')
        self.closed = False

    def __iter__(self):
        return iter(self.lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def test_ollama_request_streams_and_sends_the_token_cap(monkeypatch):
    import json as _json

    sent = {}
    stream = FakeStream(["# Hea", "ding\n\n", "Body text."])

    def fake_urlopen(req, timeout=None):
        sent.update(_json.loads(req.data))
        return stream

    monkeypatch.setattr(ai_format.urllib.request, "urlopen", fake_urlopen)
    progress = []
    reply = ai_format.OllamaClient(AIConfig()).chat(
        "m", [{"role": "user", "content": "user text"}], max_tokens=321, temperature=0.3, on_text=progress.append
    )
    assert reply == "# Heading\n\nBody text." and progress == [5, 11, 21]
    assert sent["stream"] is True and sent["options"]["num_predict"] == 321 and sent["options"]["temperature"] == 0.3
    assert stream.closed


def test_cancel_mid_reply_stops_reading_the_stream(monkeypatch):
    stream = FakeStream(["one ", "two ", "three"])
    monkeypatch.setattr(ai_format.urllib.request, "urlopen", lambda req, timeout=None: stream)
    seen = []
    with pytest.raises(ai_format.AICancelled):
        ai_format.OllamaClient(AIConfig()).chat("m", [], on_text=seen.append, should_stop=lambda: len(seen) >= 1)
    assert seen == [4] and stream.closed  # closing the connection makes Ollama stop generating



def test_log_explains_each_step_in_plain_language():
    src = "### OGRE BOLT LAUNCHER\n\n" + PROSE
    client = FakeClient(lambda t, attempt: t.replace("OGRE BOLT LAUNCHER", "Ogre Bolt Launcher") if attempt < 2 else t)
    log: list[str] = []
    format_markdown(src, client=client, config=AIConfig(), on_log=log.append)
    assert log[0].startswith("Using qwen2.5:1.5b; the page is split into 1 part")
    assert "This page: formatting (attempt 1 of 4)..." in log
    assert any("attempt 1 rejected: output changed words ('OGRE BOLT LAUNCHER' -> 'Ogre Bolt Launcher')" in m for m in log)
    assert "This page: retrying with the correction (retry 1 of 3)..." in log
    assert log[-1].startswith("This page: accepted on attempt 2")


def test_written_progress_resets_each_attempt():
    written: list[tuple[int, int]] = []
    client = FakeClient(lambda t, attempt: "nope" if attempt == 1 else t)
    format_markdown(PROSE, client=client, config=AIConfig(), on_written=lambda c, e: written.append((c, e)))
    assert written == [(0, len(PROSE)), (4, len(PROSE)), (0, len(PROSE)), (len(PROSE), len(PROSE))]


def test_cancel_between_attempts_raises_and_keeps_nothing_new():
    calls = {"n": 0}

    def respond(t):
        calls["n"] += 1
        return "nope"

    cache = DictCache()
    with pytest.raises(ai_format.AICancelled):
        format_markdown(PROSE, client=FakeClient(respond), config=AIConfig(), cache=cache, should_stop=lambda: calls["n"] >= 1)
    assert calls["n"] == 1 and cache.data == {}


# ---- punctuation (Random-Trap-Generator p. 38: periods dropped after bold labels) ----------

TRAP = (
    "**1.​ Recent Days.** A creature loses their most recent memories, forgetting the past hour.\n\n"
    "**Effect.** Creatures in the trap’s area must make a Wisdom saving throw, or forget."
)


def test_dropped_period_after_a_label_is_rejected():
    ai = TRAP.replace("Recent Days.**", "Recent Days**").replace("Effect.**", "Effect**").replace("​", "")
    with pytest.raises(AIFormatError, match=r"changed punctuation .*removed '\.' after 'Days'"):
        check_faithful(TRAP, ai)


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda t: t.replace("throw,", "throw"), "removed ','"),
        (lambda t: t.replace("forget.", "forget!"), "changed '.' to '!'"),
        (lambda t: t.replace("memories,", "memories;"), "changed ',' to ';'"),
        (lambda t: t.replace("past hour.", "past hour.."), "added '.'"),
    ],
)
def test_other_punctuation_changes_are_rejected(change, expected):
    with pytest.raises(AIFormatError, match=re.escape(expected)):
        check_faithful(TRAP, change(TRAP))


def test_layout_and_invisible_characters_may_change():
    layout = (
        "### Recent Days\n\n"
        "1. **Recent Days.** A creature loses their most recent memories, forgetting the past hour.\n\n"
        "> **Effect.** Creatures in the trap's area must make a Wisdom saving throw, or forget."
    )
    check_faithful(TRAP.replace("**1.​ ", "1. **"), layout.replace("### Recent Days\n\n", ""))
    check_faithful(TRAP, TRAP.replace("​", ""))  # invisible character removed
    check_faithful(TRAP, TRAP.replace("’", "'"))  # curly vs straight apostrophe
    # Header row then values, as PDFs extract stat rows. (A table that would reorder the words,
    # e.g. from "STR 18 DEX 11", is rejected by the word-order check.)
    table_src = PROSE + "\n\nSTR DEX\n18 (+4) 11 (+0)"
    check_faithful(table_src, PROSE + "\n\n| STR | DEX |\n|---|---|\n| 18 (+4) | 11 (+0) |")


def test_describe_changes_counts_only_the_lines_that_changed():
    source = "\n".join(f"Line {i}." for i in range(10))
    output = "# Title\n" + source  # one heading line added at the top
    assert describe_changes(source, output)["lines_changed"] == 1


def test_describe_changes_spots_an_already_formatted_page():
    info = describe_changes(TRAP, TRAP.replace("​", ""))
    assert info["meaningful"] is False and info["invisible_removed"] == 1
    assert info["summary"].startswith("This page was already well formatted")

    info = describe_changes("ARMOR CLASS 15\nHit Points 7", "## ARMOR CLASS 15\n\n**Hit Points** 7")
    assert info["meaningful"] is True and info["markup"] == 6 and info["line_breaks"] >= 1
    assert info["summary"].startswith("Layout changed on") and info["summary"].endswith("The words are unchanged.")
