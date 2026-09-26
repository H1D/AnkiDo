# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import pytest

from ankido.collection.render import (
    clean_html,
    extract_media,
    html_to_text,
    normalize_headword,
    render,
    split_answer,
)
from cardhtml import BASIC_A, BASIC_Q, CLOZE_A, CLOZE_Q, CLOZE_Q_HINT, RAW_FRONT_WITH_SOUND


def test_html_mode_strips_style_av_and_sound_but_keeps_markup() -> None:
    r = render(BASIC_Q, BASIC_A, "html")
    assert "<style" not in r.question
    assert "[anki:play" not in r.question
    assert "<b>huis</b>" in r.question
    assert r.answer == "house &amp; <i>home</i><div>the house</div>"
    assert r.media == ["huis.png"]


def test_text_mode_basic_card() -> None:
    r = render(BASIC_Q, BASIC_A)
    assert r.question == "het **huis**\n[img:huis.png]"
    assert r.answer == "house & _home_\nthe house"
    assert r.media == ["huis.png"]


def test_text_mode_cloze_question_and_answer() -> None:
    r = render(CLOZE_Q, CLOZE_A, "text")
    assert r.question == "The [...] of France"
    # Anki emits "<br>\n" here and the source newline survives as an extra blank line.
    assert [ln for ln in r.answer.splitlines() if ln] == ["The [capital] of France", "extra"]


def test_html_mode_keeps_cloze_spans() -> None:
    r = render(CLOZE_Q, CLOZE_A, "html")
    assert 'class="cloze"' in r.question
    assert "[anki:play" not in r.question
    assert 'class="cloze-inactive"' in r.answer


def test_cloze_hint_is_kept_verbatim() -> None:
    assert html_to_text(CLOZE_Q_HINT) == "The [city] of France"


def test_nested_span_inside_cloze_is_flattened() -> None:
    src = '<span class="cloze"><span class="nested">cap</span>ital</span> x'
    assert html_to_text(src) == "[capital] x"


@pytest.mark.parametrize(
    "marker",
    ["<hr id=answer>", '<hr id="answer">', "<hr id='answer' />", "<HR ID=ANSWER>"],
)
def test_split_answer_marker_variants(marker: str) -> None:
    assert split_answer(f"question{marker}answer") == "answer"


def test_split_answer_without_marker_returns_everything() -> None:
    assert split_answer("just the answer") == "just the answer"


def test_unknown_render_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown render mode"):
        render("q", "a", "markdown")


def test_script_and_style_removed_in_both_paths() -> None:
    src = "<script>alert(1)</script><style>.x{}</style>hi"
    assert clean_html(src) == "hi"
    assert html_to_text(src) == "hi"


def test_bold_italic_markers() -> None:
    assert html_to_text("<b>a</b> <i>b</i>") == "**a** _b_"
    assert html_to_text("<strong>a</strong> <em>b</em>") == "**a** _b_"


def test_br_and_block_elements_become_newlines_and_collapse() -> None:
    src = "<p>a</p><p>b</p><br><br><br><br>c<ul><li>d</li></ul>"
    assert html_to_text(src) == "a\n\nb\n\nc\nd"


def test_entities_are_decoded() -> None:
    assert html_to_text("a &amp; b &lt; c &#39;d&#39;") == "a & b < c 'd'"


def test_sound_and_av_tags_removed_in_text() -> None:
    src = "hello [sound:a.mp3][anki:play:q:0] world[anki:tts lang=en_US]"
    assert html_to_text(src) == "hello world"


def test_img_becomes_placeholder_in_text() -> None:
    assert html_to_text("<img src=x.png>") == "[img:x.png]"
    assert html_to_text('<img src="y.png" alt="a">') == "[img:y.png]"


def test_extract_media_sound_img_audio_and_skips_remote_and_data() -> None:
    chunk = (
        '[sound:a.mp3]<img src="b.png"><img src=c.jpg><img src="https://x.example/y.png">'
        '<img src="data:image/png;base64,AAAA"><audio src="d.ogg"></audio>[sound:a.mp3]'
    )
    assert extract_media(chunk) == ["a.mp3", "b.png", "c.jpg", "d.ogg"]


def test_extract_media_unescapes_and_spans_chunks() -> None:
    assert extract_media("[sound:a&amp;b.mp3]", '<img src="p.png">') == ["a&b.mp3", "p.png"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HET HUIS", "het huis"),
        (RAW_FRONT_WITH_SOUND, "het huis"),
        ("  het <i>huis</i> <br> ", "het huis"),
        ("Straße", "strasse"),
        ("a<br>b   c", "a b c"),
        ("<div>x</div>\n<div>y</div>", "x y"),
        ("", ""),
        ("[sound:only.mp3]", ""),
    ],
)
def test_normalize_headword(raw: str, expected: str) -> None:
    assert normalize_headword(raw) == expected
