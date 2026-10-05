#!/usr/bin/env python3
"""Validate iptv-org streams and publish the working ones as JSON and M3U playlists."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

DEFAULT_API_BASE = "https://iptv-org.github.io/api"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
MAX_BODY_BYTES = 256 * 1024
RETRIES = 1

log = logging.getLogger("validate")
_local = threading.local()


# --------------------------------------------------------------------------- #
# HTTP probing
# --------------------------------------------------------------------------- #

def _session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _local.session = session
    return session


def _get(url: str, headers: dict[str, str], timeout: float) -> tuple[requests.Response, bytes]:
    """GET a URL and read at most MAX_BODY_BYTES, bounded by a wall-clock deadline."""
    deadline = time.monotonic() + timeout * 2
    with _session().get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True) as resp:
        if resp.status_code >= 400:
            return resp, b""
        chunks: list[bytes] = []
        size = 0
        for chunk in resp.iter_content(8192):
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_BODY_BYTES or time.monotonic() > deadline:
                break
        return resp, b"".join(chunks)


def _as_playlist(body: bytes) -> str | None:
    text = body.decode("utf-8", "ignore").lstrip("\ufeff \t\r\n")
    return text if text.startswith("#EXTM3U") else None


def _first_variant(playlist: str) -> str | None:
    expect_uri = False
    for raw in playlist.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            expect_uri = True
        elif expect_uri and line and not line.startswith("#"):
            return line
    return None


def _is_hls(url: str, content_type: str, body: bytes) -> bool:
    return (
        ".m3u8" in urlparse(url).path.lower()
        or "mpegurl" in content_type
        or body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U")
    )


def _probe(url: str, headers: dict[str, str], timeout: float) -> str:
    resp, body = _get(url, headers, timeout)
    if resp.status_code >= 400:
        return f"http_{resp.status_code}"

    content_type = resp.headers.get("Content-Type", "").lower()
    if not _is_hls(resp.url, content_type, body):
        if "text/html" in content_type:
            return "html_response"
        return "ok" if body else "empty_response"

    playlist = _as_playlist(body)
    if playlist is None:
        return "invalid_playlist"

    if "#EXT-X-STREAM-INF" not in playlist:
        return "ok" if "#EXTINF" in playlist else "empty_playlist"

    variant = _first_variant(playlist)
    if not variant:
        return "empty_playlist"
    vresp, vbody = _get(urljoin(resp.url, variant), headers, timeout)
    if vresp.status_code >= 400:
        return f"variant_http_{vresp.status_code}"
    return "ok" if _as_playlist(vbody) is not None else "invalid_variant"


def check_stream(stream: dict[str, Any], timeout: float) -> str:
    """Return "ok" if the stream is playable, otherwise a short failure reason."""
    url = (stream.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "unsupported_url"

    headers = {"User-Agent": stream.get("user_agent") or DEFAULT_USER_AGENT}
    if stream.get("referrer"):
        headers["Referer"] = stream["referrer"]

    for attempt in range(RETRIES + 1):
        try:
            return _probe(url, headers, timeout)
        except requests.Timeout:
            return "timeout"
        except requests.exceptions.SSLError:
            return "ssl_error"
        except requests.ConnectionError:
            if attempt == RETRIES:
                return "connection_error"
            time.sleep(1)
        except (requests.RequestException, ValueError, UnicodeError):
            return "request_error"
    return "error"


# --------------------------------------------------------------------------- #
# iptv-org metadata
# --------------------------------------------------------------------------- #

@dataclass
class Catalog:
    channels: dict[str, dict[str, Any]]
    categories: dict[str, str]
    countries: dict[str, str]
    languages: dict[str, str]
    logos: dict[tuple[str, str | None], str]
    feeds: dict[tuple[str, str], dict[str, Any]]
    main_feeds: dict[str, str]

    def logo_for(self, channel: str | None, feed: str | None) -> str | None:
        if not channel:
            return None
        return self.logos.get((channel, feed)) or self.logos.get((channel, None))

    def languages_for(self, channel: str | None, feed: str | None) -> list[str]:
        if not channel:
            return []
        feed = feed or self.main_feeds.get(channel)
        return list(self.feeds.get((channel, feed), {}).get("languages") or [])


def fetch_json(api_base: str, name: str, required: bool = True) -> list[dict[str, Any]]:
    url = f"{api_base}/{name}.json"
    try:
        resp = requests.get(url, timeout=60, headers={"User-Agent": DEFAULT_USER_AGENT})
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        if required:
            raise SystemExit(f"Failed to load {url}: {exc}")
        log.warning("Could not load %s (%s); continuing without it", url, exc)
        return []


def _index_logos(logos: Iterable[dict[str, Any]]) -> dict[tuple[str, str | None], str]:
    best: dict[tuple[str, str | None], dict[str, Any]] = {}
    for logo in logos:
        if not logo.get("channel") or not logo.get("url"):
            continue
        key = (logo["channel"], logo.get("feed"))
        rank = (bool(logo.get("in_use", True)), logo.get("width") or 0)
        current = best.get(key)
        if current is None or rank > (bool(current.get("in_use", True)), current.get("width") or 0):
            best[key] = logo
    return {key: logo["url"] for key, logo in best.items()}


def load_catalog(api_base: str) -> Catalog:
    channels = fetch_json(api_base, "channels", required=False)
    feeds = [f for f in fetch_json(api_base, "feeds", required=False) if f.get("channel") and f.get("id")]
    return Catalog(
        channels={c["id"]: c for c in channels if c.get("id")},
        categories={c["id"]: c["name"] for c in fetch_json(api_base, "categories", required=False)},
        countries={c["code"]: c["name"] for c in fetch_json(api_base, "countries", required=False)},
        languages={c["code"]: c["name"] for c in fetch_json(api_base, "languages", required=False)},
        logos=_index_logos(fetch_json(api_base, "logos", required=False)),
        feeds={(f["channel"], f["id"]): f for f in feeds},
        main_feeds={f["channel"]: f["id"] for f in feeds if f.get("is_main")},
    )


# --------------------------------------------------------------------------- #
# Playlist generation
# --------------------------------------------------------------------------- #

@dataclass
class Entry:
    name: str
    url: str
    tvg_id: str | None
    logo: str | None
    group: str
    country: str | None
    categories: list[str]
    languages: list[str]
    user_agent: str | None
    referrer: str | None


def _quality_value(quality: str | None) -> int:
    digits = "".join(ch for ch in quality or "" if ch.isdigit())
    return int(digits) if digits else 0


def _stream_rank(stream: dict[str, Any], latency_ms: dict[str, int]) -> tuple:
    """Higher is better: unrestricted first, then resolution, then faster response."""
    return (
        not stream.get("labels"),
        _quality_value(stream.get("quality")),
        -latency_ms.get(stream["url"], 10**9),
    )


def pick_best_streams(streams: list[dict[str, Any]], latency_ms: dict[str, int]) -> list[dict[str, Any]]:
    """Keep one stream per channel feed, or per title for streams without a channel."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for stream in streams:
        if stream.get("channel"):
            key = ("feed", f"{stream['channel']}@{stream.get('feed') or ''}")
        else:
            key = ("title", " ".join((stream.get("title") or stream["url"]).lower().split()))
        current = best.get(key)
        if current is None or _stream_rank(stream, latency_ms) > _stream_rank(current, latency_ms):
            best[key] = stream
    return list(best.values())


