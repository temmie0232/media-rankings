"""TOKYOMOTION の一覧（ランキング）を取って、画面向けの形に整える。

保存そのものは `urlfetch.py`（yt-dlp。`POST /api/media/fetch`）の仕事で、ここは
「何が人気か」の一覧を取るだけ。動画も画像も落とさない（サムネイルは URL を返す
だけで、端末が cdn.tokyo-motion.net から直接読む）。

## 作りの方針

- **取得先は固定**。URL は `build_url()` が `https://www.tokyomotion.net/videos?o=..&t=..&page=..`
  として組む。利用者が渡すのは sort / period / page の名前（許可リストで検証）だけで、
  文字列を URL に混ぜない＝SSRF にならない。リダイレクトも同じホスト内だけ追う
- **標準ライブラリ + httpx だけ**（bs4/lxml は入っていない）。ページの形は
  `parse_listing()` の正規表現が決め打ちで読む。サイトの HTML が変わると
  1件も取れなくなるので、その場合は黙って空を返さず `FetchError`（→ 502）にする。
  本当に空のページ（`No Videos Found.`）とはそこで区別する
- **プロセス内 TTL キャッシュ**（キーは sort/period/page、10分）。同じキーが同時に来ても
  取りに行くのは1回（進行中の取得を共有する）。残すのは整形済みの結果だけで、生 HTML は持たない。
  失敗も短く（30秒）覚える。429 / 403 のときに画面の再試行連打でサイトを叩き続けないため
- api は uvicorn 1 プロセス（server/Dockerfile）なので、キャッシュはモジュール変数で足りる
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger("nasapp.tokyomotion")

SITE_HOST = "www.tokyomotion.net"
THUMB_HOST = "cdn.tokyo-motion.net"
SITE_URL = f"https://{SITE_HOST}"

# 実機の Pi から200で取れると確認済みのもの（2026-10-12）。変えると弾かれうる
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
)
_TIMEOUT = 20.0
_MAX_REDIRECTS = 3
# 一覧ページは実測 約40KB。これを大きく超えるなら想定外のページ
_MAX_HTML_BYTES = 3_000_000

# 並び替え（o=）と期間（t=）。web 側の選択肢と同じ名前
SORTS = {
    "popular": "mv",
    "rated": "tr",
    "favorites": "tf",
    "watching": "bw",
    "recent": "mr",
    "longest": "lg",
}
PERIODS = {"today": "t", "week": "w", "month": "m", "all": "a"}
DEFAULT_SORT = "popular"
DEFAULT_PERIOD = "week"
MAX_PAGE = 50

CACHE_TTL = 600.0
FAIL_TTL = 30.0


class ParamError(ValueError):
    """sort / period / page が許可リストに無い。メッセージは利用者に見せてよい。"""


class FetchError(Exception):
    """取得に失敗した・ページの形が想定と違った。メッセージは利用者に見せてよい。"""


# ---------------------------------------------------------------- 入力と URL


def validate_params(sort: str, period: str, page: str | int) -> tuple[str, str, int]:
    """(sort, period, page) を検証して返す。不正なら `ParamError`。"""
    if sort not in SORTS:
        raise ParamError(f"sort は {' / '.join(SORTS)} のどれかにしてください")
    if period not in PERIODS:
        raise ParamError(f"period は {' / '.join(PERIODS)} のどれかにしてください")
    # int() は全角数字なども通すので、ASCII の数字だけを許す
    if isinstance(page, int):
        n = page
    elif re.fullmatch(r"[0-9]{1,4}", page.strip()):
        n = int(page.strip())
    else:
        n = 0
    if not 1 <= n <= MAX_PAGE:
        raise ParamError(f"page は 1〜{MAX_PAGE} の整数にしてください")
    return sort, period, n


def build_url(sort: str, period: str, page: int) -> str:
    """取得先の URL。ホストは固定で、検証済みの名前だけから組む。"""
    return f"{SITE_URL}/videos?o={SORTS[sort]}&t={PERIODS[period]}&page={page}"


def _is_site_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return p.scheme == "https" and p.hostname == SITE_HOST and p.port in (None, 443)
    except ValueError:
        return False


# ---------------------------------------------------------------- パース

# 1件は <a href="/video/{id}/{slug}" class="thumb-popu"> から始まる
_ANCHOR_RE = re.compile(r'<a\b(?=[^>]*\bclass="[^"]*\bthumb-popu\b)[^>]*>', re.I)
_HREF_RE = re.compile(r"""\bhref=["']([^"']*)["']""", re.I)
_VIDEO_PATH_RE = re.compile(r"^(?:https?://[^/]+)?/video/(\d+)(?:[/?#]|$)")
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]*>")
_SHOWING_RE = re.compile(r"Showing\s+([\d,]+)\s+to\s+([\d,]+)\s+of\s+([\d,]+)", re.I)
_ACTIVE_PAGE_RE = re.compile(r"<li\b[^>]*\bactive\b[^>]*>\s*<span>\s*(\d+)\s*</span>", re.I)
_PAGER_RE = re.compile(r"<ul\b[^>]*\bpagination\b[^>]*>(.*?)</ul>", re.I | re.S)
_PAGER_LINK_RE = re.compile(r"""\bhref=["'][^"']*[?&;]page=(\d+)""", re.I)
_DURATION_RE = re.compile(r"^\d{1,3}(?::\d{2}){1,2}$")
# 1件ぶんの HTML の長さの上限（実測 約1.1KB）。最後の1件がページ下部まで食わないための歯止め
_ITEM_MAX_CHARS = 4000


def _text(fragment: str) -> str:
    """タグを外して、実体参照を戻し、空白を1つにする。"""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", fragment))).strip()


def _div_text(block: str, cls: str) -> str | None:
    """`<div class="... {cls} ...">…</div>` の中身の文字列。無ければ None。"""
    m = re.search(
        rf'<div\b[^>]*\bclass="[^"]*(?<![\w-]){cls}(?![\w-])[^"]*"[^>]*>(.*?)</div>',
        block,
        re.I | re.S,
    )
    return _text(m.group(1)) if m else None


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""\b{name}=(?:"([^"]*)"|'([^']*)')""", tag, re.I)
    if not m:
        return None
    return html.unescape(m.group(1) if m.group(1) is not None else m.group(2)).strip()


