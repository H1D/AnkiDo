# AnkiConnect shim: `POST /api/{profile}`

Ankido answers AnkiConnect v6 requests at `POST /api/{profile}` so that tools built for
AnkiConnect (Yomitan, asbplayer, home-grown scripts, AnkiConnect-oriented MCP clients) work
without a rewrite. It is a thin translation onto the same operations `/v1` uses, not a second
engine: `addNote` runs the same code as `POST /v1/p/{profile}/notes`, `answerCards` the same as
`POST /v1/p/{profile}/reviews`.

It is a compatibility surface. It gets bug fixes, not new features. New clients should use `/v1`
([api.md](api.md)).

## Request and response

```json
{"action": "deckNames", "version": 6, "key": "akd_...", "params": {}}
```

```json
{"result": ["Default", "Dutch", "Dutch::Common"], "error": null}
```

- `action`: one of the actions below.
- `version`: `6`. With a lower version the bare `result` is returned without the wrapper, as
  AnkiConnect does.
- `key`: the token, if not sent as a header (see Authentication).
- `params`: action parameters, as AnkiConnect defines them.

On failure the HTTP status is still 200 and the body is `{"result": null, "error": "<message>"}`.
The only exceptions are a body that is not a JSON object (400, same shape). Errors that `/v1`
would report as structured codes become plain strings here: for example a missing scope is
`"token lacks scope 'add' on profile 'alice'"`, an unknown action is
`"unsupported action: foo"`, a duplicate note is
`"cannot create note because it is a duplicate"`.

## Authentication

Two ways, checked in this order:

1. `Authorization: Bearer <token>` header. Preferred.
2. `"key": "<token>"` in the request body. This is how AnkiConnect's `apiKey` works and what
   Yomitan and asbplayer can send. Each use is logged at INFO level as `legacy key auth used`
   with the token id.

The token is an ordinary Ankido token, bound to the profile in the URL. Requests for another
profile fail. Rate limits apply per action class (`read`, `write`, `sync`) exactly as on `/v1`.
`requestPermission` always answers `{"permission": "granted", "requireApiKey": true, "version": 6}`.

## Pointing Yomitan or asbplayer at it