def build_entries(streams: list[dict[str, Any]], catalog: Catalog, include_nsfw: bool) -> list[Entry]:
    entries = []
    for stream in streams:
        channel_id = stream.get("channel")
        feed = stream.get("feed")
        channel = catalog.channels.get(channel_id, {}) if channel_id else {}
        if channel.get("is_nsfw") and not include_nsfw:
            continue

        name = stream.get("title") or channel.get("name") or stream["url"]
        if stream.get("quality"):
            name += f" ({stream['quality']})"
        name += "".join(f" [{label}]" for label in stream.get("labels") or [])

        categories = channel.get("categories") or []
        group = ";".join(catalog.categories.get(c, c.title()) for c in categories) or "Undefined"

        entries.append(Entry(
            name=" ".join(name.split()),
            url=stream["url"],
            tvg_id=f"{channel_id}@{feed}" if channel_id and feed else channel_id,
            logo=catalog.logo_for(channel_id, feed),
            group=group,
            country=channel.get("country"),
            categories=categories,
            languages=catalog.languages_for(channel_id, feed),
            user_agent=stream.get("user_agent"),
            referrer=stream.get("referrer"),
        ))
    entries.sort(key=lambda e: (e.group.lower(), e.name.lower(), e.url))
    return entries


def _attr(key: str, value: str | None) -> str:
    return f'{key}="{value.replace(chr(34), chr(39))}"' if value else ""


def render_m3u(entries: Iterable[Entry]) -> str:
    lines = ["#EXTM3U"]
    for e in entries:
        attrs = " ".join(filter(None, (
            _attr("tvg-id", e.tvg_id),
            _attr("tvg-logo", e.logo),
            _attr("group-title", e.group),
        )))
        lines.append(f"#EXTINF:-1 {attrs},{e.name}")
        if e.referrer:
            lines.append(f"#EXTVLCOPT:http-referrer={e.referrer}")
        if e.user_agent:
            lines.append(f"#EXTVLCOPT:http-user-agent={e.user_agent}")
        lines.append(e.url)
    return "\n".join(lines) + "\n"


