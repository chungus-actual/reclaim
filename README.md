# reclaim

See what your Plex library costs on disk — and get the space back.

reclaim reads Plex's index and your watch history and lines them up against bytes on disk:
what takes the space, when anyone last played it, how many people watched it, and who asked
for it. When you've decided, it deletes through Plex (and unmonitors the title in Radarr/Sonarr
so it isn't downloaded again), or asks Radarr/Sonarr to replace a file with a smaller,
lower-quality release.

It runs on your own network, talks only to your own services, and has no accounts or telemetry.

## What's on the page

| View | What it shows |
|---|---|
| Capacity | Free space, plus how the used space splits by "last played" (never, 5+ years, 3–5, 1–3, under a year), files Plex doesn't index, and everything else. A marker shows free space after your drop list. |
| Treemap | Every title sized by disk use, shaded by time since anyone played it. Click for detail. |
| Scatter | Size against years since last play, movies and TV side by side. Drag to select a region. |
| Breakdowns | Space by last played, by number of viewers, and by who requested it (with how much of that the requester never watched). |
| Table | Sortable, filterable list: size, last played, viewers, plays, hours, added, requester, resolution, Radarr/Sonarr status. |
| "Plays by" lens | Count plays from everyone, only you, or any set of people. Every number on the page follows. |
| Detail drawer | Who played it and how much, per-season table for TV, versions for movies, files Plex skipped in the same folder, request info. |
| Drop list | Shortlist titles, seasons or single versions; see the total; delete with a typed confirmation. |
| Downgrade | Search Radarr/Sonarr for a smaller release (e.g. 4K → 1080p, 1080p → 720p), pick one, and let the *arr swap it in. |
| Not in Plex | ISOs, disc rips, interrupted transfers and other files on disk that Plex doesn't index. |
| Downgrades / Deleted | Running and finished downgrades; a permanent record of everything deleted (title, size, files, who watched it, who asked for it). |

## Requirements

- Plex Media Server and the **server owner's** Plex token (watch history and deletes need it)
- Docker with Compose v2.24+ (or Python 3.12)
- Optional: Tautulli · Radarr v4+ and Sonarr v4+ (API v3) · Overseerr or Jellyseerr · Unraid 7 API

## Quick start

```sh
git clone https://github.com/chungus-actual/reclaim.git && cd reclaim
docker compose up -d --build
```

Open `http://<host>:8892`. The first visit opens **Settings**: click **Sign in with Plex**,
pick your server, **Save and build**. The first build of a large library takes about a minute;
after that the page loads from a cached snapshot and rebuilds nightly (and on Refresh).

Every other service is optional and added the same way — fill in the card, press **Test**, save.
Tests talk to the real service and say exactly what's wrong (wrong key, "that's Sonarr, not
Radarr", Tautulli watching a different Plex server, a folder mapping where Plex's files aren't
found…). Nothing is changed on the other side by a test.

To delete from Plex at all, Plex's own **Settings → Library → "Allow media deletion"** must be
on; the Plex test tells you if it isn't.

## Configuration

Everything is set in the app's **Settings** tab and saved to `settings.json` in the data folder
(`/config` in Docker; readable only by its owner). API keys and tokens are never sent back to the
browser, and a login password set there is stored as a salted hash.

If you'd rather configure with environment variables (or a `.env` file — see `.env.example`),
those **override** the app and show as locked in Settings:

| Variable | Needed for | Notes |
|---|---|---|
| `PLEX_URL`, `PLEX_TOKEN` | everything | Owner token. Without a token, only works from an IP in Plex's "allowed without auth" networks. |
| `TAUTULLI_URL`, `TAUTULLI_API_KEY` | richer history | Partial plays, watch time. Without it, Plex's history is used, which only records completed views — more titles will look "never played". |
| `RADARR_URL`, `RADARR_API_KEY`, `SONARR_URL`, `SONARR_API_KEY` | unmonitor on delete, downgrades | One of each. Left blank, they're discovered from Overseerr/Jellyseerr. |
| `SEERR_URL`, `SEERR_API_KEY` | "who requested it" | Overseerr or Jellyseerr. `OVERSEERR_URL`/`OVERSEERR_API_KEY` also work. |
| `UNRAID_URL`, `UNRAID_API_KEY` | free space | Unraid 7 GraphQL, e.g. `http://tower/graphql`. |
| `CAPACITY_PATHS` | free space | Instead of Unraid: comma-separated paths inside the container on your media filesystem(s). |
| `WALK_PATHS` | "Not in Plex" | `plex path=container path`, comma separated, e.g. `/data/movies=/media/movies`. Mount the media read-only. |
| `WALK_AFTER_REFRESH` | | Walk after every rebuild (default on). |
| `DISPLAY_PATHS` | | How paths are shown/copied, e.g. `/data=/mnt/user/media`. |
| `RECLAIM_READ_ONLY` | | `1` disables deletes and downgrades. |
| `RECLAIM_USER`, `RECLAIM_PASSWORD` | | Require HTTP basic auth. Put a TLS proxy in front if it's reachable from outside your LAN. |
| `RECLAIM_REFRESH_HOUR`, `TZ` | | Nightly rebuild hour (default 4) and its timezone. |
| `RECLAIM_DATA` | | Where state lives (default `/config` in Docker). Environment only. |

