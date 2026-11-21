"""Unit tests for reverse image search parsers, rate limits, caching and formatting."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import source_lookup as sl


SAUCE_HTML_X = """
<div class="result"><table><tr><td class="resulttablecontent">
<div class="resultmatchinfo"><div class="resultsimilarityinfo">93.14%</div>
<div class="resultmiscinfo"><a href="https://danbooru.donmai.us/post/show/12567807">db</a></div></div>
<div class="resultcontent"><div class="resulttitle"><strong>Creator: </strong>maeru<br /></div>
<div class="resultcontentcolumn"><strong>Source: </strong><a href="https://twitter.com/MaeruMui/status/2122643201291083831">x.com</a><br />
<strong>Material: </strong>zenless zone zero<br /></div>
<div class="resultcontentcolumn"><strong>Characters: </strong><br />dracaene sunbringer<br />sunbringer (original) (zenless zone zero)<br /></div></div>
</td></tr></table></div>
"""

SAUCE_HTML_PIXIV = """
<div class="result"><table><tr><td class="resulttablecontent">
<div class="resultmatchinfo"><div class="resultsimilarityinfo">93.61%</div>
<div class="resultmiscinfo"><a href="https://danbooru.donmai.us/post/show/12583476">db</a></div></div>
<div class="resultcontent"><div class="resulttitle"><strong>Creator: </strong>ohichi16<br /></div>
<div class="resultcontentcolumn"><strong>Source: </strong><a href="https://www.pixiv.net/member_illust.php?mode=medium&amp;illust_id=152247186">pixiv #152247186</a><br />
<strong>Material: </strong>zenless zone zero<br /></div>
<div class="resultcontentcolumn"><strong>Characters: </strong><br />aria (human) (zenless zone zero)<br />aria (zenless zone zero)<br />nangong yu<br />sunna (zenless zone zero)<br /></div></div>
</td></tr></table></div>
"""

SAUCE_HTML_LOW = SAUCE_HTML_PIXIV.replace("93.61%", "55.00%")

IQDB_HTML_BEST = """
<div class='pages'>
<div><table><tr><th>Best match</th></tr><tr><td class='image'><a href="//danbooru.donmai.us/posts/12567807">
<img src='/danbooru/x.jpg' alt="Rating: s Score: 1 Tags: 1girl bangboo_(zenless_zone_zero) sunbringer_(original)_(zenless_zone_zero) zenless_zone_zero">
</a></td></tr><tr><td>96% similarity</td></tr></table></div>
</div>
"""


class ParserTests(unittest.TestCase):
    def test_parse_saucenao_html_x_pixiv_and_threshold(self):
        hit_x = sl.parse_saucenao_html(SAUCE_HTML_X, 80.0)
        self.assertIsNotNone(hit_x)
        self.assertEqual(hit_x.artist, "maeru")
        self.assertEqual(hit_x.work, "zenless zone zero")
        self.assertEqual(hit_x.characters, ["dracaene sunbringer", "sunbringer"])
        self.assertEqual(hit_x.url, "https://x.com/MaeruMui/status/2122643201291083831")
        self.assertEqual(hit_x.danbooru_post_id, "12567807")

        hit_p = sl.parse_saucenao_html(SAUCE_HTML_PIXIV, 80.0)
        self.assertIsNotNone(hit_p)
        self.assertEqual(hit_p.artist, "ohichi16")
        self.assertEqual(hit_p.url, "https://www.pixiv.net/artworks/152247186")
        self.assertEqual(hit_p.danbooru_post_id, "12583476")
        self.assertEqual(hit_p.characters, ["aria", "nangong yu", "sunna"])

        self.assertIsNone(sl.parse_saucenao_html(SAUCE_HTML_LOW, 80.0))

    def test_parse_saucenao_json(self):
        payload = {
            "results": [
                {"header": {"similarity": "61.2"}, "data": {"pixiv_id": 99999}},
                {
                    "header": {"similarity": "91.5"},
                    "data": {
                        "creator": ["ohichi16"],
                        "material": "zenless zone zero, extra series",
                        "characters": "aria (zenless zone zero), nangong yu",
                        "pixiv_id": 152247186,
                        "danbooru_id": 12583476,
                        "ext_urls": ["https://danbooru.donmai.us/posts/12583476"],
                    },
                },
            ]
        }
        hit = sl.parse_saucenao_json(payload, 80.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.similarity, 91.5)
        self.assertEqual(hit.artist, "ohichi16")
        self.assertEqual(hit.work, "zenless zone zero")
        self.assertEqual(hit.characters, ["aria", "nangong yu"])
        self.assertEqual(hit.url, "https://www.pixiv.net/artworks/152247186")
        self.assertEqual(hit.danbooru_post_id, "12583476")

    def test_parse_iqdb_and_danbooru_post(self):
        info, post_id = sl.parse_iqdb_html(IQDB_HTML_BEST, 85.0)
        self.assertIsNotNone(info)
        self.assertEqual(post_id, "12567807")
        self.assertEqual(info.work, "zenless zone zero")
        self.assertNotIn("bangboo", info.characters)
        self.assertEqual(info.characters, ["sunbringer"])

        self.assertIs(sl._IPv4HTTPConnection("example.com")._create_connection, sl._create_connection_ipv4_first)
        self.assertIs(sl._IPv4HTTPSConnection("example.com")._create_connection, sl._create_connection_ipv4_first)

        self.assertEqual(sl.parse_iqdb_html(IQDB_HTML_BEST, 98.0), (None, ""))

        extra = sl.parse_danbooru_post(
            {
                "tag_string_character": "dracaene_sunbringer sunbringer_(original)_(zenless_zone_zero)",
                "tag_string_copyright": "hoyoverse zenless_zone_zero",
                "tag_string_artist": "maeru",
                "source": "https://twitter.com/MaeruMui/status/2122643201291083831",
            }
        )
        self.assertEqual(extra["work"], "zenless zone zero")
        self.assertEqual(extra["artist"], "maeru")
        self.assertEqual(extra["characters"], ["dracaene sunbringer", "sunbringer"])
        self.assertEqual(extra["source"], "https://x.com/MaeruMui/status/2122643201291083831")

    def test_parse_tracemoe_keeps_adult_when_high_similarity(self):
        data = {
            "result": [
                {
                    "similarity": 0.934,
                    "episode": 4,
                    "from": 125.4,
                    "anilist": {
                        "id": 12345,
                        "isAdult": True,
                        "title": {"chinese": "某部动画", "romaji": "Bocchi"},
                    },
                }
            ]
        }
        info = sl.parse_tracemoe(data, 90.0)
        self.assertIsNotNone(info)
        self.assertEqual(info.engine, "trace.moe")
        self.assertEqual(info.work, "某部动画")
        self.assertEqual(info.episode, "4|02:05")
        self.assertEqual(info.url, "https://anilist.co/anime/12345")

    def test_format_source_line_variants_and_dedupe(self):
        info = sl.SourceInfo(
            engine="saucenao",
            similarity=93.1,
            work="zenless zone zero",
            characters=["dracaene sunbringer", "sunbringer"],
            artist="maeru",
            url="https://x.com/MaeruMui/status/2122643201291083831",
        )
        # Dedupe repeated Chinese aliases returned by localization.
        line = sl.format_source_line(info, include_link=True, zh_characters=["孙布林格", "孙布林格"])
        self.assertEqual(line, "出处：《绝区零》孙布林格 · 画师 maeru · https://x.com/MaeruMui/status/2122643201291083831")
        self.assertEqual(
            sl.format_source_line(info, include_link=False, zh_characters=["孙布林格"]),
            "出处：《绝区零》孙布林格 · 画师 maeru",
        )

        original = sl.SourceInfo(engine="saucenao", work="original", artist="画师甲", title="夏夜", url="https://www.pixiv.net/artworks/10001")
        self.assertEqual(sl.format_source_line(original), "出处：夏夜 · 画师 画师甲 · https://www.pixiv.net/artworks/10001")

        anime = sl.SourceInfo(engine="trace.moe", work="孤独摇滚！", episode="8|03:09", url="https://anilist.co/anime/130003")
        self.assertEqual(sl.format_source_line(anime), "出处：《孤独摇滚！》 第8集 03:09 · https://anilist.co/anime/130003")


class FinderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1_000_000.0

    def tearDown(self):
        self.temp.cleanup()

    def _finder(self, http, **kwargs):
        return sl.SourceFinder(
            self.temp.name,
            saucenao_min_interval=0.0,
            saucenao_backoff_seconds=900.0,
            http=http,
            clock=lambda: self.now,
            **kwargs,
        )

    async def test_parallel_merge_positive_cache_and_danbooru_overrides_iqdb_noise(self):
        calls = []

        def fake_http(url, *, data=None, headers=None, timeout=20.0):
            del data, timeout
            calls.append(url)
            if "saucenao.com" in url:
                return b"<html>no match</html>"
            if "iqdb.org" in url:
                return IQDB_HTML_BEST.encode("utf-8")
            if "danbooru.donmai.us/posts/12567807.json" in url:
                self.assertIn("MaiBotAnimeAlert", (headers or {}).get("User-Agent", ""))
                return json.dumps(
                    {
                        "tag_string_character": "dracaene_sunbringer sunbringer_(original)_(zenless_zone_zero)",
                        "tag_string_copyright": "zenless_zone_zero",
                        "tag_string_artist": "maeru",
                        "source": "https://x.com/MaeruMui/status/2122643201291083831",
                    }
                ).encode("utf-8")
            raise AssertionError(f"unexpected url: {url}")

        finder = self._finder(fake_http)
        try:
            info = await finder.find("h1", b"img-bytes", allow_anime_frame=True)
            self.assertIsNotNone(info)
            # Danbooru authoritative character tags replace IQDB's heuristic character list (['sunbringer'] -> full list).
            self.assertEqual(info.characters, ["dracaene sunbringer", "sunbringer"])
            self.assertEqual(info.artist, "maeru")
            self.assertEqual(info.url, "https://x.com/MaeruMui/status/2122643201291083831")
            # trace.moe was NOT called because IQDB+Danbooru succeeded.
            self.assertFalse(any("trace.moe" in u for u in calls))

            # Second query hits SQLite positive cache without any HTTP call.
            call_count_before = len(calls)
            cached = await finder.find("h1", b"img-bytes", allow_anime_frame=True)
            self.assertEqual(len(calls), call_count_before)
            self.assertEqual(cached.url, info.url)
        finally:
            finder.close()

    async def test_tracemoe_fallback_only_for_frame_and_api_key_uses_json(self):
        calls = []

        def fake_http(url, *, data=None, headers=None, timeout=20.0):
            del data, headers, timeout
            calls.append(url)
            if "saucenao.com" in url and "api_key=MYKEY" in url:
                return json.dumps(
                    {
                        "results": [
                            {
                                "header": {"similarity": "94.0"},
                                "data": {"creator": "ohichi16", "material": "zenless zone zero", "pixiv_id": 152247186},
                            }
                        ]
                    }
                ).encode("utf-8")
            if "saucenao.com" in url or "iqdb.org" in url:
                return b"<html>empty</html>"
            if "trace.moe" in url:
                return json.dumps(
                    {"result": [{"similarity": 0.92, "episode": 3, "from": 65, "anilist": {"id": 99, "title": {"chinese": "某动画"}}}]}
                ).encode("utf-8")
            raise AssertionError(url)

        finder = self._finder(fake_http)
        try:
            # Non-frame image does not trigger trace.moe and writes negative cache.
            self.assertIsNone(await finder.find("nonframe", b"raw", allow_anime_frame=False))
            self.assertFalse(any("trace.moe" in u for u in calls))
            # Negative cache prevents duplicate calls for same hash.
            count = len(calls)
            self.assertIsNone(await finder.find("nonframe", b"raw", allow_anime_frame=False))
            self.assertEqual(len(calls), count)

            # Frame image calls trace.moe when SauceNAO + IQDB miss.
            hit = await finder.find("frame1", b"raw", allow_anime_frame=True)
            self.assertIsNotNone(hit)
            self.assertEqual(hit.engine, "trace.moe")
            self.assertEqual(hit.work, "某动画")

            # Setting saucenao_api_key uses output_type=2 JSON endpoint.
            finder.saucenao_api_key = "MYKEY"
            json_hit = await finder.find("art1", b"raw")
            self.assertIsNotNone(json_hit)
            self.assertEqual(json_hit.url, "https://www.pixiv.net/artworks/152247186")
            self.assertTrue(any("api_key=MYKEY" in u for u in calls))
        finally:
            finder.close()

    async def test_429_backoff_never_writes_negative_cache(self):
        state = {"raise_429": True}
        sauce_hits = 0

        def fake_http(url, *, data=None, headers=None, timeout=20.0):
            del data, headers, timeout
            nonlocal sauce_hits
            if "saucenao.com" in url:
                sauce_hits += 1
                if state["raise_429"]:
                    raise sl.RateLimited(url)
                return SAUCE_HTML_PIXIV.encode("utf-8")
            if "iqdb.org" in url:
                return b"<html>no match</html>"
            return b"{}"

        finder = self._finder(fake_http)
        logger = Mock()
        try:
            # First lookup gets 429 -> returns None, must NOT negatively cache!
            self.assertIsNone(await finder.find("img-429", b"raw", logger=logger))
            self.assertEqual(sauce_hits, 1)

            # While still in backoff window, repeated lookup skips HTTP to SauceNAO and still does NOT negatively cache.
            self.now += 60.0
            state["raise_429"] = False
            self.assertIsNone(await finder.find("img-429", b"raw", logger=logger))
            self.assertEqual(sauce_hits, 1)

            # After backoff expires, SauceNAO recovers and resolves the previously transient image.
            self.now += 900.0
            hit = await finder.find("img-429", b"raw", logger=logger)
            self.assertEqual(sauce_hits, 2)
            self.assertIsNotNone(hit)
            self.assertEqual(hit.url, "https://www.pixiv.net/artworks/152247186")
        finally:
            finder.close()

    def test_resolve_image_path(self):
        img = Path(self.temp.name) / "sample.jpg"
        img.write_bytes(b"jpg")
        self.assertEqual(sl.resolve_image_path(str(img)), img)
        self.assertIsNone(sl.resolve_image_path(""))
        self.assertIsNone(sl.resolve_image_path(str(Path(self.temp.name) / "missing.jpg")))

    async def test_saucenao_hit_skips_iqdb_completely(self):
        calls = []

        def fake_http(url, *, data=None, headers=None, timeout=20.0):
            del data, headers, timeout
            calls.append(url)
            if "saucenao.com" in url:
                return SAUCE_HTML_PIXIV.encode("utf-8")
            raise AssertionError(f"should not fall back to {url} when SauceNAO hit")

        finder = self._finder(fake_http)
        try:
            hit = await finder.find("fast-sauce", b"img")
            self.assertIsNotNone(hit)
            self.assertEqual(hit.url, "https://www.pixiv.net/artworks/152247186")
            self.assertEqual(len(calls), 1)
            self.assertIn("saucenao.com", calls[0])
        finally:
            finder.close()

    def test_http_enforces_wall_clock_deadline_on_slow_streaming_chunks(self):
        class SlowStreamResp:
            def __init__(self):
                self.chunks = [b"<script>progress(1)</script>", b"<script>progress(2)</script>", b"</html>"]
                self.idx = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read1(self, n):
                del n
                time.sleep(0.04)
                if self.idx >= len(self.chunks):
                    return b""
                c = self.chunks[self.idx]
                self.idx += 1
                return c

        with patch.object(sl._OPENER, "open", return_value=SlowStreamResp()):
            with self.assertRaises(TimeoutError):
                sl._http("https://iqdb.org/", timeout=0.05)

    def test_parse_serpapi_lens_filters_google_goto_strips_photo_suffix_and_infers_work(self):
        payload = {
            "exact_matches": [
                {
                    "source": "Facebook",
                    "title": "wrapper link",
                    "link": "https://www.google.com/goto?url=CAESiwEB",
                },
                {
                    "source": "X",
                    "title": '141STORE｜ZZZ on X: "ZZZ Repair! Drifting Art Day 4 Official"',
                    "link": "https://x.com/141STORE/status/2123315763444580513/photo/1",
                },
            ]
        }
        info = sl.parse_serpapi_lens(payload)
        self.assertIsNotNone(info)
        self.assertEqual(info.engine, "google_lens")
        self.assertEqual(info.url, "https://x.com/141STORE/status/2123315763444580513")
        self.assertEqual(info.artist, "141STORE")
        self.assertEqual(info.work, "绝区零")

    def test_waterfall_calls_serpapi_when_saucenao_misses_and_skips_iqdb_on_hit(self):
        import asyncio
        calls = []

        def fake_http(url, *, data=None, headers=None, timeout=20.0, proxy_url=""):
            del data, headers, timeout, proxy_url
            calls.append(url)
            if url.startswith("https://saucenao.com"):
                return b"<html>no match</html>"
            if url.startswith("https://uguu.se"):
                return json.dumps({"files": [{"url": "https://n.uguu.se/test.jpg"}]}).encode()
            if url.startswith("https://serpapi.com"):
                return json.dumps({
                    "exact_matches": [{
                        "source": "X",
                        "title": "ZZZ fanart",
                        "link": "https://x.com/artist_a/status/123456789/photo/1",
                    }]
                }).encode()
            raise AssertionError(f"unexpected call: {url}")

        finder = sl.SourceFinder(
            self.temp.name,
            serpapi_api_key="test-key",
            allow_temp_image_upload=True,
            proxy_url="http://127.0.0.1:17890",
            http=fake_http,
            saucenao_min_interval=0.0,
        )
        try:
            hit = asyncio.run(finder.find("lens-hit-1", b"fake-img-bytes"))
            self.assertIsNotNone(hit)
            self.assertEqual(hit.engine, "google_lens")
            self.assertEqual(hit.url, "https://x.com/artist_a/status/123456789")
            self.assertFalse(any("iqdb.org" in c for c in calls))
        finally:
            finder.close()

    def test_waterfall_skips_serpapi_when_saucenao_already_hits(self):
        import asyncio
        calls = []

        def fake_http(url, *, data=None, headers=None, timeout=20.0, proxy_url=""):
            del data, headers, timeout, proxy_url
            calls.append(url)
            if "saucenao.com" in url:
                return SAUCE_HTML_PIXIV.encode()
            raise AssertionError(f"should not call downstream engine: {url}")

        finder = sl.SourceFinder(
            self.temp.name,
            serpapi_api_key="test-key",
            http=fake_http,
            saucenao_min_interval=0.0,
        )
        try:
            hit = asyncio.run(finder.find("sauce-hit-no-lens", b"fake-img-bytes"))
            self.assertIsNotNone(hit)
            self.assertEqual(hit.engine, "saucenao")
            self.assertEqual(len(calls), 1)
        finally:
            finder.close()

    def test_saucenao_uses_proxy_and_iqdb_uses_direct(self):
        import asyncio
        seen_proxies = []

        def fake_http(url, *, data=None, headers=None, timeout=20.0, proxy_url=""):
            del data, headers, timeout
            seen_proxies.append((url, proxy_url))
            if "saucenao.com" in url:
                return b"<html>no match</html>"
            if "iqdb.org" in url:
                return b"<html>no match</html>"
            raise AssertionError(url)

        finder = sl.SourceFinder(
            self.temp.name,
            proxy_url="http://127.0.0.1:17890",
            http=fake_http,
            saucenao_min_interval=0.0,
        )
        try:
            self.assertIsNone(asyncio.run(finder.find("proxy-split", b"raw")))
            self.assertEqual(seen_proxies[0], ("https://saucenao.com/search.php", "http://127.0.0.1:17890"))
            self.assertEqual(seen_proxies[1], ("https://iqdb.org/", ""))
        finally:
            finder.close()

    def test_parse_bing_reverse_image_cosplay_consensus_and_noise_filter(self):
        hit_payload = {
            "related_content": [
                {"title": 'Chunmomo cosplay Rem - Re:Zero "33 photos"', "source": "https://cosplaytele.com/rem-2/"},
                {"title": 'Chunmomo cosplay Rem - Re:Zero "33 photos"', "source": "https://cosplaytele.com/rem-2/"},
                {"title": "Pin on Costume", "source": "https://www.pinterest.com/pin/1/"},
            ]
        }
        info = sl.parse_bing_reverse_image(hit_payload)
        self.assertIsNotNone(info)
        self.assertEqual(info.engine, "bing")
        self.assertEqual(info.work, "Re:从零开始的异世界生活")
        self.assertEqual(info.characters, ["蕾姆"])
        self.assertEqual(info.artist, "Chunmomo")
        self.assertEqual(info.url, "https://cosplaytele.com/rem-2/")

        noisy_payload = {
            "related_content": [
                {"title": "Pin on Costume", "source": "https://www.pinterest.com/pin/1/"},
                {"title": "Amazon Nun Cosplay", "source": "https://www.amazon.com/dp/B001"},
                {"title": "Random unrelated", "source": "https://example.com/a"},
            ]
        }
        self.assertIsNone(sl.parse_bing_reverse_image(noisy_payload))

    def test_cosplay_waterfall_uses_bing_after_lens_miss_and_reuses_upload(self):
        import asyncio
        calls = []
        uguu_attempts = 0

        def fake_http(url, *, data=None, headers=None, timeout=20.0, proxy_url=""):
            del data, headers, timeout, proxy_url
            nonlocal uguu_attempts
            calls.append(url)
            if url.startswith("https://uguu.se"):
                uguu_attempts += 1
                if uguu_attempts == 1:
                    raise OSError("transient EOF")
                return json.dumps({"files": [{"url": "https://n.uguu.se/cos.jpg"}]}).encode()
            if "engine=google_lens" in url:
                return json.dumps({"error": "Google Lens hasn't returned any results for this query."}).encode()
            if "engine=bing_reverse_image" in url:
                return json.dumps({
                    "related_content": [
                        {"title": "Chunmomo cosplay Rem - Re:Zero", "source": "https://cosplaytele.com/rem-2/"},
                        {"title": "Chunmomo cosplay Rem - Re:Zero", "source": "https://cosplaytele.com/rem-2/"},
                    ]
                }).encode()
            raise AssertionError(f"unexpected call: {url}")

        finder = sl.SourceFinder(
            self.temp.name,
            serpapi_api_key="test-key",
            allow_temp_image_upload=True,
            proxy_url="http://127.0.0.1:17890",
            http=fake_http,
            saucenao_min_interval=0.0,
        )
        try:
            hit = asyncio.run(finder.find("cos-rem", b"img", allow_cosplay_search=True))
            self.assertIsNotNone(hit)
            self.assertEqual(hit.engine, "bing")
            self.assertEqual(hit.url, "https://cosplaytele.com/rem-2/")
            self.assertEqual(uguu_attempts, 2)
            # No SauceNAO/IQDB wasted for real cosplay, and uploaded URL was reused across Lens+Bing.
            self.assertFalse(any("saucenao" in u or "iqdb" in u for u in calls))
            self.assertEqual(sum(1 for u in calls if u.startswith("https://uguu.se")), 2)
        finally:
            finder.close()

    def test_serpapi_blocked_when_temp_image_upload_disabled_by_default(self):
        import asyncio
        calls = []

        def fake_http(url, *, data=None, headers=None, timeout=20.0, proxy_url=""):
            del data, headers, timeout, proxy_url
            calls.append(url)
            if "saucenao.com" in url or "iqdb.org" in url:
                return b"<html>no match</html>"
            raise AssertionError(f"temp upload or serpapi must not be called by default: {url}")

        finder = sl.SourceFinder(
            self.temp.name,
            serpapi_api_key="test-key",
            http=fake_http,
            saucenao_min_interval=0.0,
        )
        try:
            self.assertFalse(finder.allow_temp_image_upload)
            self.assertIsNone(asyncio.run(finder.find("privacy-opt-in", b"img")))
            self.assertFalse(any("uguu.se" in u or "serpapi.com" in u for u in calls))
        finally:
            finder.close()


if __name__ == "__main__":
    unittest.main()
