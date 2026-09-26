# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn Anki's rendered card HTML into something a device can draw without an HTML parser.

Two output modes:

* ``html`` — Anki's HTML minus ``<style>`` blocks, ``<script>``, ``[anki:play:…]`` tags and
  ``[sound:…]`` references, with cloze spans kept (the client still needs an HTML renderer).
* ``text`` — plain text with *minimal* markup: ``**bold**``, ``_italic_``, cloze hidden as
  ``[...]`` (or ``[hint]``) on the question side and ``[answer]`` on the answer side, ``\\n`` for
  line breaks. Everything else is stripped. Entities are decoded.

Media referenced by the card (``[sound:x.mp3]``, ``<img src="x.png">``) is returned separately as
a list so the client can fetch it through ``/media/{filename}``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_AV_TAG_RE = re.compile(r"\[anki:(?:play|tts)[^\]]*\]")
_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
_IMG_RE = re.compile(r"<img\b[^>]*?\bsrc=[\"']?([^\"'>\s]+)[\"']?[^>]*>", re.I)
_AUDIO_SRC_RE = re.compile(r"<(?:audio|source|video)\b[^>]*?\bsrc=[\"']?([^\"'>\s]+)[\"']?", re.I)
_HR_ANSWER_RE = re.compile(r"<hr\s+id=[\"']?answer[\"']?\s*/?>", re.I)
_CLOZE_TAG_RE = re.compile(r"<span\b[^>]*\bclass=[\"']?cloze(?:-inactive)?[\"']?[^>]*>", re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


@dataclass
class Rendered:
    question: str
    answer: str
    media: list[str] = field(default_factory=list)


def split_answer(answer_html: str) -> str:
    """Anki's answer side repeats the question above ``<hr id=answer>``; keep only the answer."""
    parts = _HR_ANSWER_RE.split(answer_html, maxsplit=1)
    return parts[1] if len(parts) == 2 else answer_html


def extract_media(*html_chunks: str) -> list[str]:
    seen: list[str] = []
    for chunk in html_chunks:
        for m in _SOUND_RE.findall(chunk):
            name = html.unescape(m.strip())
            if name and name not in seen:
                seen.append(name)
        for pattern in (_IMG_RE, _AUDIO_SRC_RE):
            for m in pattern.findall(chunk):
                name = html.unescape(m.strip())
                if name.startswith(("http://", "https://", "data:")):
                    continue
                if name and name not in seen:
                    seen.append(name)
    return seen


def clean_html(chunk: str) -> str:
    chunk = _STYLE_RE.sub("", chunk)
    chunk = _SCRIPT_RE.sub("", chunk)
    chunk = _AV_TAG_RE.sub("", chunk)
    chunk = _SOUND_RE.sub("", chunk)
    return chunk.strip()


class _TextExtractor(HTMLParser):
    """HTML → minimal-markup text. Cloze spans become ``[...]`` / ``[answer]`` markers."""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "table"}

    def __init__(self, *, markup: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.markup = markup
        self.parts: list[str] = []
        self._cloze_depth = 0
        self._cloze_buffer: list[str] = []
        self._skip_depth = 0

    def _emit(self, text: str) -> None:
        if self._cloze_depth:
            self._cloze_buffer.append(text)
        else:
            self.parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth or tag in ("style", "script"):
            self._skip_depth += 1
            return
        attr = dict(attrs)
        cls = (attr.get("class") or "").split()
        if tag == "span" and "cloze" in cls:
            self._cloze_depth += 1
            self._cloze_buffer = []
            return
        if self._cloze_depth and tag == "span":
            self._cloze_depth += 1
            return
        if tag in ("b", "strong"):
            self._emit("**" if self.markup else "")
        elif tag in ("i", "em"):
            self._emit("_" if self.markup else "")
        elif tag == "br" or tag in self._BLOCK:
            self._emit("\n")
        elif tag == "img":
            self._emit(f"[img:{attr.get('src', '')}]")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag in ("style", "script"):
                self._skip_depth -= 1
            return
        if tag == "span" and self._cloze_depth:
            self._cloze_depth -= 1
            if self._cloze_depth == 0:
                inner = "".join(self._cloze_buffer).strip()
                # Question side: Anki already renders "[...]" or "[hint]"; keep it.
                # Answer side: the span holds the answer text; bracket it.
                if inner.startswith("[") and inner.endswith("]"):
                    self.parts.append(inner)
                else:
                    self.parts.append(f"[{inner}]")
                self._cloze_buffer = []
            return
        if tag in ("b", "strong"):
            self._emit("**" if self.markup else "")
        elif tag in ("i", "em"):
            self._emit("_" if self.markup else "")
        elif tag in self._BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        # Newlines in HTML source are ordinary whitespace; only <br> and blocks break lines.
        self._emit(data.replace("\r", " ").replace("\n", " "))

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [_WS_RE.sub(" ", ln).strip() for ln in raw.split("\n")]
        joined = "\n".join(lines)
        joined = _NL_RE.sub("\n\n", joined)
        return joined.strip()


def html_to_text(chunk: str, *, markup: bool = True) -> str:
    chunk = _AV_TAG_RE.sub("", chunk)
    chunk = _SOUND_RE.sub("", chunk)
    parser = _TextExtractor(markup=markup)
    parser.feed(chunk)
    parser.close()
    return parser.text()


def render(question_html: str, answer_html: str, mode: str = "text") -> Rendered:
    media = extract_media(question_html, answer_html)
    answer_only = split_answer(answer_html)
    if mode == "html":
        return Rendered(clean_html(question_html), clean_html(answer_only), media)
    if mode != "text":
        raise ValueError(f"unknown render mode {mode!r}")
    return Rendered(
        html_to_text(clean_html(question_html)),
        html_to_text(clean_html(answer_only)),
        media,
    )


def normalize_headword(field_html: str) -> str:
    """Dedupe key: first field with media, markup and whitespace removed, case-folded."""
    text = html_to_text(field_html, markup=False)
    text = _WS_RE.sub(" ", text.replace("\n", " ")).strip()
    return text.casefold()