## How deleting works

Delete goes through Plex (`DELETE /library/metadata/{id}`), which removes the media files from
disk; reclaim then checks the item is really gone from the library. Seasons and single movie
versions can be deleted on their own.

Radarr and Sonarr ship with "Unmonitor deleted movies/episodes" switched **off**, so a file that
disappears outside them leaves the title *missing and monitored* — and the next matching
release on RSS gets downloaded again. With Radarr/Sonarr connected, reclaim unmonitors exactly
the item you deleted (not a global setting), so it stays in the *arr as a record and is never
re-grabbed. Every delete is also written to the Deleted tab with its files, who watched it and
who requested it.

Files in the same folder that Plex doesn't index (an ISO next to the MKV, subtitles) are left
behind; the detail drawer lists them.

**There is no undo.** Nothing deletes without typing `DELETE`, and `RECLAIM_READ_ONLY=1` turns
deletion off entirely.

## How downgrading works

Radarr and Sonarr only ever upgrade. Their quality "rank" is simply a quality's position in the
profile's list, and the import step only rejects a file that ranks *below* the one already
there. So reclaim creates two profiles in each app — `Reclaim ↓1080p` and `Reclaim ↓720p` —
built from the app's `Any` profile with everything above the target (and remux/disc formats)
ranked at the very bottom, only the target tier allowed, and upgrades switched off.

When you pick a release:

1. the title moves to the matching `Reclaim ↓` profile;
2. reclaim grabs your pick through the *arr's normal release API;
3. when the download imports, the *arr treats it as an upgrade and replaces the old file
   (no gap where the title is missing);
4. reclaim notices the new file, asks Plex to rescan that folder, and records the savings.

With upgrades off, the *arr can't grab anything on its own while you wait, and can't drift
back up later. If the download fails or you cancel, the original profile is restored. For a
TV series the profile applies to the whole show, so new episodes also arrive at the lower tier.

The picker only shows the target tier (season packs only for TV), hides extras/specials packs,
blocks releases the *arr itself rejects for real reasons (retention, unparseable, wrong
language, hardcoded subs, already downloading), and flags very low bitrates, multi-audio
releases and AV1 (some Plex clients must transcode it). The pre-selected pick is the *arr's own
top-ranked usable release that saves at least 25%.

## Finding files Plex doesn't index

Mount your media read-only (see `docker-compose.yml`), then in **Settings → Disk walk** map each
Plex library folder to where it appears inside reclaim — the Plex folders are offered as
buttons and **Browse** shows the container's folders. **Test** looks up a sample of Plex's own
files at the mapped location, so a wrong mapping is caught before you save. reclaim walks after
each rebuild and lists everything Plex doesn't know about: disc images and rips, interrupted
transfers (`.partial`, hidden temp files), video it didn't match, and sidecars.

If the media isn't reachable from where reclaim runs (for example a share only one Windows
machine can read), run `tools/remote_walk.py` there instead; it posts the file list to reclaim.

## Running without Docker

```sh
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cd backend && RECLAIM_DATA=../data ../.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8892
```

Then open the page and set it up in Settings.

## What leaves your network

Only calls to plex.tv: **Sign in with Plex** (a PIN, then the list of your servers), and — when
Tautulli isn't configured — one lookup of the server owner's account id so Plex's history and
your request tool agree on who you are. Everything else is between reclaim and your own services.

## Limitations

- Plex only (no Jellyfin/Emby libraries).
- One Radarr and one Sonarr; separate 4K instances aren't handled.
- Downgrade targets are 1080p and 720p; TV downgrades use season packs.
- Interactive searches are as fast as your indexers: seconds for a movie, minutes for a big season.
- No undo for deletes. Keep backups of anything you can't re-acquire.

## Development

```sh
python tests/test_model.py
python tests/test_downgrade.py
python tests/test_settings.py
```

The backend is FastAPI (`backend/main.py`); the page is plain HTML/CSS/JS with a vendored d3
(`backend/static`). State is SQLite plus a gzipped snapshot in `RECLAIM_DATA`.

## License

MIT — see `LICENSE`. Bundles d3 (ISC) — see `THIRD_PARTY_NOTICES`.
