"""Reverse image search orchestrator with SQLite cache and multi-engine waterfall.

Waterfall order (no anti-bot/CAPTCHA bypassing):
  1. SQLite cache (7d positive / 1d negative)
  2. SauceNAO     - Pixiv / X / Danbooru / manga / anime index (routed via optional proxy to avoid shared-IP 429)
  3. Google Lens  - SerpApi exact_matches (when SauceNAO misses & key set;
                    temp JPEG uploaded via uguu.se -> qu.ax -> litterbox fallback over optional proxy)
  4. Bing Visual  - SerpApi bing_reverse_image (conservative consensus fallback for cosplay photos)
  5. IQDB         - Danbooru / Gelbooru / Yande.re / Sankaku booru index (direct connection, fast 10s cap)
  6. Danbooru     - optional enrichment of booru hits (character / copyright / artist / source)
  7. trace.moe    - anime screenshot lookup, only for frame-like descriptions
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable
import urllib.parse

try:
    from .source_http import (
        USER_AGENT,
        RateLimited,
        _EngineSkipped,
        _IPv4HTTPConnection,
        _IPv4HTTPHandler,
        _IPv4HTTPSConnection,
        _IPv4HTTPSHandler,
        _OPENER,
        _build_opener,
        _compress_jpeg,
        _create_connection_ipv4_first,
        _http,
        _multipart,
        _read_with_deadline,
    )
    from .source_parsers import (
        COPYRIGHT_ZH,
        SourceInfo,
        dedupe,
        extract_danbooru_post_id,
        format_source_line,
        localize_work,
        normalize_url,
        parse_bing_reverse_image,
        parse_danbooru_post,
        parse_iqdb_html,
        parse_saucenao_html,
        parse_saucenao_json,
        parse_serpapi_lens,
        parse_tracemoe,
        pick_best_url,
    )
except ImportError:
    from source_http import (
        USER_AGENT,
        RateLimited,
        _EngineSkipped,
        _IPv4HTTPConnection,
        _IPv4HTTPHandler,
        _IPv4HTTPSConnection,
        _IPv4HTTPSHandler,
        _OPENER,
        _build_opener,
        _compress_jpeg,
        _create_connection_ipv4_first,
        _http,
        _multipart,
        _read_with_deadline,
    )
    from source_parsers import (
        COPYRIGHT_ZH,
        SourceInfo,
        dedupe,
        extract_danbooru_post_id,
        format_source_line,
        localize_work,
        normalize_url,
        parse_bing_reverse_image,
        parse_danbooru_post,
        parse_iqdb_html,
        parse_saucenao_html,
        parse_saucenao_json,
        parse_serpapi_lens,
        parse_tracemoe,
        pick_best_url,
    )

CACHE_TTL_SECONDS = 7 * 86400
NEGATIVE_CACHE_TTL_SECONDS = 86400
IQDB_MAX_BYTES = 8 * 1024 * 1024
SEARCH_MAX_BYTES = 15 * 1024 * 1024

__all__ = [
    "CACHE_TTL_SECONDS",
    "COPYRIGHT_ZH",
    "IQDB_MAX_BYTES",
    "NEGATIVE_CACHE_TTL_SECONDS",
    "RateLimited",
    "SEARCH_MAX_BYTES",
    "SourceFinder",
    "SourceInfo",
    "USER_AGENT",
    "_IPv4HTTPConnection",
    "_IPv4HTTPHandler",
    "_IPv4HTTPSConnection",
    "_IPv4HTTPSHandler",
    "_OPENER",
    "_build_opener",
    "_compress_jpeg",
    "_create_connection_ipv4_first",
    "_http",
    "_multipart",
    "_read_with_deadline",
    "dedupe",
    "format_source_line",
    "localize_work",
    "normalize_url",
    "parse_bing_reverse_image",
    "parse_danbooru_post",
    "parse_iqdb_html",
    "parse_saucenao_html",
    "parse_saucenao_json",
    "parse_serpapi_lens",
    "parse_tracemoe",
    "pick_best_url",
    "resolve_image_path",
]


class SourceFinder:
    """Cached, rate-limited reverse image search. All network work runs in threads."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        saucenao_api_key: str = "",
        serpapi_api_key: str = "",
        allow_temp_image_upload: bool = False,
        proxy_url: str = "",
        min_similarity: float = 80.0,
        timeout_seconds: float = 25.0,
        saucenao_min_interval: float = 15.0,
        saucenao_backoff_seconds: float = 900.0,
        http: Callable[..., bytes] = _http,
        clock: Callable[[], float] = time.time,
    ) -> None:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(data_dir) / "sources.sqlite3"), timeout=5)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sources (image_hash TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)"
        )
        self.db.commit()
        self.saucenao_api_key = saucenao_api_key
        self.serpapi_api_key = serpapi_api_key
        self.allow_temp_image_upload = bool(allow_temp_image_upload)
        self.proxy_url = proxy_url
        self.min_similarity = min_similarity
        self.timeout_seconds = timeout_seconds
        self.saucenao_min_interval = saucenao_min_interval
        self.saucenao_backoff_seconds = saucenao_backoff_seconds
        self._http = http
        self._clock = clock
        self._sauce_lock = asyncio.Lock()
        self._sauce_next_at = 0.0
        self._serp_backoff_until = 0.0
        self._inflight: dict[str, asyncio.Task[SourceInfo | None]] = {}

    def close(self) -> None:
        self.db.close()

    # ---- cache

    def _cache_get(self, image_hash: str) -> SourceInfo | None | bool:
        row = self.db.execute("SELECT created, payload FROM sources WHERE image_hash=?", (image_hash,)).fetchone()
        if not row:
            return False
        created, payload = row
        data = json.loads(payload)
        ttl = CACHE_TTL_SECONDS if data else NEGATIVE_CACHE_TTL_SECONDS
        if self._clock() - created > ttl:
            return False
        return SourceInfo.from_dict(data) if data else None

    def _cache_put(self, image_hash: str, info: SourceInfo | None) -> None:
        now = self._clock()
        with self.db:
            self.db.execute("DELETE FROM sources WHERE created<?", (now - CACHE_TTL_SECONDS,))
            self.db.execute(
                "INSERT OR REPLACE INTO sources VALUES (?,?,?)",
                (image_hash, now, json.dumps(info.to_dict() if info else {}, ensure_ascii=False)),
            )

    # ---- engines

    def _call_http(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
        use_proxy: bool = False,
    ) -> bytes:
        kwargs: dict[str, Any] = {"data": data, "headers": headers, "timeout": timeout}
        if use_proxy and self.proxy_url:
            kwargs["proxy_url"] = self.proxy_url
        return self._http(url, **kwargs)

    async def _upload_json_host(self, url: str, jpg: bytes, extra: dict[str, str] | None, timeout: float) -> str:
        body, ctype = _multipart("files[]", jpg, extra=extra)
        payload = await asyncio.to_thread(
            self._call_http,
            url,
            data=body,
            headers={"Content-Type": ctype},
            timeout=timeout,
            use_proxy=True,
        )
        parsed = json.loads(payload.decode("utf-8", "ignore"))
        files = parsed.get("files") or []
        if files and isinstance(files[0], dict):
            hosted = str(files[0].get("url") or "").strip()
            if hosted.startswith(("http://", "https://")):
                return hosted
        raise ValueError(f"invalid temp host json response from {url}")

    async def _upload_temp_image(self, raw: bytes) -> str:
        jpg = await asyncio.to_thread(_compress_jpeg, raw)
        upload_timeout = min(12.0, self.timeout_seconds)
        # Try uguu.se twice (absorbs transient TLS EOF over proxy), then qu.ax (1h expiry), then litterbox (1h expiry).
        for endpoint, extra in (
            ("https://uguu.se/upload", None),
            ("https://uguu.se/upload", None),
            ("https://qu.ax/upload.php", {"expiry": "1h"}),
        ):
            try:
                return await self._upload_json_host(endpoint, jpg, extra, upload_timeout)
            except Exception:
                continue

        body, ctype = _multipart("fileToUpload", jpg, extra={"reqtype": "fileupload", "time": "1h"})
        payload = await asyncio.to_thread(
            self._call_http,
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data=body,
            headers={"Content-Type": ctype},
            timeout=upload_timeout,
            use_proxy=True,
        )
        hosted = payload.decode("utf-8", "ignore").strip()
        if not hosted.startswith(("http://", "https://")):
            raise ValueError("temp upload returned invalid url")
        return hosted

    async def _call_serpapi(self, params: dict[str, str]) -> dict[str, Any]:
        if not self.serpapi_api_key:
            return {}
        if self._clock() < self._serp_backoff_until:
            raise _EngineSkipped("serpapi_backoff")
        query = urllib.parse.urlencode({**params, "api_key": self.serpapi_api_key})
        try:
            payload = await asyncio.to_thread(
                self._call_http,
                f"https://serpapi.com/search.json?{query}",
                timeout=min(20.0, self.timeout_seconds),
                use_proxy=False,
            )
        except RateLimited:
            self._serp_backoff_until = self._clock() + 3600.0
            raise
        data = json.loads(payload.decode("utf-8", "ignore"))
        err = str(data.get("error") or "")
        if "run out of searches" in err.lower() or "quota" in err.lower():
            self._serp_backoff_until = self._clock() + 3600.0
            raise RateLimited("serpapi_quota")
        return data if isinstance(data, dict) else {}

    async def _serpapi_lens(self, img_url: str) -> SourceInfo | None:
        data = await self._call_serpapi(
            {"engine": "google_lens", "type": "exact_matches", "url": img_url, "hl": "zh-CN"}
        )
        return parse_serpapi_lens(data)

    async def _serpapi_bing(self, img_url: str) -> SourceInfo | None:
        data = await self._call_serpapi({"engine": "bing_reverse_image", "image_url": img_url})
        return parse_bing_reverse_image(data)

    async def _saucenao(self, raw: bytes) -> SourceInfo | None:
        async with self._sauce_lock:
            wait = self._sauce_next_at - self._clock()
            if wait > self.saucenao_min_interval:
                raise _EngineSkipped("saucenao_backoff")
            if wait > 0:
                await asyncio.sleep(wait)
            self._sauce_next_at = self._clock() + self.saucenao_min_interval
        try:
            if self.saucenao_api_key:
                query = urllib.parse.urlencode(
                    {"output_type": 2, "numres": 5, "db": 999, "api_key": self.saucenao_api_key}
                )
                body, ctype = _multipart("file", raw)
                payload = await asyncio.to_thread(
                    self._call_http,
                    f"https://saucenao.com/search.php?{query}",
                    data=body,
                    headers={"Content-Type": ctype},
                    timeout=self.timeout_seconds,
                    use_proxy=True,
                )
                return parse_saucenao_json(json.loads(payload.decode("utf-8", "ignore")), self.min_similarity)
            body, ctype = _multipart("file", raw)
            payload = await asyncio.to_thread(
                self._call_http,
                "https://saucenao.com/search.php",
                data=body,
                headers={"Content-Type": ctype},
                timeout=self.timeout_seconds,
                use_proxy=True,
            )
            return parse_saucenao_html(payload.decode("utf-8", "ignore"), self.min_similarity)
        except _EngineSkipped:
            raise
        except RateLimited:
            self._sauce_next_at = self._clock() + self.saucenao_backoff_seconds
            raise

    async def _iqdb(self, raw: bytes) -> tuple[SourceInfo | None, str]:
        if len(raw) > IQDB_MAX_BYTES:
            return None, ""
        body, ctype = _multipart("file", raw)
        payload = await asyncio.to_thread(
            self._call_http,
            "https://iqdb.org/",
            data=body,
            headers={"Content-Type": ctype},
            timeout=min(10.0, self.timeout_seconds),
            use_proxy=False,
        )
        return parse_iqdb_html(payload.decode("utf-8", "ignore"), max(self.min_similarity, 85.0))

    async def _danbooru(self, post_id: str) -> dict[str, Any] | None:
        # Danbooru JSON API blocks generic browser User-Agents with HTTP 403 without cookies.
        payload = await asyncio.to_thread(
            self._call_http,
            f"https://danbooru.donmai.us/posts/{post_id}.json",
            headers={"User-Agent": "MaiBotAnimeAlert/1.3"},
            timeout=min(6.0, self.timeout_seconds),
            use_proxy=True,
        )
        return parse_danbooru_post(json.loads(payload.decode("utf-8", "ignore")))

    async def _tracemoe(self, raw: bytes) -> SourceInfo | None:
        payload = await asyncio.to_thread(
            self._call_http,
            "https://api.trace.moe/search?anilistInfo&cutBorders",
            data=raw,
            headers={"Content-Type": "image/jpeg"},
            timeout=self.timeout_seconds,
        )
        return parse_tracemoe(json.loads(payload.decode("utf-8", "ignore")), 90.0)

    async def _safe(self, coro: Any, logger: Any, name: str) -> tuple[Any, bool]:
        try:
            return await coro, False
        except asyncio.CancelledError:
            raise
        except _EngineSkipped:
            return None, True
        except RateLimited:
            if logger is not None:
                logger.warning("anime-alert source: %s rate_limited", name)
            return None, True
        except Exception as exc:
            if logger is not None:
                logger.warning("anime-alert source: %s %s", name, type(exc).__name__)
            return None, True

    async def _enrich_from_danbooru(
        self,
        info: SourceInfo,
        post_id: str,
        *,
        had_sauce_chars: bool,
        logger: Any,
    ) -> None:
        needs_enrich = (
            not info.characters
            or info.engine == "iqdb"
            or not info.artist
            or "danbooru" in info.url
            or not info.url
        )
        if not post_id or not needs_enrich:
            return
        extra, _ = await self._safe(self._danbooru(post_id), logger, "danbooru")
        if not extra:
            return
        if extra["characters"] and (not info.characters or info.engine == "iqdb" or not had_sauce_chars):
            info.characters = extra["characters"]
        info.work = info.work or extra["work"]
        info.artist = info.artist or extra["artist"]
        if extra["source"] and ("danbooru" in info.url or not info.url):
            info.url = pick_best_url([extra["source"], info.url])

    async def find(
        self,
        image_hash: str,
        raw: bytes,
        *,
        allow_anime_frame: bool = False,
        allow_cosplay_search: bool = False,
        logger: Any = None,
    ) -> SourceInfo | None:
        cached = self._cache_get(image_hash)
        if cached is not False:
            return cached  # type: ignore[return-value]
        if not raw or len(raw) > SEARCH_MAX_BYTES:
            return None

        existing = self._inflight.get(image_hash)
        if existing is not None:
            return await asyncio.shield(existing)

        task = asyncio.create_task(
            self._find_uncached(
                image_hash,
                raw,
                allow_anime_frame=allow_anime_frame,
                allow_cosplay_search=allow_cosplay_search,
                logger=logger,
            )
        )
        self._inflight[image_hash] = task

        def _cleanup(done: asyncio.Task[SourceInfo | None]) -> None:
            if self._inflight.get(image_hash) is done:
                self._inflight.pop(image_hash, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(_cleanup)
        return await asyncio.shield(task)

    async def _find_uncached(
        self,
        image_hash: str,
        raw: bytes,
        *,
        allow_anime_frame: bool = False,
        allow_cosplay_search: bool = False,
        logger: Any = None,
    ) -> SourceInfo | None:
        info: SourceInfo | None = None
        sauce_err = False
        had_sauce_chars = False
        if not allow_cosplay_search:
            info, sauce_err = await self._safe(self._saucenao(raw), logger, "saucenao")
            had_sauce_chars = bool(info is not None and info.characters)

        lens_err = False
        bing_err = False
        if info is None and self.serpapi_api_key and self.allow_temp_image_upload:
            img_url, upload_err = await self._safe(self._upload_temp_image(raw), logger, "temp_upload")
            if img_url:
                info, lens_err = await self._safe(self._serpapi_lens(img_url), logger, "google_lens")
                if info is None and allow_cosplay_search:
                    info, bing_err = await self._safe(self._serpapi_bing(img_url), logger, "bing_reverse")
            else:
                lens_err = upload_err

        iqdb_err = False
        iqdb_post_id = ""
        if info is None and not allow_cosplay_search:
            iqdb_raw, iqdb_err = await self._safe(self._iqdb(raw), logger, "iqdb")
            if isinstance(iqdb_raw, tuple):
                info, iqdb_post_id = iqdb_raw

        if info is not None:
            post_id = iqdb_post_id or info.danbooru_post_id or (
                extract_danbooru_post_id([info.url]) if info.url else ""
            )
            await self._enrich_from_danbooru(info, post_id, had_sauce_chars=had_sauce_chars, logger=logger)

        trace_err = False
        if info is None and allow_anime_frame:
            info, trace_err = await self._safe(self._tracemoe(raw), logger, "trace.moe")

        if info is not None and not info.found():
            info = None
        if info is not None or (not sauce_err and not lens_err and not bing_err and not iqdb_err and not trace_err):
            self._cache_put(image_hash, info)
        return info


def resolve_image_path(full_path: str, data_dir: Path | str | None = None) -> Path | None:
    text = str(full_path or "").strip()
    if not text:
        return None
    p = Path(text)
    if p.is_absolute():
        return p if p.is_file() else None
    cwd = Path.cwd()
    candidates = [cwd / p, cwd / "data" / p.name, cwd / "data" / "images" / p.name]
    if data_dir is not None:
        base = Path(data_dir).resolve()
        for parent in (base, *base.parents):
            candidates.append(parent / p)
            candidates.append(parent / "images" / p.name)
            candidates.append(parent / "data" / "images" / p.name)
            if parent.name == "data" or len(candidates) > 16:
                break
    return next((c for c in candidates if c.is_file()), None)
