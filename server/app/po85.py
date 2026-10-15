"""85po（https://www.85po.net/ja/）の一覧（ランキング）と、動画ページの解決。

`tokyomotion.py` と同じ作りの「一覧を取って画面向けに整える」部分と、
`urlfetch.py`（`POST /api/media/fetch`）が 85po のリンクを保存するときに使う
「動画ページから動画ファイルの URL を割り出す」部分の2つ。ファイルを落とす処理そのものは
`urlfetch._run_po85` にある（ここは URL と題名を割り出すだけ）。

## 作りの方針（一覧）

- **取得先は固定**。URL は `build_url()` が `https://www.85po.net/ja/{一覧}/?mode=async&function=get_block&…&from=N`
  と組む。利用者が渡すのは sort / period / page の名前（許可リストで検証）だけ＝SSRF にならない。
  リダイレクトも同じサイト内（https）だけ追う
- **非同期ブロック形式**（`mode=async&function=get_block`）を使う。通常ページと違い、ヘッダの
  おすすめカルーセルが混ざらず、1ページ分（24件）のブロックだけが返る。`from` がページ番号
  （`from=1` は省略と同じ）
- **標準ライブラリ + httpx だけ**（bs4/lxml は入っていない）。ブロックの形は `parse_listing()` の
  正規表現が決め打ちで読む。サイトの HTML が変わって1件も取れなくなったら、黙って空を返さず
  `FetchError`（→ 502）にする。本当に空なのは「ブロックはあるのに中身が空」のときだけ
- **プロセス内 TTL キャッシュ**（キーは sort/period/page、10分）。同じキーが同時に来ても取りに行くのは1回。
  残すのは整形済みの結果だけ。失敗も短く（30秒）覚える
- 範囲外のページ（最終ページより先）は、サイトが**空の 404** を返す。それは `items: []` にする。
  ブロックの名前が違うときの「空の 200」とは区別する（そちらは 502）

## 作りの方針（動画ページ）

yt-dlp は 85po を取れない（generic が KVS と判定して `Unable to extract flashvars` で失敗する。
埋め込み URL も同じ）。そこで専用に、動画ページの2か所から画質ごとの URL を集める。

- JSON-LD の `VideoObject`（`name` / `duration` / `contentUrl`）。`contentUrl` は 720p の1本だけ
- インライン JS のプレイヤー設定（kt_player。`flashvars` という語は無い）の `video_url`（480p）・
  `video_alt_url`（720p）・`video_alt_url2`（1080p）…。`*_text` が画質、`*_codec` が `av1` など

どちらかが欠けても取れるほうで動く。**URL に付いた `v-acctoken` は要らなかった**（無くても取れる）が、
付いているものを優先して使う。`get_file` は別ホストの CDN へ 302 で飛ばす（その先も https）。
Cookie も Referer も要らない（実測 2026-10-12）。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import NamedTuple
from urllib.parse import quote, urljoin, urlsplit

import httpx

log = logging.getLogger("nasapp.po85")

SITE_HOST = "www.85po.net"
SITE_HOSTS = (SITE_HOST, "85po.net")
THUMB_HOST = SITE_HOST
SITE_URL = f"https://{SITE_HOST}"
LANG = "ja"

# 実機の Pi から200で取れると確認済みのもの（2026-10-12）。変えると弾かれうる
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
)
_TIMEOUT = 20.0
_MAX_REDIRECTS = 3
# ブロックは実測 約30KB、動画ページは約50KB。これを大きく超えるなら想定外のページ
_MAX_HTML_BYTES = 3_000_000

# 一覧の種類 → (ページのパス, ブロック id, 期間 → sort_by)。web 側の選択肢と同じ名前。
# 人気の期間は「その期間の再生数」で並べる（Tokyomotion の「その期間に追加された動画」とは違う）。
# 新着は期間を持たない（どの期間でも post_date）
_LISTS: dict[str, tuple[str, str, dict[str, str]]] = {
    "popular": (
        "most-popular",
        "list_videos_common_videos_list",
        {
            "today": "video_viewed_today",
            "week": "video_viewed_week",
            "month": "video_viewed_month",
            "all": "video_viewed",
        },
    ),
    "rated": (
        "top-rated",
        "list_videos_common_videos_list",
        {
            "today": "rating_today",
            "week": "rating_week",
            "month": "rating_month",
            "all": "rating",
        },
    ),
    "recent": (
        "latest-updates",
        "list_videos_latest_videos_list",
        {p: "post_date" for p in ("today", "week", "month", "all")},
    ),
}
SORTS = tuple(_LISTS)
PERIODS = ("today", "week", "month", "all")
DEFAULT_SORT = "popular"
DEFAULT_PERIOD = "week"
MAX_PAGE = 50

CACHE_TTL = 600.0
FAIL_TTL = 30.0


class ParamError(ValueError):
    """sort / period / page が許可リストに無い。メッセージは利用者に見せてよい。"""


class FetchError(Exception):
    """取得に失敗した・ページの形が想定と違った。メッセージは利用者に見せてよい。"""


class PageNotFound(Exception):
    """一覧の範囲外のページ（サイトは空の 404 を返す）。"""


# ---------------------------------------------------------------- 入力と URL


def validate_params(sort: str, period: str, page: str | int) -> tuple[str, str, int]:
    """(sort, period, page) を検証して返す。不正なら `ParamError`。

    新着（recent）は期間を持たないので period は無視されるが、不正な値は弾く。
    """
    if sort not in _LISTS:
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
    path, block, sort_by = _LISTS[sort]
    return (
        f"{SITE_URL}/{LANG}/{path}/?mode=async&function=get_block"
        f"&block_id={block}&sort_by={sort_by[period]}&from={page}"
    )


def _is_site_redirect(url: str) -> bool:
    """リダイレクト先として許す場所（このサイトの https）。"""
    try:
        p = urlsplit(url)
        return (
            p.scheme == "https"
            and p.hostname in SITE_HOSTS
            and p.username is None
            and p.password is None
            and p.port in (None, 443)
        )
    except ValueError:
        return False


# 言語の接頭辞（/ja/ /en/ /zh-cn/ など）は付いても付かなくてもよい
_VIDEO_PATH_RE = re.compile(r"^/(?:[A-Za-z]{2}(?:-[A-Za-z]{2,4})?/)?video/(\d+)(?:/|$)")
_VIDEO_PATH_FULL_RE = re.compile(
    r"^/(?P<lang>[A-Za-z]{2}(?:-[A-Za-z]{2,4})?/)?video/(?P<id>\d+)(?:/(?P<slug>[^/?#]*))?"
)


def is_site_url(url: str) -> bool:
    """85po の動画ページ（`/video/{id}` を含む）の URL か。

    ホストは `www.85po.net` / `85po.net` だけ（前方一致や userinfo のすり抜けは通さない）。
    """
    url = (url or "").strip()
    if not url or re.search(r"[\s\x00-\x1f\\]", url):
        return False
    try:
        p = urlsplit(url)
        return (
            p.scheme in ("http", "https")
            and p.hostname in SITE_HOSTS
            and p.username is None
            and p.password is None
            and p.port in (None, 80, 443)
            and _VIDEO_PATH_RE.match(p.path) is not None
        )
    except ValueError:
        return False


def video_id(url: str) -> str | None:
    """動画ページの URL から動画 id。動画ページでなければ None。"""
    if not is_site_url(url):
        return None
    m = _VIDEO_PATH_RE.match(urlsplit(url.strip()).path)
    return m.group(1) if m else None


def normalize_page_url(url: str) -> str:
    """動画ページの URL を `https://www.85po.net/{言語}/video/{id}/{slug}/` の形にする。

    サイトは slug 無しの `/ja/video/{id}/` を 404 にするが、**間違った slug は正しい slug へ
    301 で飛ばす**。slug が無いときは `-` を入れて、その転送に直してもらう。
    動画ページでなければ `FetchError`。
    """
    if not is_site_url(url):
        raise FetchError("85po の動画ページの URL ではありません")
    m = _VIDEO_PATH_FULL_RE.match(urlsplit(url.strip()).path)
    assert m is not None
    slug = quote(m.group("slug") or "-", safe="-._~%")
    return f"{SITE_URL}/{m.group('lang') or ''}video/{m.group('id')}/{slug}/"


# ---------------------------------------------------------------- 一覧のパース

_ITEMS_OPEN_RE = re.compile(r'<div\b[^>]*\bid="list_videos_\w+_items"[^>]*>', re.I)
# 1件は <div class="item "> から始まる
_ITEM_START_RE = re.compile(r'<div\b[^>]*\bclass="item(?:\s[^"]*)?"[^>]*>', re.I)
_A_RE = re.compile(r"<a\b[^>]*>", re.I)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]*>")
_CURRENT_RE = re.compile(r"<li\b[^>]*\bpage-current\b[^>]*>\s*<span>\s*(\d+)\s*</span>", re.I)
_NEXT_RE = re.compile(r"""<li\b[^>]*\bclass=["']next["'][^>]*>\s*<a\b""", re.I)
_FROM_RE = re.compile(r"\bfrom:(\d+)")
_DURATION_RE = re.compile(r"^\d{1,3}(?::\d{2}){1,2}$")
_VIEWS_RE = re.compile(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*([KkMmBb万千])?\s*$")
_VIEWS_UNIT = {
    "k": 1_000,
    "千": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "万": 10_000,
}
# 1件ぶんの HTML の長さの上限（実測 1.2〜2KB）。最後の1件がページ下部まで食わないための歯止め
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
    m = re.search(rf"""(?<![\w-]){name}=(?:"([^"]*)"|'([^']*)')""", tag, re.I)
    if not m:
        return None
    return html.unescape(m.group(1) if m.group(1) is not None else m.group(2)).strip()


def _safe_thumbnail(url: str | None) -> str | None:
    """https で www.85po.net の `/contents/` 以下のものだけ通す。それ以外は None。"""
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
            and p.path.startswith("/contents/")
        )
    except ValueError:
        return None
    return url if ok else None


def parse_views(s: str | None) -> int | None:
    """`63K`→63000、`1.2M`→1200000、`1,234`→1234。読めなければ None（略記は概数）。"""
    m = _VIEWS_RE.match(s or "")
    if not m:
        return None
    n = Decimal(m.group(1).replace(",", ""))
    unit = m.group(2)
    if unit:
        n *= _VIEWS_UNIT[unit.lower()]
    return int(n)


def _parse_item(chunk: str) -> dict | None:
    """1件ぶん。`/video/{id}` が読めなければ（別のサイトへのリンクなども）None＝件ごと捨てる。"""
    a_m = _A_RE.search(chunk)
    href = html.unescape(_attr(a_m.group(0), "href") or "") if a_m else ""
    if not href:
        return None
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    if parts.netloc and (parts.hostname not in SITE_HOSTS or parts.scheme != "https"):
        return None
    m = _VIDEO_PATH_FULL_RE.match(parts.path)
    if not m:
        return None
    vid = m.group("id")
    # slug 無しの /ja/video/{id}/ は 404 になる（slug 付きでないと動画ページとして取れない）。
    # 付いていないときは `-` で、サイトに正しい slug へ転送してもらう
    slug = quote(m.group("slug") or "-", safe="-._~%")

    img_m = _IMG_RE.search(chunk)
    img = img_m.group(0) if img_m else ""

    title_m = re.search(r'<strong\b[^>]*\btitle\b[^>]*>(.*?)</strong>', chunk, re.I | re.S)
    title = _text(title_m.group(1)) if title_m else ""
    if not title:
        title = _attr(a_m.group(0), "title") or _attr(img, "alt") or ""
    if not title:
        title = f"動画 {vid}"

    duration = _div_text(chunk, "duration")
    rating = _div_text(chunk, "rating")
    rating_m = re.search(r"(\d{1,3})\s*%", rating or "")

    return {
        "id": vid,
        "title": title,
        "url": f"{SITE_URL}/{LANG}/video/{vid}/{slug}/",
        # 実画像は data-original（src は読み込み前の1px の gif）
        "thumbnail": _safe_thumbnail(_attr(img, "data-original") or _attr(img, "src")),
        "duration": duration if duration and _DURATION_RE.match(duration) else None,
        "views": parse_views(_div_text(chunk, "views")),
        "rating": int(rating_m.group(1)) if rating_m else None,
        "added": _div_text(chunk, "added") or None,
        # HD / 2K / 4K のどれかの印（`<span class="is-hd …">`）があれば HD
        "hd": re.search(r'\bclass="[^"]*(?<![\w-])is-hd(?![\w-])', chunk, re.I) is not None,
    }


def parse_listing(text: str, page: int) -> dict:
    """一覧ブロックの HTML を `{items, page, hasMore}` にする。

    形が想定と違って1件も読めないときは `FetchError`。本当に空（ブロックはあるが
    リンクが1つも無い）のときだけ空の items を返す。
    """
    box = _ITEMS_OPEN_RE.search(text)
    if not box:
        raise FetchError(
            "85po のページの形が想定と違い、一覧を読めませんでした"
            "（ブロックの名前が変わったか、別のページが返った可能性があります）"
        )

    # 範囲外のページは 404 になるのでここには来ないが、念のため。
    # 今のページ番号が頼んだものと違うなら、重複を返さず「その先は無い」とする
    current = _CURRENT_RE.search(text)
    if current and int(current.group(1)) != page:
        return {"items": [], "page": page, "hasMore": False}

    pager_at = text.find('<div class="pagination"', box.end())
    region = text[box.end() : pager_at if pager_at >= 0 else len(text)]

    starts = list(_ITEM_START_RE.finditer(region))
    items: list[dict] = []
    seen: set[str] = set()
    for i, sm in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(region)
        end = min(end, sm.start() + _ITEM_MAX_CHARS)
        item = _parse_item(region[sm.start() : end])
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append(item)

    if not items:
        if "<a" in region.lower():
            # リンクはあるのに1件も読めない＝サイト側の作りが変わった
            raise FetchError(
                "85po の一覧を読めませんでした（ページの作りが変わった可能性があります）"
            )
        return {"items": [], "page": page, "hasMore": False}

    has_more = _NEXT_RE.search(text) is not None or any(
        int(n) > page for n in _FROM_RE.findall(text)
    )
    # web 側が頼めるのは MAX_PAGE まで。その先を「ある」と言うと空振りの要求になる
    return {"items": items, "page": page, "hasMore": has_more and page < MAX_PAGE}


# ---------------------------------------------------------------- 一覧の取得


def _status_message(code: int) -> str:
    if code == 429:
        return "85po が短時間のアクセス過多として断りました（HTTP 429）。しばらく待ってから試してください"
    if code == 403:
        return "85po がアクセスを拒否しました（HTTP 403）。しばらく待ってから試してください"
    return f"85po から一覧を取得できませんでした（HTTP {code}）"


async def fetch_html(url: str) -> str:
    """固定ホストの URL を取る。リダイレクトは同じサイトの https だけ追う。

    範囲外のページ（空の 404）は `PageNotFound`。
    """
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
                if not _is_site_redirect(nxt):
                    raise FetchError("85po が想定外の場所へ転送しました")
                current = nxt
            else:
                raise FetchError("85po の転送が多すぎます")
    except httpx.TimeoutException as e:
        raise FetchError("85po の応答が遅く、取得を打ち切りました") from e
    except httpx.HTTPError as e:
        log.warning("85po への接続に失敗: %s", e)
        raise FetchError("85po に接続できませんでした") from e

    if resp.status_code == 404 and not resp.content.strip():
        raise PageNotFound()
    if resp.status_code != 200:
        raise FetchError(_status_message(resp.status_code))
    if len(resp.content) > _MAX_HTML_BYTES:
        raise FetchError("85po の応答が大きすぎます")
    return resp.text


_JST = timezone(timedelta(hours=9))


async def fetch(sort: str, period: str, page: int) -> dict:
    """キャッシュを通さず1回取りに行く。引数は `validate_params` を通したもの。"""
    try:
        text = await fetch_html(build_url(sort, period, page))
    except PageNotFound:
        result = {"items": [], "page": page, "hasMore": False}
    else:
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
    結果はキャッシュに入る（`shield`）。新着は期間で中身が変わらないので、
    期間違いでも同じキーに寄せる。
    """
    key = (sort, "all" if sort == "recent" else period, page)
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


# ---------------------------------------------------------------- 動画ページの解決
# ここから先はワーカースレッド（urlfetch）から呼ぶので同期。


def new_client() -> httpx.Client:
    """動画ページの取得とファイルの取得で共有するクライアント（Cookie も引き継ぐ）。"""
    return httpx.Client(
        timeout=httpx.Timeout(30.0, connect=15.0),
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"},
        follow_redirects=False,
    )


def _video_status_message(code: int) -> str:
    if code == 403:
        return "サイトに拒否されました（403）。ログインや地域制限の動画は取れません"
    if code == 404:
        return "ページが見つかりません（404）。削除された動画かもしれません"
    if code == 429:
        return "サイトが短時間のアクセス過多として断りました（429）。しばらく待ってから試してください"
    return f"85po から動画ページを取得できませんでした（HTTP {code}）"


def fetch_page(client: httpx.Client, url: str) -> str:
    """動画ページの HTML。リダイレクトは同じサイトの https だけ追う（slug の補正の 301 など）。"""
    current = url
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            resp = client.get(current)
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            loc = resp.headers.get("location")
            nxt = urljoin(current, loc) if loc else ""
            if not _is_site_redirect(nxt):
                raise FetchError("85po が想定外の場所へ転送しました")
            current = nxt
        else:
            raise FetchError("85po の転送が多すぎます")
    except httpx.TimeoutException as e:
        raise FetchError("85po の応答が遅く、取得を打ち切りました") from e
    except httpx.HTTPError as e:
        log.warning("85po への接続に失敗: %s", e)
        raise FetchError("85po に接続できませんでした") from e
    if resp.status_code != 200:
        raise FetchError(_video_status_message(resp.status_code))
    if len(resp.content) > _MAX_HTML_BYTES:
        raise FetchError("85po の応答が大きすぎます")
    return resp.text


_LD_RE = re.compile(
    r'<script\b[^>]*\btype=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S
)
_PLAYER_KV_RE = re.compile(
    r"""(?<![\w$.])(video_(?:alt_)?url\d*(?:_[a-z]+)?)\s*:\s*"""
    r"""(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")"""
)
_PLAYER_KEY_RE = re.compile(r"^(video_(?:alt_)?url\d*)(?:_([a-z]+))?$")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$", re.I
)
_HEIGHT_NAMED = {"2k": 1440, "4k": 2160, "8k": 4320}


