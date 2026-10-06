"""Deterministic (rule-based) page cleanup."""
from __future__ import annotations

import pytest

from codex_engine.formatting import (
    Line,
    PageLayout,
    Span,
    first_heading,
    format_document,
    join_lines,
    markdown_to_plain,
)

H = 800.0  # page height used by the synthetic layouts


def line(text: str, y: float, size: float = 10.0, bold: bool = False) -> Line:
    return Line(spans=[Span(text, size, bold)], y0=y, y1=y + size)


def page(*blocks: list[Line]) -> PageLayout:
    return PageLayout(height=H, blocks=list(blocks))


def footer(n: int) -> list[Line]:
    return [line(f"Tome of Beasts {n}", 780)]


# ---- rule-based cleanup ----------------------------------------------------

def test_join_lines_dehyphenates_wrapped_words():
    assert join_lines(["The goblin is a mis-", "chievous creature."]) == "The goblin is a mischievous creature."
    # A real hyphenated compound followed by a capital stays as-is.
    assert join_lines(["a half-", "Orc warrior"]) == "a half- Orc warrior"


def test_headings_from_font_size_and_paragraph_reflow():
    pages = [
        page(
            [line("Goblins", 100, size=20)],
            [line("Goblins are small, black-hearted hu-", 140), line("manoids that lair in caves.", 152)],
        )
    ]
    (md,) = format_document(pages)
    assert md == "# Goblins\n\nGoblins are small, black-hearted humanoids that lair in caves."


def test_repeated_headers_footers_and_page_numbers_are_removed():
    pages = [
        page([line("CHAPTER 3 | MONSTERS", 10)], [line(f"Body text on page {n}.", 300)], footer(n), [line(str(n), 790)])
        for n in range(1, 7)
    ]
    for md in format_document(pages):
        assert "Tome of Beasts" not in md
        assert "CHAPTER 3" not in md
        assert md.startswith("Body text on page")


def test_body_text_in_margin_that_does_not_repeat_is_kept():
    pages = [page([line(f"Unique closing line {n}", 785)], [line("x", 300)]) for n in range(5)]
    pages[2] = page([line("A one-off final sentence.", 785)], [line("x", 300)])
    assert "A one-off final sentence." in format_document(pages)[2]


def test_bullets_and_numbered_lists():
    pages = [page([line("\u2022 First thing that", 100), line("wraps onto two lines", 112), line("\u2022 Second", 124), line("2) Step two", 136)])]
    (md,) = format_document(pages)
    assert md.split("\n\n") == ["- First thing that wraps onto two lines", "- Second", "2. Step two"]


def test_bold_labels_survive_and_short_line_blocks_keep_breaks():
    stat = [
        Line(spans=[Span("Armor Class", 10, True), Span(" 15", 10, False)], y0=100, y1=110),
        Line(spans=[Span("Hit Points", 10, True), Span(" 7 (2d6)", 10, False)], y0=112, y1=122),
        Line(spans=[Span("Speed", 10, True), Span(" 30 ft.", 10, False)], y0=124, y1=134),
    ]
    (md,) = format_document([page(stat)])
    assert md == "**Armor Class** 15\n**Hit Points** 7 (2d6)\n**Speed** 30 ft."


def test_short_bold_line_becomes_minor_heading():
    (md,) = format_document([page([line("Actions", 100, bold=True)], [line("Scimitar. Melee attack.", 120)])])
    assert md.startswith("#### Actions\n\n")


def test_markdown_to_plain_and_first_heading():
    md = "## Goblin Boss\n\n- **Armor Class** 17\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nA 3\\*"
    plain = markdown_to_plain(md)
    assert "#" not in plain and "**" not in plain and "|" not in plain
    assert "Goblin Boss" in plain and "Armor Class 17" in plain and "A 3*" in plain
    assert first_heading(md) == "Goblin Boss"


