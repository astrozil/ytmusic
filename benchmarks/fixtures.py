"""Synthetic SDK/HTTP responses; no network or authentication is needed."""

import hashlib
import json
import threading
import time
from collections import Counter
from contextlib import contextmanager
from functools import partial

import requests


def track(identifier, artist="artist-a"):
    return {
        "videoId": identifier, "title": f"Song {identifier}",
        "artists": [{"id": artist, "name": artist}],
        "album": {"id": "album-a", "name": "Album"},
        "duration": "3:00", "duration_seconds": 180,
        "thumbnails": [{"url": "https://lh3.googleusercontent.com/fixture=w120-h120",
                        "width": 120, "height": 120}],
    }


def chart_html():
    rows = []
    for rank in range(1, 101):
        stats = "".join(
            f'<span class="c-span">{label}</span><ul><li><span class="c-label">{value}</span></li></ul>'
            for label, value in [("LW", rank), ("PEAK", rank), ("WEEKS ON CHART", 7)]
        )
        rows.append(f'''<ul class="o-chart-results-list-row">
          <li><span class="c-label">{rank}</span></li><li></li><li></li>
          <li><ul><li><h3 id="title-of-a-story">Chart song {rank}</h3>
          <span class="c-label">artist-{rank % 10}</span></li><li>{stats}</li></ul></li>
        </ul>''')
    return '<div id="chart-date-picker" data-date="2026-10-03"></div>' + "".join(rows)


class SyntheticUpstream:
    """Count actual simulated attempts inside the real upstream executor."""

    METHODS = frozenset({"get_charts", "search", "get_song", "get_artist",
                         "get_watch_playlist", "get_album", "get_artist_albums", "get_playlist"})

    def __init__(self, delay_ms):
        self.delay_sec = delay_ms / 1000
        self._lock = threading.Lock()
        self._active = 0
        self._peak = 0
        self._calls = Counter()
        self._chart_html = chart_html()
        self._chart_tracks = [track(f"chart-{i:03d}") for i in range(100)]

    def __getattr__(self, method):
        if method not in self.METHODS:
            raise AttributeError(method)
        return partial(self.invoke, method)

    @contextmanager
    def attempt(self, name):
        with self._lock:
            self._calls[name] += 1
            self._active += 1
            self._peak = max(self._peak, self._active)
        try:
            time.sleep(self.delay_sec)
            yield
        finally:
            with self._lock:
                self._active -= 1

    def reset_metrics(self):
        with self._lock:
            if self._active:
                raise RuntimeError("Cannot reset metrics while upstream work is running")
            self._calls.clear()
            self._peak = 0

    def snapshot(self):
        with self._lock:
            return {"calls": dict(sorted(self._calls.items())),
                    "total_calls": sum(self._calls.values()),
                    "peak_active": self._peak, "active": self._active}

    def invoke(self, method, *args, **kwargs):
        with self.attempt(f"ytmusic.{method}"):
            identifier = str(args[0]) if args else ""
            if method == "get_charts":
                return {"videos": {"items": self._chart_tracks}}
            if method == "search":
                if kwargs.get("filter") == "artists":
                    return [{"browseId": identifier, "artist": identifier}]
                key = hashlib.sha256(identifier.encode()).hexdigest()[:11]
                return [track(key)]
            if method == "get_song":
                item = track(identifier)
                return {"videoDetails": {
                    "videoId": identifier, "title": item["title"], "author": "artist-a",
                    "channelId": "artist-a", "lengthSeconds": "180", "isLive": False,
                    "thumbnail": {"thumbnails": item["thumbnails"]},
                }, "microformat": {"microformatDataRenderer": {"category": "Music"}}}
            if method == "get_artist":
                return {"name": identifier, "thumbnails": track(identifier)["thumbnails"],
                        "songs": {"results": [track(f"{identifier}-{i:03d}", identifier)
                                               for i in range(100)]},
                        "sections": [{"title": "Albums", "items": [
                            {"browseId": f"{identifier}-album-{i}"} for i in range(3)]}]}
            if method in {"get_watch_playlist", "get_playlist"}:
                return {"tracks": [track(f"{identifier}-{i:03d}") for i in range(60)]}
            if method == "get_album":
                return {"title": identifier, "year": "2026", "artists": [{"name": "artist-a"}],
                        "thumbnails": track(identifier)["thumbnails"],
                        "tracks": [track(f"{identifier}-{i:03d}") for i in range(15)]}
            if method == "get_artist_albums":
                return [{"browseId": f"{identifier}-album-{i}"} for i in range(3)]
            raise ValueError(f"Unsupported synthetic SDK method: {method}")

    def http_get(self, url, **kwargs):
        if url.startswith("https://www.billboard.com/charts/"):
            name, body, content_type = "http.billboard", self._chart_html, "text/html"
        elif url == "https://lrclib.net/api/get":
            name, body, content_type = "http.lrclib", json.dumps({
                "plainLyrics": "Synthetic benchmark lyrics\n" * 40,
                "syncedLyrics": "[00:01.00] Synthetic benchmark lyrics\n" * 40,
            }), "application/json"
        else:
            raise ValueError("Unexpected HTTP URL in offline benchmark")
        with self.attempt(name):
            response = requests.Response()
            response.status_code = 200
            response.url = url
            response.encoding = "utf-8"
            response.headers["Content-Type"] = content_type
            response._content = body.encode("utf-8")
            return response