def _js_unescape(s: str) -> str:
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return re.sub(r"\\(.)", r"\1", s)


def _iso_duration(s: str | None) -> int | None:
    m = _ISO_DURATION_RE.match((s or "").strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, sec = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + int(float(sec or 0))


def _clean_title(s: str | None) -> str:
    t = re.sub(r"[\x00-\x1f\x7f]+", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", t).strip()


def _height_of(label: str | None, url: str) -> int | None:
    """画質の高さ。ラベル（`720p` `1080p` `2K`）→ URL の `_720p.mp4` の順に読む。"""
    low = (label or "").strip().lower()
    m = re.search(r"(\d{3,4})\s*p", low)
    if m:
        return int(m.group(1))
    if low in _HEIGHT_NAMED:
        return _HEIGHT_NAMED[low]
    m = re.search(r"_(\d{3,4})p\.", urlsplit(url).path)
    return int(m.group(1)) if m else None


def _ext_of(url: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{2,4})/?$", urlsplit(url).path)
    return m.group(1).lower() if m else "mp4"


def _media_url(raw: str | None, base: str) -> str | None:
    """動画ファイルの URL として使えるもの（https）だけ。相対なら絶対にする。"""
    if not raw:
        return None
    url = urljoin(base, _js_unescape(html.unescape(raw)).strip())
    if re.search(r"[\s\x00-\x1f\\]", url):
        return None
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        return None
    return url


def _url_key(url: str) -> str:
    """トークン（クエリ）と末尾の `/` を除いた形。JSON-LD とプレイヤーの重複を見分ける。"""
    p = urlsplit(url)
    return f"{p.hostname}{p.path.rstrip('/')}"


def _json_ld_video(text: str) -> dict:
    """JSON-LD の `VideoObject`（無ければ空）。壊れた JSON は読み飛ばす。"""
    for m in _LD_RE.finditer(text):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if not isinstance(obj, dict):
                continue
            kind = obj.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "VideoObject" in kinds:
                return obj
    return {}


def _player_formats(text: str, base: str) -> tuple[list[dict], str]:
    """プレイヤー設定の `video_url` `video_alt_url` `video_alt_url2`… から画質ごとの URL と、題名。"""
    groups: dict[str, dict[str, str]] = {}
    for m in _PLAYER_KV_RE.finditer(text):
        km = _PLAYER_KEY_RE.match(m.group(1))
        if not km:
            continue
        value = m.group(2) if m.group(2) is not None else m.group(3)
        groups.setdefault(km.group(1), {})[km.group(2) or "url"] = _js_unescape(value)
    formats: list[dict] = []
    for g in groups.values():
        url = _media_url(g.get("url"), base)
        if not url:
            continue
        codec = (g.get("codec") or "").strip().lower() or None
        formats.append(
            {"url": url, "height": _height_of(g.get("text"), url), "codec": codec, "ext": _ext_of(url)}
        )
    tm = re.search(
        r"""(?<![\w$.])video_title\s*:\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")""", text
    )
    title = _js_unescape((tm.group(1) if tm.group(1) is not None else tm.group(2)) or "") if tm else ""
    return formats, title


def _format_key(f: dict) -> tuple[int, int]:
    # 高さが大きいほうが上。同じ高さなら AV1 より（端末で再生しやすい）それ以外を優先
    return (f["height"] or 0, 0 if f["codec"] == "av1" else 1)


def pick_format(formats: list[dict]) -> dict:
    """最高画質を選ぶ。同じ解像度なら AV1 より H.264 などを優先（無ければ AV1）。"""
    if not formats:
        raise FetchError("動画のURLを見つけられませんでした")
    return max(formats, key=_format_key)


def _login_required(text: str) -> bool:
    """プレイヤー欄（`class="player"` から `class="video-info"` まで）がログインを求めているか。

    ヘッダやお気に入りボタンにも `login-required` のリンクが常にあるので、ページ全体では見ない。
    会員限定の動画の見え方は未確認（公開の一覧には出てこない）。KVS の作りからの推測。
    """
    m = re.search(r'<div\b[^>]*\bclass="player\b[^"]*"[^>]*>(.*?)<div\b[^>]*\bclass="video-info', text, re.I | re.S)
    if not m:
        return False
    return re.search(r"login-required|ログイン|会員|\b(?:private|premium)\b", m.group(1), re.I) is not None


def parse_video_page(text: str, page_url: str = SITE_URL) -> dict:
    """動画ページの HTML から `{title, formats:[{url, height, codec, ext}], duration}`。

    JSON-LD とプレイヤー設定の両方を見て、どちらかが欠けても取れるほうで動く。
    `formats` は画質の高い順。何も取れなければ `FetchError`（会員限定らしければその旨）。
    """
    ld = _json_ld_video(text)
    formats, player_title = _player_formats(text, page_url)

    seen = {_url_key(f["url"]) for f in formats}
    content = _media_url(ld.get("contentUrl") if isinstance(ld.get("contentUrl"), str) else None, page_url)
    if content and _url_key(content) not in seen:
        formats.append(
            {"url": content, "height": _height_of(None, content), "codec": None, "ext": _ext_of(content)}
        )

    if not formats:
        if _login_required(text):
            raise FetchError("ログインが必要な動画は取れません")
        raise FetchError("動画のURLを見つけられませんでした（ページの作りが変わった可能性があります）")

    formats.sort(key=_format_key, reverse=True)

    title = _clean_title(ld.get("name") if isinstance(ld.get("name"), str) else None)
    if not title:
        title = _clean_title(player_title)
    if not title:
        og = re.search(r"""<meta\b[^>]*\bproperty=["']og:title["'][^>]*>""", text, re.I)
        title = _clean_title(_attr(og.group(0), "content") if og else None)

    duration = _iso_duration(ld.get("duration") if isinstance(ld.get("duration"), str) else None)
    if duration is None:
        dm = re.search(r"""<meta\b[^>]*\bproperty=["']video:duration["'][^>]*>""", text, re.I)
        raw = _attr(dm.group(0), "content") if dm else None
        duration = int(raw) if raw and raw.isdigit() else None

    return {"title": title, "formats": formats, "duration": duration}


def resolve_video(page_url: str, *, client: httpx.Client | None = None) -> dict:
    """動画ページを取って `{title, formats:[{url, height, codec, ext}], duration}` にする。

    `client` を渡すと（`new_client()`）ファイルの取得と Cookie を共有できる。
    失敗は `FetchError`（日本語。そのまま利用者に見せてよい）。
    """
    url = normalize_page_url(page_url)
    own = client is None
    c = client if client is not None else new_client()
    try:
        text = fetch_page(c, url)
    finally:
        if own:
            c.close()
    return parse_video_page(text, url)