def test_end_to_end_on_a_real_pdf(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from codex_engine.ingest import build_page_texts

    path = tmp_path / "book.pdf"
    doc = pymupdf.open()
    for n in range(1, 6):
        p = doc.new_page(width=600, height=800)
        p.insert_text((50, 30), "MONSTER MANUAL", fontsize=8)
        p.insert_text((50, 120), f"Owlbear {n}", fontsize=22, fontname="hebo")
        p.insert_text((50, 170), "An owlbear's screech echoes through dark val-", fontsize=10)
        p.insert_text((50, 182), "leys and forests.", fontsize=10)
        p.insert_text((290, 780), str(n), fontsize=8)
    doc.save(path)
    doc.close()

    pages = build_page_texts(path)
    assert len(pages) == 5
    assert pages[0].startswith("# Owlbear 1")
    assert "valleys and forests." in pages[0]
    assert "MONSTER MANUAL" not in pages[0]


# ---- fixes found on real books ------------------------------------------------

def test_ability_scores_become_a_table():
    stats = [line(t, 100 + i * 10) for i, t in enumerate(["STR", "18 (+4)", "DEX", "11 (+0)", "CON", "14 (+2)"])]
    stats2 = [line(t, 200 + i * 10) for i, t in enumerate(["INT", "11 (+0)", "WIS", "10 (+0)", "CHA", "9 (-1)"])]
    (md,) = format_document([page(stats, stats2)])
    assert md == "| STR | DEX | CON | INT | WIS | CHA |\n|---|---|---|---|---|---|\n| 18 (+4) | 11 (+0) | 14 (+2) | 11 (+0) | 10 (+0) | 9 (-1) |"


def test_runin_label_in_a_different_font_is_bold():
    stat = Line(spans=[Span("Armor Class", 7, False, "Type3 (1)"), Span(" 18 (chain mail)", 7, False, "Type3 (2)")], y0=100, y1=107)
    (md,) = format_document([page([stat], [line("Some body text here that is long enough.", 200, size=7)])])
    assert md.startswith("**Armor Class** 18 (chain mail)")


def test_replacement_char_between_letters_becomes_apostrophe():
    from codex_engine.formatting import _clean_text

    assert _clean_text("tinker\ufffds tools") == "tinker's tools"


def test_stacked_duplicate_headings_are_dropped(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from codex_engine.ingest import build_page_texts

    path = tmp_path / "gmbinder.pdf"
    doc = pymupdf.open()
    p = doc.new_page(width=600, height=800)
    p.insert_text((50, 300), "GUNSLINGER", fontsize=14)
    p.insert_text((50, 300), "GUNSLINGER", fontsize=14)
    p.insert_text((50, 330), "You have trained to wield a gun in each hand.", fontsize=10)
    doc.save(path)
    doc.close()
    (md,) = build_page_texts(path)
    assert md.count("GUNSLINGER") == 1


# ---- preservation of legitimate content ---------------------------------------

def test_paragraph_breaks_between_blocks_are_preserved():
    pages = [page([line("First paragraph ends here.", 100)], [line("Second paragraph starts here.", 130)])]
    assert format_document(pages)[0] == "First paragraph ends here.\n\nSecond paragraph starts here."


def test_numbers_in_body_text_are_kept():
    # A bare number mid-page (table cell, dice result) is content, not a page number.
    body = [line("Roll on the table below.", 300), line("12", 312), line("Result: a goblin ambush.", 324)]
    pages = [page(body, footer(n)) for n in range(1, 6)]
    md = format_document(pages)[0]
    assert "12" in md and "Roll on the table below." in md


def test_unique_chapter_title_at_top_of_page_is_kept():
    pages = [page([line("Body text.", 300)], footer(n)) for n in range(1, 6)]
    pages[0].blocks.insert(0, [line("Chapter One: The Road", 20, size=20)])
    assert format_document(pages)[0].startswith("# Chapter One: The Road")


def test_hyphen_kept_when_next_line_starts_uppercase_or_digit():
    assert join_lines(["See pre-", "1990 rules"]) == "See pre- 1990 rules"
    assert join_lines(["imag-", "ination"]) == "imagination"


def test_real_compound_broken_at_line_end_keeps_its_hyphen():
    # "well-known" appears hyphenated mid-line elsewhere, so the line-end hyphen is real.
    pages = [
        page([line("A well-known tavern stands here.", 100)]),
        page([line("The innkeeper is well-", 100), line("known in town, and a mis-", 112), line("chievous gossip.", 124)]),
    ]
    assert format_document(pages)[1] == "The innkeeper is well-known in town, and a mischievous gossip."


def test_whitespace_normalised_without_flattening_structure():
    pages = [page([line("Too    many   spaces\there.", 100)], [line("\u2022 item", 130)])]
    assert format_document(pages)[0] == "Too many spaces here.\n\n- item"


def test_raw_text_is_kept_from_extraction(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from codex_engine.ingest import build_source_content, extract_layouts

    path = tmp_path / "raw.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 100), "Exactly as extracted", fontsize=11)
    doc.save(path)
    doc.close()
    pages, chunks = build_source_content(extract_layouts(path))
    assert "Exactly as extracted" in pages[0].raw_text
    assert pages[0].clean_version >= 1
    assert chunks and chunks[0].body == "Exactly as extracted"


# ---- GM Binder-style layouts (from Technomancer's Textbook) -----------------------------

def span(text, size=9.0, font="Georgia", bold=False):
    return Span(text, size, bold, font)


def labelled(label, value, y, label_font="Type3 (1113 0 R)", label_size=8.5, value_font="ScalySans", value_size=10.0):
    return Line(spans=[span(label, label_size, label_font), span(" " + value, value_size, value_font)], y0=y, y1=y + 10, x0=320)


def big_book(*pages_blocks):
    """Pages plus enough 9pt Georgia body text that font shares are meaningful."""
    filler = [[Line(spans=[span("Body text of the book goes on and on here. " * 3)], y0=500 + i, y1=510 + i)] for i in range(3)]
    stat_font_text = [[Line(spans=[span("value text " * 6, 10.0, "ScalySans")], y0=600, y1=610)]]
    pages = [PageLayout(height=H, blocks=list(b)) for b in pages_blocks]
    pages += [PageLayout(height=H, blocks=filler + stat_font_text) for _ in range(60)]
    return pages


def test_lines_hanging_off_the_page_edge_are_dropped():
    class FakePage:
        rect = type("R", (), {"width": 612.0, "height": 792.0})()

        def get_text(self, kind, flags=0):
            if kind == "text":
                return "raw"
            line = lambda text, x0, x1: {"bbox": (x0, 100, x1, 110), "spans": [{"text": text, "size": 9, "flags": 0, "font": "Georgia"}]}
            return {"width": 612.0, "height": 792.0, "blocks": [
                {"type": 0, "lines": [line("Real text in the column.", 320, 560)]},
                {"type": 0, "lines": [line("When", 592.2, 612), line("an up", 592.2, 612)]},  # clipped overflow
            ]}

    from codex_engine.formatting import layout_from_pymupdf

    layout = layout_from_pymupdf(FakePage())
    assert [l.text for b in layout.blocks for l in b] == ["Real text in the column."]


def test_common_larger_stat_font_is_not_a_heading_and_labels_are_bold():
    block = [labelled("Armor Class", "16 (natural armor)", 75), labelled("Hit Points", "72 (11d8 + 22)", 86), labelled("Speed", "30 ft.", 97)]
    md = format_document(big_book([block]))[0]
    assert md == "**Armor Class** 16 (natural armor)\n**Hit Points** 72 (11d8 + 22)\n**Speed** 30 ft."


def test_rare_large_font_is_still_a_heading():
    heading = [Line(spans=[span("RIOT DRONE", 12.8, "Type3 (1111 0 R)")], y0=40, y1=52)]
    assert format_document(big_book([heading]))[0] == "## RIOT DRONE"  # 12.8pt vs 9pt body


def test_ability_header_row_is_a_table_not_a_heading():
    names = [Line(spans=[span(n, 9.0, "Georgia-Bold", True)], y0=120, y1=130, x0=330 + 40 * i) for i, n in enumerate(["STR", "DEX", "CON"])]
    values = [Line(spans=[span(v)], y0=133, y1=143) for v in ["19 (+4)", "11 (+0)", "15 (+2)"]]
    md = format_document(big_book([names + values]))[0]
    assert md.startswith("| STR | DEX | CON |") and "#" not in md


def test_font_switch_mid_sentence_is_not_a_label():
    prose = [
        Line(spans=[span("Two spectral wings sprout from your back. Until the", 7.0, "Type3 (5 0 R)")], y0=100, y1=108),
        Line(spans=[span("transformation ends, you have a", 7.0, "Type3 (9 0 R)"), span(" flying speed.", 7.0, "Type3 (5 0 R)")], y0=109, y1=117),
    ]
    md = format_document([page(prose)])[0]
    assert "**" not in md


def test_real_labels_starting_with_digits_or_containing_colons_stay_bold():
    lines = [
        Line(spans=[span("3/day each:", 7.0, "Type3 (2 0 R)"), span(" Dispel Magic, Fly", 7.0, "Type3 (5 0 R)")], y0=100, y1=108),
        Line(spans=[span("Monk: Way of the Primal Forces", 9.0, "Type3 (3 0 R)"), span(" begins here.", 9.0, "Georgia")], y0=120, y1=128),
    ]
    md = format_document([page(lines[:1]), page(lines[1:])])
    assert md[0].startswith("**3/day each:**") and md[1].startswith("**Monk: Way of the Primal Forces**")


def test_short_documents_keep_size_based_headings():
    # Font-share rules need a whole book's worth of text; a handout keeps its headings.
    (md,) = format_document([page([line("Goblins", 100, size=20)], [line("Small, black-hearted humanoids.", 140)])])
    assert md.startswith("# Goblins")


def test_replacement_char_after_a_word_or_between_digits():
    from codex_engine.formatting import _clean_text

    assert _clean_text("the creatures\ufffd spaces") == "the creatures' spaces"
    assert _clean_text("Recharge 5\ufffd6") == "Recharge 5\u20136"
