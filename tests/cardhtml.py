# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic card HTML in the shapes Anki really renders. Nobody's personal cards live here."""

CARD_CSS = (
    "<style>.card { font-family: arial; font-size: 20px; text-align: center; "
    "color: black; background-color: white; }</style>"
)

# Basic (and reversed card) front/back after Anki rendering: a [sound:] in the raw field becomes
# an [anki:play:q:0] tag; the answer side repeats the question above <hr id=answer>.
BASIC_Q = CARD_CSS + 'het <b>huis</b>[anki:play:q:0]<br><img src="huis.png">'
BASIC_A = (
    BASIC_Q + "\n\n<hr id=answer>\n\nhouse &amp; <i>home</i>[anki:play:a:0]<div>the house</div>"
)

CLOZE_Q = (
    CARD_CSS + 'The <span class="cloze" data-cloze="capital" data-ordinal="1">[...]</span> of '
    '<span class="cloze-inactive" data-ordinal="2">France</span>[anki:play:q:0]'
)
CLOZE_Q_HINT = (
    'The <span class="cloze" data-cloze="capital" data-ordinal="1">[city]</span> of France'
)
CLOZE_A = (
    'The <span class="cloze" data-ordinal="1">capital</span> of '
    '<span class="cloze-inactive" data-ordinal="2">France</span><br>\nextra'
)

# What a raw note field looks like before rendering.
RAW_FRONT_WITH_SOUND = "het <b>huis</b>[sound:huis.mp3]"