def _safe_thumbnail(url: str | None) -> str | None:
    """https で cdn.tokyo-motion.net のものだけ通す。それ以外は None。"""
    if not url or re.search(r"[\s\x00-\x1f\\]", url):
        return None
    try:
        p = urlsplit(url)
        ok = (
            p.scheme == "https"
            and p.hostname == THUMB_HOST
            and p.username is None
            and p.password is None
            and p.port in (None, 443)
        )
    except ValueError:
        return None
    return url if ok else None


def _int_or_none(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"\d[\d,]*", s)
    return int(m.group(0).replace(",", "")) if m else None


def _parse_item(anchor: str, block: str) -> dict | None:
    """1件ぶん。`/video/{id}` が読めなければ None（件ごと捨てる）。"""
    href = _HREF_RE.search(anchor)
    m = _VIDEO_PATH_RE.match(html.unescape(href.group(1)).strip()) if href else None
    if not m:
        return None
    vid = m.group(1)

    img_m = _IMG_RE.search(block)
    img = img_m.group(0) if img_m else ""

    title_m = re.search(
        r'<span\b[^>]*\bvideo-title\b[^>]*>(.*?)</span>', block, re.I | re.S
    )
    title = _text(title_m.group(1)) if title_m else ""
    if not title:
        title = _attr(img, "title") or _attr(img, "alt") or ""
    if not title:
        title = f"動画 {vid}"

    duration = _div_text(block, "duration")
    rating = _div_text(block, "video-rating")
    rating_m = re.search(r"(\d{1,3})\s*%", rating or "")

    return {
        "id": vid,
        "title": title,
        # slug は付けない。yt-dlp は /video/{id} で取れる
        "url": f"{SITE_URL}/video/{vid}",
        "thumbnail": _safe_thumbnail(_attr(img, "src") or _attr(img, "data-src")),
        "duration": duration if duration and _DURATION_RE.match(duration) else None,
        "views": _int_or_none(_div_text(block, "video-views")),
        "rating": int(rating_m.group(1)) if rating_m else None,
        "added": _div_text(block, "video-added") or None,
        "hd": re.search(r'\bclass="[^"]*(?<![\w-])hd-text-icon(?![\w-])', block, re.I)
        is not None,
    }


def _showing(text: str) -> tuple[int, int, int] | None:
    """`Showing a to b of N videos.` の (a, b, N)。タグをまたぐので外してから探す。"""
    i = text.find("Showing")
    if i < 0:
        return None
    m = _SHOWING_RE.search(_text(text[i : i + 400]))
    if not m:
        return None
    a, b, n = (int(g.replace(",", "")) for g in m.groups())
    return a, b, n


