# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ankido — headless, multi-account HTTP API for AnkiWeb collections."""

__version__ = "0.3.0"

# The anki package has a circular import between anki.cards and anki.collection; importing
# anki.collection first is the only import order that works. Do it once, here.
import anki.collection as _anki_collection

del _anki_collection
