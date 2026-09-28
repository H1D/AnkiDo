# Migrating to Ankido

Two starting points: you have code that talks to AnkiConnect, or you have a script that talks
to AnkiWeb's sync protocol directly.

## From AnkiConnect

### Step 0: keep it working

Point the existing tool at `https://<your-host>/api/<profile>` with the token as the API key,
give the token scopes `read,add` (plus `review` if it grades cards), and it works as before. The
shim at `POST /api/{profile}` accepts AnkiConnect v6 requests and answers in AnkiConnect's shape;
the full action list and limits are in [ankiconnect-shim.md](ankiconnect-shim.md). Yomitan and
asbplayer can stay on the shim indefinitely.

Differences you will notice on day one:

- The URL contains the profile name. AnkiConnect served whatever profile the desktop app had
  open; Ankido serves every configured profile at once, each at its own path.
- There is always an API key. `requestPermission` reports `requireApiKey: true`.
- Nothing is running on the desktop, so `gui*` actions are unsupported.
- Adding a note no longer means the note is on AnkiWeb: it means it is in Ankido's collection,
  and it reaches AnkiWeb at the next sync (`after_write` autosync, default 30 s after the last
  write). If your workflow was "add in AnkiConnect, then sync from the desktop app", the
  desktop app now downloads the notes from AnkiWeb at its next sync instead.

### Step 1: move writes to `/v1`

Move the code paths that create or grade things first, because that is where `/v1` gives you
something the shim cannot: idempotency, structured errors, offline timestamps.

| AnkiConnect | `/v1` | What changes |
| --- | --- | --- |
| `addNote`, `addNotes` | `POST /v1/p/{p}/notes` | One request for many notes with per-note results; `client_id` per note makes retries safe; `dedupe` replaces `options.allowDuplicate` (`skip` is the default; `update` has no AnkiConnect equivalent); `deckName`/`modelName` become request-level `deck`/`model` with per-note overrides. `audio`/`picture` keep the same `data`/`path`/`url`/`filename`/`fields` shape. The response tells you the stored media filenames. |
| `canAddNotes` | `POST notes` with `dedupe: skip` | The result `skipped_duplicate` says the note existed, with its id. There is no dry run; if you need one, keep `canAddNotes` on the shim. |
| `answerCards` | `POST /v1/p/{p}/reviews` | `cardId`/`ease` become `card_id`/`ease`; add `client_id` and `answered_at` or `elapsed_s`; get back the new interval and due time per card instead of a boolean, and `stale` when another device already graded it. |
| `storeMediaFile` | attach to the note in `POST notes` | There is no standalone media upload on `/v1`. If you need to store a file that no note references, keep `storeMediaFile` on the shim. |
| `sync` | `POST /v1/p/{p}/sync` | Returns a structured result (`no_changes`, `merged`, `downloaded`) and fails with `sync_required_full` instead of doing a full sync silently. `{"wait": false}` for fire-and-forget. |

### Step 2: move reads

| AnkiConnect | `/v1` | What changes |
| --- | --- | --- |
| `findCards("is:due")` + `cardsInfo` | `GET /v1/p/{p}/queue` | One request. Cards come in Anki's study order for the decks you name, already rendered to text or cleaned HTML, with media as a list of filenames. `limit` and `cursor` replace fetching everything. `counts` replaces separate count queries. |
| `deckNames`, `deckNamesAndIds`, `deckDueTree` | `GET /v1/p/{p}/decks` | Names, ids, parent, and counts in one list. |
| `getNumCardsReviewedByDay`, `getNumCardsReviewedToday` | `GET /v1/p/{p}/stats` | Plus `day_rollover_hour` and `next_day_at`, which AnkiConnect never told you. |
| `retrieveMediaFile` | `GET /v1/p/{p}/media/{filename}` | Bytes with a content type and `Range` support instead of base64 in JSON. |
| `syncStatus` | `GET /v1/p/{p}/sync/status` | Same object. |
| `cardsInfo` for a known card | `GET queue` with `fields=full` | If you need arbitrary cards by id rather than the study queue, keep `cardsInfo` on the shim. |

