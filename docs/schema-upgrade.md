# Schema upgrades, backups, and the full-sync policy

This page explains the one way a tool built on the Anki library can damage a collection, what
Ankido does about it, and when Ankido will and will not perform a full sync.

## The risk, for a normal Anki user

Your collection is a SQLite file with a schema version number. The Anki program (desktop,
AnkiMobile, AnkiDroid) and the `anki` Python library each know one schema version. When a newer
program opens an older collection, it upgrades the file in place. That is fine on one device.

The problem is what happens next. A schema change cannot be synced incrementally. Once the
upgraded collection reaches AnkiWeb, every other device of that account is told "a full sync is
required" and shown a dialog: upload this device's collection, or download the server's copy.
Whichever side is not chosen is discarded. If a device had reviews or new cards that never
synced, and its owner picks "download", those are gone. If two devices each pick "upload", the
second overwrites the first. Most people have seen this dialog once and clicked something.

Ankido ships the `anki` library at whatever version was current when the image was built. Your
phone or desktop may be older. So the very first time Ankido opens your collection, it could be
the "newer program" that triggers all of this, on a server, without anyone watching.

## What Ankido does

Before opening a collection, Ankido reads its schema version with plain SQLite, without the
library. If the version is lower than the one the bundled library writes (currently 18):

- By default it refuses. Every request that needs the collection fails with
  `409 schema_upgrade_required`, whose `details` carry `current` and `required`. Nothing is
  modified. The log gets the same message.
- If the profile has `allow_schema_upgrade: true`, it first writes a backup named
  `collection-<timestamp>-pre-schema-upgrade.anki2`, then opens the collection, which upgrades
  it. `GET /v1/admin/profiles` shows `schema_upgraded: true` for that process's lifetime and the
  log has a warning with the old and new versions.

`allow_schema_upgrade` is per profile and defaults to `false`. Setting it is a one-time decision;
after the upgrade has happened the flag has no further effect until the library moves again.

What you should do before turning it on:

1. Sync every other device of that account so nothing unsynced is sitting on a phone.
2. Update those devices to a version of Anki that supports the new schema, if you can. AnkiWeb
   accepts collections from the current and several previous versions, but a device that is too
   old will not be able to sync at all after the upgrade.
3. Set `allow_schema_upgrade: true`, restart, make one request that opens the collection (for
   example `GET /v1/p/{profile}/decks`), and check the backup exists.
4. Sync from Ankido. AnkiWeb will require a full sync; see the policy below.

## How other devices react

After the upgraded collection is on AnkiWeb, each other device shows the one-off full sync prompt
the next time it syncs. Choose **download** on each of them, since AnkiWeb now holds the upgraded
collection with all merged reviews. Choose upload only if that device has changes you cannot lose
and you understand the server copy will be replaced. This happens once per device.

## Backups

Ankido writes a backup of the collection file to `<data_dir>/<profile>/backups/`:

- before a schema upgrade (`pre-schema-upgrade`);
- before a forced full sync (`pre-force-upload`, `pre-force-download`), unless the local
  collection has zero notes and zero cards;
- before an automatic full download (`pre-bootstrap`), only if the collection is not completely
  empty; since the automatic download itself only happens into an empty collection, this one is
  rarely written;
- before the first note deletion in an hour (`pre-delete`), through `POST notes/delete`, the
  MCP `delete_notes` tool or the shim's `deleteNotes`;
- on request: `POST /v1/p/{profile}/backup` (admin token) or `ankido backup <profile>` with the
  service stopped (`manual`).

The file is a consistent SQLite copy taken with the collection closed and its WAL checkpointed,
so it opens in Anki desktop as-is (File, Switch Profile, Open Backup does not know about it; copy
it over `collection.anki2` of a spare profile instead, with Anki closed). Media is not included;
it is the `collection.media/` directory next to the collection and is re-fetched from AnkiWeb on
the next media sync.

`backups.keep` (default 10) limits how many are kept per profile; the oldest are deleted after
each new backup, whatever their reason, so a day of hourly `pre-delete` backups rotates out older
ones. Copy a backup you want to keep elsewhere. These backups live on the same disk as the collection. For real safety, copy
`data_dir` elsewhere on a schedule (see [deploy.md](deploy.md#backups-and-retention)); AnkiWeb
holds another copy of everything that has synced.

## Full-sync policy

An incremental sync exchanges only what changed since the last sync. A full sync replaces one
side with the other. AnkiWeb demands a full sync after a schema change, after "Check Database"
repairs, when the two sides have diverged, and sometimes on the very first sync of a brand-new
account that no client has ever synced.

Ankido's rules:

1. **Never an automatic upload.** If AnkiWeb wants a full sync and there is anything in the local
   collection, the sync fails with `409 sync_required_full`. `details.required` is `full_upload`
   when AnkiWeb wants an upload specifically, or `full` when either direction would do.
   `GET /v1/p/{profile}/sync/status` and `GET /v1/admin/profiles` show `full_sync_pending: true`
   until it is resolved. Autosync keeps hitting the same wall and logs it; nothing is lost, the
   local collection just stops syncing until an admin decides.

2. **Automatic download only into an empty collection.** If, at sync time, the local collection
   has zero notes and zero cards (a fresh profile whose file Ankido just created, or one you
   deliberately emptied) and AnkiWeb requires a full sync or a full download, Ankido downloads
   AnkiWeb's copy. There is nothing local to lose. The result has `outcome: downloaded` and
   `local_replaced: true`.

3. **Forced full sync is an explicit admin action.** Either:

   ```sh
   curl -sS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
     https://anki.example.com/v1/p/alice/sync -d '{"force_full": "download", "confirm": "alice"}'
   ```

   or, with the service stopped, `ankido sync alice --force-full download --yes`. Both take a
   backup first (unless the collection is empty), write `force_full_<direction>` audit entries
   with outcomes `requested` and then the result, and perform the full sync whether or not
   AnkiWeb asked for one. `confirm` must equal the profile name. `download` replaces the local
   collection: any local reviews or notes that have not synced are lost, which is why the backup
   exists. `upload` replaces AnkiWeb's copy: every other device then gets the full-sync prompt.

4. After any full sync, Ankido restarts the media sync, since a full sync aborts one in flight.

Which direction to force:

| Situation | Direction |
| --- | --- |
| Brand-new AnkiWeb account, AnkiWeb demands `full_upload`, local is empty or holds what you want on the server | `upload` |
| Ankido upgraded the schema and other devices are current | `upload` (then choose download on each other device) |
| Another device uploaded and you trust its copy; Ankido has no unsynced work worth keeping | `download` |
| Both sides have unsynced work you care about | Neither, yet. Sync the other device first, decide which copy is authoritative, export what the other has (Anki desktop can import a backup as a package), then force. |

## Checklist for a new profile

1. Create the profile in the config, no `allow_schema_upgrade`.
2. First sync: expect `downloaded` (existing account) or `sync_required_full` with
   `full_upload` (new account, then force an upload once).
3. From then on, `merged` or `no_changes`. A `sync_required_full` later means something changed
   on another device that requires a decision; look at `server_message` and the other device.