def parse_listing(text: str, page: int) -> dict:
    """一覧ページの HTML を `{items, page, hasMore}` にする。

    形が想定と違って1件も読めないときは `FetchError`。本当に空のページ
    （`No Videos Found.` や `of 0 videos`）は空の items を返す。
    """
    anchors = list(_ANCHOR_RE.finditer(text))
    showing = _showing(text)
    empty_marker = re.search(r"No Videos Found", text, re.I) is not None
    if not anchors and showing is None and not empty_marker:
        raise FetchError(
            "TOKYOMOTION のページの形が想定と違い、一覧を読めませんでした"
            "（年齢確認などの別ページが返った可能性があります）"
        )

    # 範囲外のページを頼むとサイトは最終ページを返す。今のページ番号が
    # 頼んだものと違うなら、重複を返さず「その先は無い」とする
    active = _ACTIVE_PAGE_RE.search(text)
    if active and int(active.group(1)) != page:
        return {"items": [], "page": page, "hasMore": False}

    pager_at = text.find('<ul class="pagination')
    items: list[dict] = []
    seen: set[str] = set()
    for i, am in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        end = min(end, am.start() + _ITEM_MAX_CHARS)
        if pager_at > am.start():
            end = min(end, pager_at)
        item = _parse_item(am.group(0), text[am.start() : end])
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append(item)

    if not items:
        if not (empty_marker or (showing is not None and showing[2] == 0)):
            raise FetchError(
                "TOKYOMOTION の一覧を読めませんでした（ページの作りが変わった可能性があります）"
            )
        return {"items": [], "page": page, "hasMore": False}

    if showing is not None:
        has_more = showing[1] < showing[2]
    else:
        nums = [
            int(n)
            for ul in _PAGER_RE.findall(text)
            for n in _PAGER_LINK_RE.findall(ul)
        ]
        has_more = any(n > page for n in nums)
    # web 側が頼めるのは MAX_PAGE まで。その先を「ある」と言うと空振りの要求になる
    return {"items": items, "page": page, "hasMore": has_more and page < MAX_PAGE}


# ---------------------------------------------------------------- 取得


def _status_message(code: int) -> str:
    if code == 429:
        return "TOKYOMOTION が短時間のアクセス過多として断りました（HTTP 429）。しばらく待ってから試してください"
    if code == 403:
        return "TOKYOMOTION がアクセスを拒否しました（HTTP 403）。しばらく待ってから試してください"
    return f"TOKYOMOTION から一覧を取得できませんでした（HTTP {code}）"


async def fetch_html(url: str) -> str:
    """固定ホストの URL を取る。リダイレクトは同じホストの https だけ追う。"""
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        ) as client:
            current = url
            for _ in range(_MAX_REDIRECTS + 1):
                resp = await client.get(current)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = resp.headers.get("location")
                nxt = urljoin(current, loc) if loc else ""
                if not _is_site_url(nxt):
                    raise FetchError("TOKYOMOTION が想定外の場所へ転送しました")
                current = nxt
            else:
                raise FetchError("TOKYOMOTION の転送が多すぎます")
    except httpx.TimeoutException as e:
        raise FetchError("TOKYOMOTION の応答が遅く、取得を打ち切りました") from e
    except httpx.HTTPError as e:
        log.warning("TOKYOMOTION への接続に失敗: %s", e)
        raise FetchError("TOKYOMOTION に接続できませんでした") from e

    if resp.status_code != 200:
        raise FetchError(_status_message(resp.status_code))
    if len(resp.content) > _MAX_HTML_BYTES:
        raise FetchError("TOKYOMOTION の応答が大きすぎます")
    return resp.text


_JST = timezone(timedelta(hours=9))


async def fetch(sort: str, period: str, page: int) -> dict:
    """キャッシュを通さず1回取りに行く。引数は `validate_params` を通したもの。"""
    text = await fetch_html(build_url(sort, period, page))
    result = parse_listing(text, page)
    result["fetchedAt"] = datetime.now(_JST).isoformat(timespec="seconds")
    return result


# ---------------------------------------------------------------- キャッシュ


class _Entry(NamedTuple):
    expires: float
    value: dict | None
    error: str | None


_now = time.monotonic
_cache: dict[tuple[str, str, int], _Entry] = {}
_inflight: dict[tuple[str, str, int], asyncio.Task] = {}


def clear_cache() -> None:
    """キャッシュを空にする（テスト用）。"""
    _cache.clear()
    _inflight.clear()


def _store(key: tuple[str, str, int], *, value: dict | None, error: str | None, ttl: float) -> None:
    now = _now()
    for k in [k for k, e in _cache.items() if e.expires <= now]:
        del _cache[k]
    _cache[key] = _Entry(now + ttl, value, error)


async def _load(key: tuple[str, str, int]) -> dict:
    try:
        value = await fetch(*key)
    except FetchError as e:
        _store(key, value=None, error=str(e), ttl=FAIL_TTL)
        raise
    else:
        _store(key, value=value, error=None, ttl=CACHE_TTL)
        return value
    finally:
        _inflight.pop(key, None)


async def get_ranking(sort: str, period: str, page: int) -> dict:
    """キャッシュ越しの一覧。同じキーの同時要求は1回の取得を共有する。

    失敗は `FetchError`。呼び出し側が途中で切れても取得そのものは続け、
    結果はキャッシュに入る（`shield`）。
    """
    key = (sort, period, page)
    hit = _cache.get(key)
    if hit is not None and hit.expires > _now():
        if hit.error is not None:
            raise FetchError(hit.error)
        assert hit.value is not None
        return hit.value

    task = _inflight.get(key)
    if task is None:
        task = asyncio.get_running_loop().create_task(_load(key))
        _inflight[key] = task
        # 待つ人が全員いなくなっても「例外が回収されていない」警告を出さない
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return await asyncio.shield(task)