### What stays on the shim

Since 0.3.0 these have `/v1` counterparts:

| AnkiConnect | `/v1` |
| --- | --- |
| `findNotes` + `notesInfo` | `GET /v1/p/{p}/notes?query=` (`render=html` for stored values) |
| `updateNoteFields`, `addTags`, `removeTags` | `PATCH /v1/p/{p}/notes`, which adds `expected_mod` and media checks |
| `getTags` | `GET /v1/p/{p}/tags` |
| `suspend`, `unsuspend`, `forgetCards`, `setDueDate` | `POST /v1/p/{p}/cards/schedule` |
| `changeDeck` | `POST /v1/p/{p}/cards/move` |
| `createDeck` | `POST /v1/p/{p}/decks` |
| `deleteNotes` | `POST /v1/p/{p}/notes/delete` (scope `delete`) |

`deleteDecks` (admin scope), `cardsInfo` for arbitrary cards, model inspection, and the media
directory actions have no `/v1` counterpart. They keep working on the shim with the same token.

### Error handling

AnkiConnect gives `{"result": null, "error": "text"}` with HTTP 200. `/v1` gives a real status
and `{"error": {"code", "message", "retryable"}}`. Replace string matching with checks on
`code`, and use `retryable` to decide whether to back off. See
[clients.md](clients.md#error-handling).

## From a hand-rolled sync script

Scripts built directly on the sync protocol (or on the `anki` library's `full_upload_or_download`)
usually work like this: open a local copy, add notes, upload the whole collection. It works, and
it is the single most common way people lose reviews.

### Why it breaks phones

AnkiWeb's incremental sync works on a shared sequence number. A full upload replaces the server
collection wholesale and resets that sequence. Every other client of the account then discovers
at its next sync that it can no longer merge and shows the full-sync prompt: upload mine, or
download theirs. Whatever was reviewed on the phone since its last sync is lost if the owner
picks download, and your uploaded notes are lost if they pick upload. Doing this nightly from a
cron job means the phone shows that prompt every morning, and eventually someone picks wrong.

The second problem is the schema: whichever library version the script uses opens and possibly
upgrades the collection, with the consequences described in [schema-upgrade.md](schema-upgrade.md).

### The Ankido path

Ankido keeps a local collection per profile and syncs it incrementally, the way the desktop app
does. Your script stops talking to AnkiWeb and talks to Ankido:

1. `POST /v1/p/{profile}/notes` with the notes and a `client_id` per note. The response says
   which were added, which already existed, and where media ended up.
2. Nothing else. With `autosync: after_write`, Ankido merges the notes into AnkiWeb a few
   seconds later, incrementally. Phones see new notes at their next sync with no prompt.

If you want the script to wait until the notes are on AnkiWeb, call `POST /v1/p/{profile}/sync`
with a token that has the `sync` scope and check for `outcome: merged`. If AnkiWeb ever requires a
full sync, that call returns `409 sync_required_full` instead of doing it, and an admin decides
([schema-upgrade.md](schema-upgrade.md#full-sync-policy)).

What to do with the old script's local collection: nothing. Ankido creates its own from AnkiWeb
on first sync. Do not point Ankido's `collection` at a file another program still opens.

### Mapping

| Old script step | Ankido |
| --- | --- |
| Log in to AnkiWeb with username and password | Credentials in the profile's `credentials` file; Ankido caches the sync key in `sync.key`. |
| Open the local `.anki2` with the `anki` library | Ankido's profile worker; one writer, schema gate. |
| `col.add_note(...)` in a loop | `POST notes`, up to 500 per request. |
| `col.media.add_file(...)` and edit the field | `audio`/`picture` on the note; the field reference is written for you. |
| `full_upload_or_download(upload=True)` | Never. Incremental `POST sync` or autosync. |
| Search for existing notes to avoid duplicates | `dedupe: skip` (default) or `update`. |