def write_playlists(entries: list[Entry], out_dir: Path) -> dict[str, int]:
    _write_text(out_dir / "playlist.m3u", render_m3u(entries))

    playlists_dir = out_dir / "playlists"
    shutil.rmtree(playlists_dir, ignore_errors=True)

    groups: dict[str, dict[str, list[Entry]]] = {
        "countries": defaultdict(list),
        "categories": defaultdict(list),
        "languages": defaultdict(list),
    }
    for e in entries:
        groups["countries"][(e.country or "undefined").lower()].append(e)
        for category in e.categories or ["undefined"]:
            groups["categories"][category].append(e)
        for language in e.languages or ["undefined"]:
            groups["languages"][language].append(e)

    for kind, playlists in groups.items():
        for key, items in playlists.items():
            _write_text(playlists_dir / kind / f"{key}.m3u", render_m3u(items))

    return {kind: len(playlists) for kind, playlists in groups.items()}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _previous_count(path: Path) -> int:
    try:
        return len(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return 0


def _timed_check(stream: dict[str, Any], timeout: float) -> tuple[str, int]:
    started = time.monotonic()
    reason = check_stream(stream, timeout)
    return reason, round((time.monotonic() - started) * 1000)


def validate_all(
    streams: list[dict[str, Any]], timeout: float, workers: int
) -> tuple[list[dict[str, Any]], Counter, dict[str, int]]:
    valid: list[dict[str, Any]] = []
    reasons: Counter = Counter()
    latency_ms: dict[str, int] = {}
    total = len(streams)
    started = time.monotonic()
    log_every = max(1, total // 40)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_timed_check, s, timeout): s for s in streams}
        for done, future in enumerate(as_completed(futures), 1):
            stream = futures[future]
            try:
                reason, elapsed_ms = future.result()
            except Exception:  # noqa: BLE001 - one bad stream must not abort the run
                log.exception("Unexpected error checking %s", stream.get("url"))
                reason, elapsed_ms = "error", 0
            reasons[reason] += 1
            if reason == "ok":
                valid.append(stream)
                latency_ms[stream["url"]] = elapsed_ms
            if done % log_every == 0 or done == total:
                elapsed = time.monotonic() - started
                log.info("%d/%d checked, %d working (%.0f/s)", done, total, len(valid), done / max(elapsed, 1e-6))

    valid.sort(key=lambda s: ((s.get("title") or "").lower(), s["url"]))
    return valid, reasons, latency_ms


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="iptv-org API base URL")
    parser.add_argument("--timeout", type=float, default=10, help="per-request timeout in seconds")
    parser.add_argument("--workers", type=int, default=100, help="concurrent checks")
    parser.add_argument("--limit", type=int, default=0, help="only check the first N streams (for testing)")
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="where to write results")
    parser.add_argument("--include-nsfw", action="store_true", help="include NSFW channels in playlists")
    parser.add_argument("--keep-duplicates", action="store_true",
                        help="list every working stream in playlists instead of the best one per channel")
    parser.add_argument("--min-ratio", type=float, default=0.5,
                        help="refuse to overwrite results if fewer than this fraction of the previous "
                             "count are working (guards against network outages)")
    parser.add_argument("--force", action="store_true", help="write results even if the --min-ratio guard trips")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()

    log.info("Loading streams from %s", args.api_base)
    raw_streams = fetch_json(args.api_base, "streams")
    seen: set[str] = set()
    streams = []
    for stream in raw_streams:
        url = (stream.get("url") or "").strip()
        if url and url not in seen:
            seen.add(url)
            streams.append(stream)
    if args.limit:
        streams = streams[: args.limit]
    log.info("%d unique streams to check (%d in source)", len(streams), len(raw_streams))

    catalog = load_catalog(args.api_base)
    valid, reasons, latency_ms = validate_all(streams, args.timeout, args.workers)

    out_dir: Path = args.output_dir
    streams_path = out_dir / "validated_streams.json"
    previous = _previous_count(streams_path)
    if not args.limit and not args.force and previous and len(valid) < previous * args.min_ratio:
        log.error("Only %d working streams vs %d last run; not overwriting (use --force)", len(valid), previous)
        return 1

    best = valid if args.keep_duplicates else pick_best_streams(valid, latency_ms)
    entries = build_entries(best, catalog, args.include_nsfw)
    _write_json(streams_path, valid)
    playlist_counts = write_playlists(entries, out_dir)

    stats = {
        "generated_at": started_at.isoformat(timespec="seconds"),
        "duration_seconds": round(time.monotonic() - started),
        "source": f"{args.api_base}/streams.json",
        "total_streams": len(streams),
        "working_streams": len(valid),
        "failed_streams": len(streams) - len(valid),
        "playlist_entries": len(entries),
        "duplicates_removed": len(valid) - len(best),
        "playlists": playlist_counts,
        "failure_reasons": dict(sorted(((k, v) for k, v in reasons.items() if k != "ok"),
                                       key=lambda kv: -kv[1])),
        "quality": dict(Counter(s.get("quality") or "unknown" for s in valid).most_common()),
        "settings": {
            "timeout": args.timeout,
            "workers": args.workers,
            "include_nsfw": args.include_nsfw,
            "keep_duplicates": args.keep_duplicates,
        },
    }
    _write_json(out_dir / "validation_stats.json", stats)

    log.info("Done: %d/%d working, %d playlist entries in %ss",
             len(valid), len(streams), len(entries), stats["duration_seconds"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
