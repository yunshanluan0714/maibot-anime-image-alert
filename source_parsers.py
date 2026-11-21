"""Reverse-image-search response parsers, URL normalization, localization, and formatting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import html as html_lib
import re
from typing import Any

# Danbooru / SauceNAO copyright tags -> mainland Chinese titles.
COPYRIGHT_ZH: dict[str, str] = {
    "zenless zone zero": "绝区零",
    "genshin impact": "原神",
    "honkai: star rail": "崩坏：星穹铁道",
    "honkai (series)": "崩坏系列",
    "honkai impact 3rd": "崩坏3",
    "wuthering waves": "鸣潮",
    "blue archive": "蔚蓝档案",
    "arknights": "明日方舟",
    "arknights: endfield": "明日方舟：终末地",
    "azur lane": "碧蓝航线",
    "girls' frontline": "少女前线",
    "girls' frontline 2": "少女前线2：追放",
    "fate/grand order": "Fate/Grand Order",
    "fate (series)": "Fate系列",
    "umamusume": "赛马娘",
    "hololive": "Hololive",
    "nijisanji": "彩虹社",
    "vocaloid": "VOCALOID",
    "touhou": "东方Project",
    "goddess of victory: nikke": "胜利女神：妮姬",
    "punishing: gray raven": "战双帕弥什",
    "reverse:1999": "重返未来：1999",
    "honkai: nexus anima": "崩坏：因缘精灵",
    "love live!": "LoveLive!",
    "idolmaster": "偶像大师",
    "the idolmaster": "偶像大师",
    "bocchi the rock!": "孤独摇滚！",
    "sousou no frieren": "葬送的芙莉莲",
    "re:zero kara hajimeru isekai seikatsu": "Re:从零开始的异世界生活",
    "re:zero": "Re:从零开始的异世界生活",
    "chainsaw man": "电锯人",
    "spy x family": "间谍过家家",
    "oshi no ko": "【我推的孩子】",
    "kantai collection": "舰队Collection",
    "pokemon": "宝可梦",
    "original": "原创",
}

_SAUCE_BLOCK = re.compile(r'<td class="resulttablecontent">(.*?)</td>', re.S)
_SAUCE_SIM = re.compile(r'<div class="resultsimilarityinfo">\s*([\d.]+)%\s*</div>')
_TAG = re.compile(r"<[^>]+>")
_PIXIV_ID = re.compile(r"(?:illust_id=|artworks/|pximg\.net/.*/)(\d{5,12})")
_DANBOORU_POST_ID = re.compile(r"danbooru\.donmai\.us/posts?/(?:show/)?(\d+)")
_IQDB_BLOCK = re.compile(r"<table><tr><th>(Best match|Additional match)</th></tr>(.*?)</table>", re.S)
_X_STATUS_HANDLE = re.compile(r"https?://x\.com/([A-Za-z0-9_]{1,30})/status/\d+")
_COSPLAY_TITLE_RE = re.compile(r"^\s*([A-Za-z0-9_\-\u4e00-\u9fff]{2,24})\s+cosplay\s+([A-Za-z0-9_ '\-]{2,32})", re.I)

_URL_PRIORITY = (
    "pixiv.net/artworks",
    "x.com/",
    "skeb.jp",
    "fanbox.cc",
    "fantia.jp",
    "deviantart.com",
    "artstation.com",
    "cosplaytele.com",
    "worldcosplay.net",
    "miyoushe.com",
    "bilibili.com",
    "lofter.com",
    "weibo.com",
)
_IGNORED_URL_MARKERS = ("google.com/goto", "google.com/url", "google.com/search")
_NOISY_COMMERCE_DOMAINS = (
    "pinterest.",
    "amazon.",
    "aliexpress.",
    "ebay.",
    "etsy.",
    "taobao.",
    "walmart.",
    "bing.com/images",
)
_IQDB_NON_CHARACTER_TAGS = {"bangboo", "mascot", "plush", "fumo"}
_TITLE_WORK_HINTS: tuple[tuple[str, str], ...] = (
    ("zenless zone zero", "绝区零"),
    ("绝区零", "绝区零"),
    ("ゼンゼロ", "绝区零"),
    ("angels of delusion", "绝区零"),
    ("妄想天使", "绝区零"),
    ("eldreda", "绝区零"),
    ("sunbringer", "绝区零"),
    ("dracaena", "绝区零"),
    ("zzz", "绝区零"),
    ("genshin impact", "原神"),
    ("原神", "原神"),
    ("honkai: star rail", "崩坏：星穹铁道"),
    ("星穹铁道", "崩坏：星穹铁道"),
    ("wuthering waves", "鸣潮"),
    ("鸣潮", "鸣潮"),
    ("blue archive", "蔚蓝档案"),
    ("蔚蓝档案", "蔚蓝档案"),
    ("arknights", "明日方舟"),
    ("明日方舟", "明日方舟"),
    ("azur lane", "碧蓝航线"),
    ("碧蓝航线", "碧蓝航线"),
    ("fate/grand order", "Fate/Grand Order"),
    ("re:zero", "Re:从零开始的异世界生活"),
    ("re：zero", "Re:从零开始的异世界生活"),
    ("hatsune miku", "VOCALOID"),
    ("vocaloid", "VOCALOID"),
    ("frieren", "葬送的芙莉莲"),
    ("touhou", "东方Project"),
)
_KNOWN_COSPLAY_CHAR_ZH: tuple[tuple[str, str, str], ...] = (
    ("rem", "蕾姆", "Re:从零开始的异世界生活"),
    ("ram", "拉姆", "Re:从零开始的异世界生活"),
    ("emilia", "爱蜜莉雅", "Re:从零开始的异世界生活"),
    ("hatsune miku", "初音未来", "VOCALOID"),
    ("miku", "初音未来", "VOCALOID"),
    ("furina", "芙宁娜", "原神"),
    ("faruzan", "珐露珊", "原神"),
    ("frieren", "芙莉莲", "葬送的芙莉莲"),
    ("kafka", "卡芙卡", "崩坏：星穹铁道"),
    ("firefly", "流萤", "崩坏：星穹铁道"),
    ("ellen", "艾莲·乔", "绝区零"),
    ("miyabi", "星见雅", "绝区零"),
    ("cheshire", "柴郡", "碧蓝航线"),
    ("formidable", "可畏", "碧蓝航线"),
)


@dataclass
class SourceInfo:
    engine: str = ""
    similarity: float = 0.0
    work: str = ""
    characters: list[str] = field(default_factory=list)
    artist: str = ""
    title: str = ""
    url: str = ""
    episode: str = ""
    danbooru_post_id: str = ""

    def found(self) -> bool:
        return bool(self.url or self.work or self.characters or self.artist or self.title)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceInfo":
        names = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in names}
        clean["characters"] = [str(c) for c in clean.get("characters") or []][:6]
        return cls(**clean)


def _plain(fragment: str) -> str:
    text = _TAG.sub(" ", fragment.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n"))
    return html_lib.unescape(re.sub(r"[ \t]+", " ", text)).strip()


def _clean_tag(tag: str) -> str:
    """'aria_(human)_(zenless_zone_zero)' -> 'aria'."""
    text = tag.replace("_", " ").strip()
    return re.sub(r"\s*\([^)]*\)", "", text).strip()


def dedupe(items: list[str], limit: int = 4) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
        if len(out) >= limit:
            break
    return out


def extract_danbooru_post_id(urls: list[str]) -> str:
    for raw in urls:
        url = normalize_url(raw)
        match = _DANBOORU_POST_ID.search(url)
        if match:
            return match.group(1)
    return ""


def localize_work(work: str) -> str:
    key = work.strip().lower().replace("_", " ")
    if not key:
        return ""
    first = key.split(",")[0].strip()
    return COPYRIGHT_ZH.get(first, work.strip())


def normalize_url(url: str) -> str:
    url = html_lib.unescape(str(url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return ""
    if "pixiv.net" in url or "pximg.net" in url:
        match = _PIXIV_ID.search(url)
        if match:
            return f"https://www.pixiv.net/artworks/{match.group(1)}"
    url = url.replace("://twitter.com/", "://x.com/").replace("://mobile.twitter.com/", "://x.com/")
    return re.sub(r"^(https?://x\.com/[^/]+/status/\d+)/(?:photo|video)/\d+.*$", r"\1", url)


def _infer_work_from_text(text: str) -> str:
    low = text.lower()
    for needle, zh in _TITLE_WORK_HINTS:
        if needle == "zzz":
            if re.search(r"\bzzz\b", low):
                return zh
        elif needle in low:
            return zh
    return ""


def _infer_cosplay_character(text: str) -> tuple[str, str]:
    low = text.lower()
    for token, char_zh, work_zh in _KNOWN_COSPLAY_CHAR_ZH:
        if re.search(rf"\b{re.escape(token)}\b", low):
            return char_zh, work_zh
    return "", ""


def pick_best_url(urls: list[str]) -> str:
    normalized = [
        u
        for u in (normalize_url(x) for x in urls)
        if u and not any(bad in u for bad in _IGNORED_URL_MARKERS)
    ]
    for marker in _URL_PRIORITY:
        for url in normalized:
            if marker in url:
                return url
    for url in normalized:
        if "danbooru.donmai.us" in url or "gelbooru.com" in url or "yande.re" in url:
            return url
    return normalized[0] if normalized else ""


def parse_serpapi_lens(data: dict[str, Any]) -> SourceInfo | None:
    """Parse SerpApi google_lens exact_matches response into SourceInfo."""
    if not isinstance(data, dict) or data.get("error"):
        return None
    matches = data.get("exact_matches") or []
    if not isinstance(matches, list) or not matches:
        return None
    candidates: list[tuple[str, dict[str, Any]]] = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        url = normalize_url(str(item.get("link") or ""))
        if not url or any(bad in url for bad in _IGNORED_URL_MARKERS):
            continue
        candidates.append((url, item))
    if not candidates:
        return None
    best_url = pick_best_url([u for u, _ in candidates])
    best_item = next((item for u, item in candidates if u == best_url), candidates[0][1])
    title = str(best_item.get("title") or "").strip()[:100]
    info = SourceInfo(
        engine="google_lens",
        similarity=95.0,
        title=title,
        url=best_url,
        danbooru_post_id=extract_danbooru_post_id([u for u, _ in candidates]),
    )
    all_titles = " ".join(str(item.get("title") or "") for _, item in candidates[:6])
    info.work = _infer_work_from_text(title) or _infer_work_from_text(all_titles)
    match = _X_STATUS_HANDLE.search(best_url)
    if match:
        info.artist = match.group(1)
    return info if info.found() else None


def parse_bing_reverse_image(data: dict[str, Any]) -> SourceInfo | None:
    """Conservatively parse SerpApi bing_reverse_image related_content with consensus gate."""
    if not isinstance(data, dict) or data.get("error"):
        return None
    items = data.get("related_content") or []
    if not isinstance(items, list) or not items:
        return None

    clean_items: list[tuple[str, str]] = []
    url_counts: dict[str, int] = {}
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        url = normalize_url(str(item.get("source") or ""))
        title = str(item.get("title") or "").strip()
        if not url or any(bad in url for bad in _NOISY_COMMERCE_DOMAINS):
            continue
        clean_items.append((url, title))
        url_counts[url] = url_counts.get(url, 0) + 1
    if not clean_items:
        return None

    repeated = [u for u, count in url_counts.items() if count >= 2]
    chosen_url = ""
    chosen_title = ""
    if repeated:
        chosen_url = pick_best_url(repeated) or repeated[0]
        chosen_title = next((t for u, t in clean_items if u == chosen_url and t), "")
    else:
        # Fallback: require at least two hits explicitly agreeing on the same cosplay character.
        char_hits: dict[str, list[tuple[str, str]]] = {}
        for url, title in clean_items[:5]:
            if "cosplay" not in title.lower():
                continue
            char_zh, _ = _infer_cosplay_character(title)
            if char_zh:
                char_hits.setdefault(char_zh, []).append((url, title))
        top_group = next((grp for grp in char_hits.values() if len(grp) >= 2), None)
        if not top_group:
            return None
        chosen_url = pick_best_url([u for u, _ in top_group]) or top_group[0][0]
        chosen_title = next((t for u, t in top_group if u == chosen_url), top_group[0][1])

    work = _infer_work_from_text(chosen_title)
    char_zh, char_work = _infer_cosplay_character(chosen_title)
    work = work or char_work
    artist = ""
    cos_match = _COSPLAY_TITLE_RE.match(chosen_title)
    if cos_match:
        artist = cos_match.group(1)

    info = SourceInfo(
        engine="bing",
        similarity=92.0,
        work=work,
        characters=[char_zh] if char_zh else [],
        artist=artist,
        title=chosen_title[:100],
        url=chosen_url,
    )
    return info if info.found() else None


def parse_saucenao_html(page: str, min_similarity: float) -> SourceInfo | None:
    for block in _SAUCE_BLOCK.findall(page):
        sim_m = _SAUCE_SIM.search(block)
        if not sim_m:
            continue
        similarity = float(sim_m.group(1))
        if similarity < min_similarity:
            continue
        info = SourceInfo(engine="saucenao", similarity=similarity)
        title_m = re.search(r'<div class="resulttitle">(.*?)</div>', block, re.S)
        title_text = _plain(title_m.group(1)) if title_m else ""
        links = re.findall(r'href="([^"]+)"', block)

        columns = re.findall(r'<div class="resultcontentcolumn">(.*?)</div>', block, re.S)
        fields: dict[str, str] = {}
        for column in columns:
            for raw_key, raw_value in re.findall(r"<strong>([^<:]+):\s*</strong>(.*?)(?=<strong>|$)", column, re.S):
                fields[raw_key.strip().lower()] = raw_value

        creator_m = re.match(r"Creator\(?s?\)?:\s*(.+)", title_text, re.S)
        if creator_m:
            info.artist = creator_m.group(1).split("\n")[0].strip()
        elif title_text:
            info.title = title_text.split("\n")[0].strip()[:80]

        for key in ("member", "author", "artist(s)", "creator(s)", "twitter", "author(s)"):
            if not info.artist and key in fields:
                info.artist = _plain(fields[key]).split("\n")[0].strip().lstrip("@")
        if "material" in fields:
            info.work = _plain(fields["material"]).split("\n")[0].strip()
        if "characters" in fields:
            chars = [_clean_tag(c) for c in _plain(fields["characters"]).split("\n")]
            info.characters = dedupe([c for c in chars if c])
        if "est time" in fields or re.search(r"\bEP\b", block):
            ep_m = re.search(r"-\s*(\d+)\s*$", info.title)
            if ep_m:
                info.episode = ep_m.group(1)
        info.url = pick_best_url(links)
        info.danbooru_post_id = extract_danbooru_post_id(links)
        if info.found():
            return info
    return None


def parse_saucenao_json(data: dict[str, Any], min_similarity: float) -> SourceInfo | None:
    for item in data.get("results") or []:
        header = item.get("header") or {}
        body = item.get("data") or {}
        try:
            similarity = float(header.get("similarity") or 0)
        except (TypeError, ValueError):
            continue
        if similarity < min_similarity:
            continue
        info = SourceInfo(engine="saucenao", similarity=similarity)
        creator = (
            body.get("creator")
            or body.get("member_name")
            or body.get("author_name")
            or body.get("twitter_user_handle")
            or ""
        )
        if isinstance(creator, list):
            creator = ", ".join(str(c) for c in creator[:2])
        info.artist = str(creator).strip()
        info.work = str(body.get("material") or "").split(",")[0].strip()
        chars = str(body.get("characters") or "")
        info.characters = dedupe([_clean_tag(c) for c in re.split(r"[,\n]", chars) if c.strip()])
        info.title = str(body.get("title") or body.get("eng_name") or body.get("jp_name") or "").strip()[:80]
        if body.get("part"):
            info.episode = str(body.get("part"))
        urls = list(body.get("ext_urls") or [])
        danbooru_id = str(body.get("danbooru_id") or "").strip()
        if danbooru_id.isdigit():
            urls.append(f"https://danbooru.donmai.us/posts/{danbooru_id}")
        if body.get("pixiv_id"):
            urls.insert(0, f"https://www.pixiv.net/artworks/{body['pixiv_id']}")
        if isinstance(body.get("source"), str) and body["source"].startswith("http"):
            urls.insert(0, body["source"])
        info.url = pick_best_url(urls)
        info.danbooru_post_id = danbooru_id if danbooru_id.isdigit() else extract_danbooru_post_id(urls)
        if info.found():
            return info
    return None


def parse_iqdb_html(
    page: str,
    min_similarity: float,
    known_copyrights: set[str] | None = None,
) -> tuple[SourceInfo | None, str]:
    """Return (info, danbooru_post_id)."""
    known = known_copyrights or set(COPYRIGHT_ZH)
    for _, block in _IQDB_BLOCK.findall(page):
        sim_m = re.search(r"(\d+)% similarity", block)
        if not sim_m or float(sim_m.group(1)) < min_similarity:
            continue
        href_m = re.search(r'<a href="([^"]+)"', block)
        alt_m = re.search(r'alt="([^"]*)"', block)
        url = normalize_url(href_m.group(1)) if href_m else ""
        post_id = extract_danbooru_post_id([url])
        tags_text = ""
        if alt_m:
            tags_m = re.search(r"Tags:\s*(.*)$", html_lib.unescape(alt_m.group(1)))
            tags_text = tags_m.group(1) if tags_m else ""
        tags = tags_text.split()
        works = [t for t in tags if t.replace("_", " ") in known and t != "original"]
        work_tags = {w.replace("_", " ") for w in works}
        chars = []
        for tag in tags:
            match = re.search(r"_\(([^()]+)\)$", tag)
            if match and match.group(1).replace("_", " ") in work_tags:
                name = _clean_tag(tag)
                if name and name.lower() not in _IQDB_NON_CHARACTER_TAGS:
                    chars.append(name)
        info = SourceInfo(
            engine="iqdb",
            similarity=float(sim_m.group(1)),
            work=works[0].replace("_", " ") if works else "",
            characters=dedupe(chars),
            url=url,
            danbooru_post_id=post_id,
        )
        return info, post_id
    return None, ""


def parse_danbooru_post(data: dict[str, Any]) -> dict[str, Any]:
    chars = [_clean_tag(t) for t in str(data.get("tag_string_character") or "").split()]
    works = [t.replace("_", " ") for t in str(data.get("tag_string_copyright") or "").split()]
    artists = [t.replace("_", " ") for t in str(data.get("tag_string_artist") or "").split()]
    source = str(data.get("source") or "")
    if data.get("pixiv_id"):
        source = f"https://www.pixiv.net/artworks/{data['pixiv_id']}"
    work = ""
    for item in works:
        if item in COPYRIGHT_ZH and item != "original":
            work = item
            break
    if not work and works:
        work = works[-1]
    return {
        "characters": dedupe([c for c in chars if c]),
        "work": work,
        "artist": artists[0] if artists else "",
        "source": normalize_url(source),
    }


def parse_tracemoe(data: dict[str, Any], min_similarity: float) -> SourceInfo | None:
    results = data.get("result") or []
    if not results:
        return None
    top = results[0]
    try:
        similarity = float(top.get("similarity") or 0) * 100
    except (TypeError, ValueError):
        return None
    if similarity < min_similarity:
        return None
    anilist = top.get("anilist") or {}
    titles = anilist.get("title") if isinstance(anilist, dict) else {}
    titles = titles if isinstance(titles, dict) else {}
    work = titles.get("chinese") or titles.get("native") or titles.get("romaji") or ""
    seconds = int(float(top.get("from") or 0))
    episode = str(top.get("episode") or "")
    return SourceInfo(
        engine="trace.moe",
        similarity=round(similarity, 1),
        work=str(work),
        episode=f"{episode}|{seconds // 60:02d}:{seconds % 60:02d}" if episode else f"|{seconds // 60:02d}:{seconds % 60:02d}",
        url=f"https://anilist.co/anime/{anilist.get('id')}" if isinstance(anilist, dict) and anilist.get("id") else "",
    )


def format_source_line(info: SourceInfo, include_link: bool = True, zh_characters: list[str] | None = None) -> str:
    if not info.found():
        return ""
    parts: list[str] = []
    work_zh = localize_work(info.work) if info.work else ""
    raw_chars = zh_characters if zh_characters else [c.title() for c in info.characters]
    chars = dedupe(raw_chars, limit=3)
    if info.engine == "trace.moe":
        ep, _, ts = info.episode.partition("|")
        head = f"《{work_zh or info.work}》" if (work_zh or info.work) else "动画截图"
        if ep:
            head += f" 第{ep}集"
        if ts:
            head += f" {ts}"
        parts.append(head)
    else:
        if work_zh and work_zh != "原创":
            head = f"《{work_zh}》"
            if chars:
                head += "、".join(chars[:3])
            parts.append(head)
        elif chars:
            parts.append("、".join(chars[:3]))
        elif info.title and info.engine == "saucenao":
            parts.append(info.title[:40])
        if info.artist:
            parts.append(f"画师 {info.artist[:30]}")
    if include_link and info.url:
        parts.append(info.url)
    if not parts:
        return ""
    return "出处：" + " · ".join(parts)
