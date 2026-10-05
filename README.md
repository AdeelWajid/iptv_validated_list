# IPTV Validated List

A daily-checked list of **working** public IPTV streams, built from the
[iptv-org](https://github.com/iptv-org/iptv) database. Every stream is probed
once a day, and only the ones that respond with a real, playable stream are
published.

## Playlists

Paste any of these URLs into VLC, Kodi, TiviMate, IPTV Smarters, or any other
M3U-compatible player.

| Playlist | URL |
| --- | --- |
| All channels | `https://raw.githubusercontent.com/AdeelWajid/iptv_validated_list/main/playlist.m3u` |
| By country | `https://raw.githubusercontent.com/AdeelWajid/iptv_validated_list/main/playlists/countries/<code>.m3u` |
| By category | `https://raw.githubusercontent.com/AdeelWajid/iptv_validated_list/main/playlists/categories/<category>.m3u` |
| By language | `https://raw.githubusercontent.com/AdeelWajid/iptv_validated_list/main/playlists/languages/<code>.m3u` |

Country codes are lowercase ISO 3166-1 alpha-2, for example `us`, `uk`, `pk`, or `in`.
Categories use iptv-org IDs, for example `news`, `sports`, `movies`, or `kids`.
Languages use ISO 639-3 codes, for example `eng`, `urd`, `hin`, or `ara`.
Streams without a known country, category, or language go into `undefined.m3u`.

Each channel appears once. When several working streams exist for the same
channel feed, the playlists keep the best one: streams without restrictions
like geo-blocking come first, then higher resolution, then faster response.

Each entry includes `tvg-id` (for EPG matching), `tvg-logo`, and `group-title`.
Streams that need a specific user agent or referrer carry `#EXTVLCOPT` lines.

## Data files

- [`validated_streams.json`](validated_streams.json) lists every working stream,
  including duplicates, in the same schema as
  [iptv-org's `streams.json`](https://iptv-org.github.io/api/streams.json).
- [`validation_stats.json`](validation_stats.json) records when the list was
  generated, how many streams passed, and why the rest failed.

## How a stream is validated

1. The stream URL is fetched with `GET` (many HLS servers reject `HEAD`), using
   the stream's own user agent and referrer when iptv-org provides them.
2. HLS playlists must parse as M3U and list media segments. For master
   playlists, the first variant playlist must load as well.
3. Non-HLS streams must return data that is not an HTML page.
4. Timeouts, connection failures, TLS errors, and HTTP 4xx/5xx count as failures.

A stream that passes was reachable from a GitHub Actions runner in the US at
check time. Geo-blocked streams may still fail or work differently where you are.

NSFW channels are excluded from the playlists by default.

## Running locally

```bash
pip install -r requirements.txt
python validate.py                     # full run, writes results to the current directory
python validate.py --limit 200 --output-dir /tmp/test   # quick test
python validate.py --help
```

If far fewer streams pass than last run (default: under 50%), the script
leaves existing results alone and exits with an error, so a network outage
cannot wipe the list. Pass `--force` to override.

## Credits

Stream and channel data come from [iptv-org](https://github.com/iptv-org). This
repository hosts no content; it only checks which publicly listed links work.