- URL: `http://host:8765/api/<profile>` (or your proxy's `https://` URL plus `/api/<profile>`).
  Use the profile name, not `/api` alone.
- API key field: the token.
- Both tools call `version`, `requestPermission`, `deckNames`, `modelNames`, `modelFieldNames`,
  `canAddNotes`, `addNote`, `storeMediaFile`, `findNotes`, `notesInfo`, `updateNoteFields` and
  `guiBrowse`. Everything except `guiBrowse` is supported; `guiBrowse` returns the error string
  `unsupported action: guiBrowse`, which those tools tolerate. Give the token scopes `read,add`.
- Browser extensions send a CORS preflight. Set `server.cors_origins` to the extension's origin
  (for Yomitan it is shown in the extension's settings), otherwise the browser blocks the call
  before it reaches Ankido.

## Supported actions

The scope column is what the token must have; `admin` covers everything. Actions marked
"write" also arm the `after_write` autosync.

| Action | Scope | Notes |
| --- | --- | --- |
| `version` | read | Returns `6`. |
| `requestPermission` | read | Always granted with `requireApiKey: true`. |
| `apiReflect` | read | `{"scopes": ["actions"], "actions": [...]}`; filters by `params.actions` if given. |
| `getProfiles` | read | `[<profile>]`: one profile per URL. |
| `getActiveProfile` | read | The profile in the URL. |
| `loadProfile` | read | `true` if `params.name` is the profile in the URL, else `false`. Nothing is loaded. |
| `deckNames` | read | Omits the empty `Default` deck. |
| `deckNamesAndIds` | read | |
| `createDeck` | add (write) | Returns the deck id. |
| `deleteDecks` | admin (write) | `params.decks`: names. Cards inside are deleted, as in Anki. `cardsToo` is ignored. |
| `getDeckConfig` | read | Deck options as a dict. |
| `getDecks` | read | `params.cards` -> `{deckName: [cardIds]}`. Unknown ids are skipped. |
| `changeDeck` | add (write) | Moves `params.cards` to `params.deck`, creating it if needed. |
| `modelNames` | read | |
| `modelNamesAndIds` | read | |
| `modelFieldNames` | read | `params.modelName`. |
| `modelStyling` | read | `{"css": ...}`. |
| `modelTemplates` | read | `{templateName: {"Front": qfmt, "Back": afmt}}`. |
| `canAddNotes` | read | One boolean per note: model exists, first field non-empty, and not a duplicate unless `options.allowDuplicate`. |
| `addNote` | add (write) | Returns the note id. `audio`/`picture` accept `data`, `path`, `url`, `filename`, `fields` with the same allowlists as `/v1`. `options.allowDuplicate` maps to `dedupe: allow`; otherwise a duplicate is the error `cannot create note because it is a duplicate`. `options.duplicateScope` is ignored. |
| `addNotes` | add (write) | List of ids; `null` where a note failed. |
| `findNotes` | read | `params.query` in Anki search syntax. |
| `notesInfo` | read | `noteId`, `modelName`, `tags`, `fields` (`{name: {value, order}}`), `cards`, `mod`. `{}` for unknown ids. |
| `updateNoteFields` | add (write) | `params.note = {id, fields}`. Audio/picture on update are not supported. |
| `deleteNotes` | admin (write) | |
| `addTags`, `removeTags` | add (write) | `params.notes`, `params.tags` (space-separated string). |
| `getTags` | read | |
| `findCards` | read | |
| `cardsInfo` | read | AnkiConnect's shape including raw `question`/`answer` HTML, `css`, `nextReviews`. Returns everything at once; there is no paging. `{}` for unknown ids. |
| `cardsModTime` | read | `[{cardId, mod}]`. |
| `cardsToNotes` | read | |
| `suspend`, `unsuspend` | review (write) | Return `true`. |
| `areSuspended` | read | `null` for unknown ids. |
| `areDue` | read | |
| `getIntervals` | read | Current interval per card, or the full revlog history with `complete: true`. |
| `setDueDate` | review (write) | `params.days` as Anki's string syntax (`"0"`, `"1-3"`, `"5!"`). |
| `forgetCards` | review (write) | Reschedules as new. |
| `answerCards` | review (write) | `params.answers = [{cardId, ease, timeMs?}]` -> list of booleans (`true` if applied). Timestamped at receipt; no `client_id`; see limitations. |
| `getNumCardsReviewedToday` | read | |
| `getNumCardsReviewedByDay` | read | `[["YYYY-MM-DD", count], ...]`, newest first, up to ten years. |
| `deckDueTree` | read | Ankido-specific: flat list `{id, name, level, new, learning, due, total, filtered}` in tree order. |
| `getMediaDirPath` | read | Server-side path; only useful if you share the volume. |
| `storeMediaFile` | add (write) | `filename` plus one of `data`, `path`, `url`; same allowlists and size limit as note attachments. Returns the stored name. |
| `retrieveMediaFile` | read | Base64, or `false` if missing. Directory components in `filename` are stripped. |
| `deleteMediaFile` | admin (write) | Moves the file to Anki's media trash. |
| `getMediaFilesNames` | read | `params.pattern` glob, default `*`. |
| `sync` | sync | Incremental sync; returns `null`. Fails with the `sync_required_full` message if AnkiWeb wants a full sync. |
| `syncStatus` | read | Same object as `GET /v1/p/{profile}/sync/status`. |
| `multi` | per sub-action | `params.actions = [{action, params}]`; each entry is checked for its own scope and returns `{result, error}`. A failing sub-action does not stop the others. |

Anything else, including every `gui*` action, `exportPackage`, `importPackage`,
`createModel`, `updateModelTemplates`, `getEaseFactors`, `setEaseFactors`, `relearnCards`,
`getReviewsOfCards` and `insertReviews`, returns `unsupported action: <name>`.

## Why the shim is not the primary surface

These limitations are inherent to the AnkiConnect dialect and are not going to be fixed here:

- No idempotency. Re-delivering `answerCards` after a lost response grades the card twice (the
  second grade is only refused if Anki already has a newer review of that card). Re-delivering
  `addNote` is caught only by first-field deduplication.
- Errors are bare strings with no code and no `retryable` flag, always with HTTP 200. Clients
  have to match on message text.
- The token can travel in the request body (`key`), which ends up in more places (proxy logs,
  browser dev tools, crash reports) than a header does.
- No `ETag` or `304`, no cursor, no `Range`: `cardsInfo` and `findCards` return everything at
  once.
- No cleanup of card HTML. `cardsInfo` returns Anki's rendered HTML including `<style>` blocks
  and `[anki:play:...]` markers; the client needs its own cleanup layer. `/v1` `queue` does this
  server-side.
- Offline replay is impossible: `answerCards` has no timestamp field, so reviews are dated at
  receipt.

When you write a new client, target `/v1`. [migration.md](migration.md) maps the common actions.
