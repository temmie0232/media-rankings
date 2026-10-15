"""85po の一覧 API・動画ページの解決 (docs/po85-api.md) の回帰テスト。

ネットには出ない。取得（`fetch_html` / `fetch`、動画ページは httpx の MockTransport）を
差し替え、HTML は実サイトのカード・プレイヤー設定と同じ作りで組んだ**架空の題名**の断片を使う
（実在の題名・URL は入れない）。保存の道（urlfetch._run_po85）は test_urlfetch_po85.py。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import po85

THUMB = "https://www.85po.net/contents/videos_screenshots/9000"
GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


@pytest.fixture(autouse=True)
def _clean_cache():
    po85.clear_cache()
    yield
    po85.clear_cache()


# ---- HTML の組み立て（実サイトの非同期ブロックと同じ作り） ----


def _card(
    vid: str,
    title: str,
    *,
    thumb: str | None = None,
    hd: str | None = "HD",
    duration: str = "0:54",
    added: str = "5日前",
    views: str | None = "63K",
    rating: str | None = "87%",
    slug: str | None = "ao-no-todai",
    href: str | None = None,
    lazy: bool = True,
) -> str:
    src = thumb if thumb is not None else f"{THUMB}/{vid}/320x180/1.jpg"
    img = (
        f'<img class="thumb lazy-load" src="{GIF}" data-original="{src}" '
        f'data-webp="{src}" alt="{title}" data-cnt="1" width="320" height="180"/>'
        if lazy
        else f'<img class="thumb" src="{src}" alt="{title}" width="320" height="180"/>'
    )
    hd_html = f'<span class="is-hd{" is-2k" if hd == "2K" else ""}">{hd}</span>' if hd else ""
    views_html = f'<div class="views">{views}</div>' if views is not None else ""
    rating_html = f'<div class="rating positive"> {rating} </div>' if rating is not None else ""
    link = href if href is not None else (
        f"https://www.85po.net/ja/video/{vid}/{slug}/" if slug else f"https://www.85po.net/ja/video/{vid}/"
    )
    return (
        f'<div class="item "> <a href="{link}" title="{title}" > <div class="img"> {img} '
        f'<span class="ico-fav-0 " title="お気に入りに追加" data-fav-video-id="{vid}" data-fav-type="0"></span> '
        f'<span class="ico-fav-1 " title="後で見る" data-fav-video-id="{vid}" data-fav-type="1"></span> '
        f'{hd_html} <div class="duration">{duration}</div> </div> '
        f'<strong class="title"> {title} </strong> <div class="wrap"> {views_html} {rating_html} '
        f'<div class="added"><em>{added}</em></div> </div> </a> </div>'
    )


def _pager(current: int, last: int, *, has_next: bool = True) -> str:
    def li(n: int) -> str:
        return (
            f'<li class="page"><a href="/ja/most-popular/{n}/" data-action="ajax" '
            f'data-block-id="list_videos_common_videos_list" '
            f'data-parameters="sort_by:video_viewed_week;from:{n}">{n}</a></li>'
        )

    nxt = (
        f'<li class="next"><a href="/ja/most-popular/{current + 1}/" data-action="ajax" '
        f'data-parameters="sort_by:video_viewed_week;from:{current + 1}">次へ</a></li>'
        if has_next
        else '<li class="next"><span>次へ</span></li>'
    )
    return (
        '<div class="pagination" id="list_videos_common_videos_list_pagination"> '
        '<div class="pagination-holder"> <ul> <li class="prev"><span>前へ</span></li> '
        f'<li class="page-current"><span>{current}</span></li> {li(current + 1) if has_next else ""} '
        f'<li class="last"><a href="/ja/most-popular/{last}/" data-parameters="sort_by:video_viewed_week;from:{last}">最後の</a></li> '
        f"{nxt} </ul> </div> </div>"
    )


def _block(cards: list[str], *, pager: str = "", block: str = "list_videos_common_videos_list") -> str:
    return (
        f'<div id="{block}"> <div class="headline"> <h1> 1週間の人気のビデオ </h1> <div class="sort"> '
        f'<ul id="{block}_sort_list"> <li> <a data-action="ajax" data-container-id="{block}_sort_list" '
        f'data-block-id="{block}" data-parameters="sort_by:video_viewed">すべての時間で</a> </li> </ul> </div> </div> '
        f'<div class="box wrapper"> <div class="list-videos"> <div class="margin-fix" id="{block}_items"> '
        f"{' '.join(cards)} </div> </div> </div> {pager} </div>"
    )


# ---- 入力検証と URL ----


@pytest.mark.parametrize(
    "sort,path,block,by",
    [
        ("popular", "most-popular", "list_videos_common_videos_list",
         {"today": "video_viewed_today", "week": "video_viewed_week", "month": "video_viewed_month", "all": "video_viewed"}),
        ("rated", "top-rated", "list_videos_common_videos_list",
         {"today": "rating_today", "week": "rating_week", "month": "rating_month", "all": "rating"}),
        ("recent", "latest-updates", "list_videos_latest_videos_list",
         {"today": "post_date", "week": "post_date", "month": "post_date", "all": "post_date"}),
    ],
)
def test_build_url_is_fixed_host(sort, path, block, by):
    for period, sort_by in by.items():
        assert po85.build_url(sort, period, 3) == (
            f"https://www.85po.net/ja/{path}/?mode=async&function=get_block"
            f"&block_id={block}&sort_by={sort_by}&from=3"
        )


@pytest.mark.parametrize(
    "sort,period,page",
    [
        ("evil", "week", "1"),
        ("tokyo", "week", "1"),  # 他サイトの名前は通さない
        ("favorites", "week", "1"),
        ("popular", "forever", "1"),
        ("recent", "forever", "1"),  # 新着は期間を使わないが、不正な値は弾く
        ("https://evil.example/", "week", "1"),
        ("popular", "week", "0"),
        ("popular", "week", "51"),
        ("popular", "week", "-1"),
        ("popular", "week", "abc"),
        ("popular", "week", "1.5"),
        ("popular", "week", ""),
        ("popular", "week", "１"),
        ("popular", "week", "1&sort_by=x"),
    ],
)
def test_validate_params_rejects(sort, period, page):
    with pytest.raises(po85.ParamError):
        po85.validate_params(sort, period, page)


def test_validate_params_accepts_edges():
    assert po85.validate_params("popular", "week", "1") == ("popular", "week", 1)
    assert po85.validate_params("rated", "all", "50") == ("rated", "all", 50)
    assert po85.validate_params("recent", "month", " 7 ") == ("recent", "month", 7)


# ---- 一覧のパース ----


def test_parse_fields_of_a_card():
    text = _block(
        [_card("1001", "テスト動画 &amp; 青い灯台", hd="2K", duration="1:07:42", views="1.2M",
               rating="87%", added="2週間前", slug="tesuto-doga")],
        pager=_pager(1, 567),
    )
    r = po85.parse_listing(text, 1)
    assert r["page"] == 1
    assert r["hasMore"] is True
    assert r["items"] == [
        {
            "id": "1001",
            # 題名は実体参照を戻す。url は slug 付き（slug 無しの /ja/video/{id}/ は 404 になる）
            "title": "テスト動画 & 青い灯台",
            "url": "https://www.85po.net/ja/video/1001/tesuto-doga/",
            "thumbnail": f"{THUMB}/1001/320x180/1.jpg",
            "duration": "1:07:42",
            "views": 1200000,
            "rating": 87,
            "added": "2週間前",
            "hd": True,
        }
    ]


def test_parse_optional_fields_missing():
    text = _block([_card("1002", "ひなたの散歩", rating=None, views=None, hd=None, duration="0:19")])
    (item,) = po85.parse_listing(text, 1)["items"]
    assert item["rating"] is None
    assert item["views"] is None  # 0 ではなく null
    assert item["hd"] is False
    assert item["duration"] == "0:19"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("63K", 63000),
        ("1.2M", 1200000),
        ("9.9K", 9900),
        ("108K", 108000),
        ("1,234", 1234),
        ("434576", 434576),
        ("0", 0),
        ("2.5万", 25000),
        ("1.1k", 1100),
        ("", None),
        (None, None),
        ("abc", None),
        ("K", None),
        ("1.2.3M", None),
        ("-5", None),
    ],
)
def test_parse_views(raw, expected):
    assert po85.parse_views(raw) == expected


def test_parse_drops_odd_duration():
    (item,) = po85.parse_listing(_block([_card("1004", "あおぞら", duration="LIVE")]), 1)["items"]
    assert item["duration"] is None


def test_hd_badge_covers_hd_2k_4k():
    text = _block([_card("1010", "あ", hd="HD"), _card("1011", "い", hd="2K"), _card("1012", "う", hd="4K"), _card("1013", "え", hd=None)])
    assert [i["hd"] for i in po85.parse_listing(text, 1)["items"]] == [True, True, True, False]


@pytest.mark.parametrize(
    "thumb",
    [
        "http://www.85po.net/contents/videos_screenshots/1/320x180/1.jpg",  # https でない
        "https://evil.example/contents/videos_screenshots/1/320x180/1.jpg",  # 別ホスト
        "https://www.85po.net.evil.example/contents/x.jpg",  # 前方一致のすり抜け
        "https://www.85po.net@evil.example/contents/x.jpg",  # userinfo のすり抜け
        "https://evil.example/@www.85po.net/contents/x.jpg",
        "https://www.85po.net:8443/contents/x.jpg",  # 別ポート
        "https://www.85po.net/static/images/x.jpg",  # /contents/ 以外
        "https://www.85po.net/contentsx/x.jpg",
        "//www.85po.net/contents/x.jpg",  # スキーム無し
        "javascript:alert(1)",
        "data:image/jpeg;base64,AAAA",
        GIF,
    ],
)
def test_parse_rejects_foreign_thumbnail(thumb):
    (item,) = po85.parse_listing(_block([_card("1005", "さざなみ", thumb=thumb)]), 1)["items"]
    # 項目は残し、サムネだけ落とす（保存は URL だけで足りる）
    assert item["id"] == "1005"
    assert item["thumbnail"] is None


def test_thumbnail_falls_back_to_src_when_not_lazy():
    (item,) = po85.parse_listing(_block([_card("1006", "もみじ", lazy=False)]), 1)["items"]
    assert item["thumbnail"] == f"{THUMB}/1006/320x180/1.jpg"


def test_url_without_slug_gets_a_placeholder_the_site_redirects():
    # slug 無しは 404。間違った slug は正しい slug へ 301 されるので、`-` を入れておく
    (item,) = po85.parse_listing(_block([_card("1007", "いちょう", slug=None)]), 1)["items"]
    assert item["url"] == "https://www.85po.net/ja/video/1007/-/"


def test_parse_dedupes_and_skips_foreign_or_non_video_cards():
    foreign = _card("1008", "外部", href="https://evil.example/ja/video/1008/x/")
    other = _card("1009", "ユーザー", href="https://www.85po.net/ja/members/12/")
    text = _block([_card("1006", "もみじ"), _card("1006", "もみじ"), foreign, other, _card("1007", "いちょう")])
    assert [i["id"] for i in po85.parse_listing(text, 1)["items"]] == ["1006", "1007"]


def test_item_url_is_always_on_the_site_even_with_a_relative_href():
    (item,) = po85.parse_listing(_block([_card("1014", "さくら", href="/ja/video/1014/sakura/")]), 1)["items"]
    assert item["url"] == "https://www.85po.net/ja/video/1014/sakura/"


def test_last_card_does_not_read_the_pager():
    # 最後のカードに rating が無いとき、ページ下部の別の要素を拾わない
    text = _block(
        [_card("1015", "つばめ", rating=None, views=None)],
        pager=_pager(1, 2) + '<div class="rating positive"> 99% </div><div class="views">9M</div>',
    )
    (item,) = po85.parse_listing(text, 1)["items"]
    assert item["rating"] is None and item["views"] is None


def test_headline_sort_list_is_not_read_as_cards():
    # 見出しの並び替えリストにも <a> があるが、カードは items の中だけ
    (item,) = po85.parse_listing(_block([_card("1016", "かもめ")]), 1)["items"]
    assert item["id"] == "1016"


# ---- hasMore / 空 / 想定外 ----


def test_has_more_from_the_next_link_and_the_last_page():
    cards = [_card("2001", "あ")]
    assert po85.parse_listing(_block(cards, pager=_pager(1, 9)), 1)["hasMore"] is True
    last = _pager(9, 9, has_next=False)
    assert po85.parse_listing(_block(cards, pager=last), 9)["hasMore"] is False


def test_has_more_without_a_next_link_uses_page_numbers():
    pager = _pager(1, 9).replace('class="next"', 'class="nxt"')
    assert po85.parse_listing(_block([_card("2002", "い")], pager=pager), 1)["hasMore"] is True


def test_single_page_without_pager_has_no_more():
    assert po85.parse_listing(_block([_card("2003", "う")]), 1)["hasMore"] is False


def test_has_more_is_false_at_max_page():
    text = _block([_card("2004", "え")], pager=_pager(po85.MAX_PAGE, 567))
    assert po85.parse_listing(text, po85.MAX_PAGE)["hasMore"] is False


def test_genuinely_empty_block_is_not_an_error():
    # ブロックはあるがカード（リンク）が1つも無い
    assert po85.parse_listing(_block([]), 1) == {"items": [], "page": 1, "hasMore": False}


@pytest.mark.parametrize(
    "text",
    [
        "",  # ブロック名が違うとサイトは空の 200 を返す
        "<html><body>Please verify your age to continue</body></html>",
        "<html><body><h1>Just a moment...</h1></body></html>",
        # ブロックはあるのにカードの作りが変わった（リンクはある）
        _block(['<div class="card"><a href="/ja/video/1/x/">x</a></div>']),
        # カードはあるが /video/{id} が読めない
        _block(['<div class="item "><a href="/ja/members/1/">x</a></div>']),
        # 別のブロックの名前が返った
        '<div id="something_else"><div class="item "><a href="/ja/video/1/x/">x</a></div></div>',
    ],
)
def test_unexpected_html_is_an_error_not_empty(text):
    with pytest.raises(po85.FetchError):
        po85.parse_listing(text, 1)


def test_page_number_mismatch_returns_empty_instead_of_a_duplicate():
    text = _block([_card("2005", "お")], pager=_pager(1, 3))
    assert po85.parse_listing(text, 2) == {"items": [], "page": 2, "hasMore": False}


# ---- 取得（ネットは差し替え） ----


def test_fetch_requests_only_the_built_url(monkeypatch):
    seen: list[str] = []

    async def fake_html(url: str) -> str:
        seen.append(url)
        return _block([_card("3001", "か")], pager=_pager(2, 3))

    monkeypatch.setattr(po85, "fetch_html", fake_html)
    r = asyncio.run(po85.fetch("rated", "month", 2))
    assert seen == [po85.build_url("rated", "month", 2)]
    assert "block_id=list_videos_common_videos_list&sort_by=rating_month&from=2" in seen[0]
    assert r["page"] == 2 and r["items"][0]["id"] == "3001"
    # 例: 2026-10-12T12:00:00+09:00
    assert r["fetchedAt"].endswith("+09:00") and "T" in r["fetchedAt"]


def test_fetch_turns_a_missing_page_into_an_empty_result(monkeypatch):
    async def gone(url: str) -> str:
        raise po85.PageNotFound()

    monkeypatch.setattr(po85, "fetch_html", gone)
    r = asyncio.run(po85.fetch("popular", "week", 50))
    assert r["items"] == [] and r["hasMore"] is False and r["page"] == 50 and r["fetchedAt"]


@pytest.fixture()
def fake_site(monkeypatch):
    """`fetch_html` の中の httpx.AsyncClient を MockTransport に差し替える。"""
    real = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            po85.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
        )

    return install


def test_fetch_html_distinguishes_empty_404_from_other_failures(fake_site):
    fake_site(lambda req: httpx.Response(404, content=b""))
    with pytest.raises(po85.PageNotFound):
        asyncio.run(po85.fetch_html("https://www.85po.net/ja/most-popular/?from=999"))

    fake_site(lambda req: httpx.Response(404, content=b"<html>Not Found</html>"))
    with pytest.raises(po85.FetchError, match="404"):
        asyncio.run(po85.fetch_html("https://www.85po.net/x"))

    fake_site(lambda req: httpx.Response(429, content=b"slow down"))
    with pytest.raises(po85.FetchError, match="429"):
        asyncio.run(po85.fetch_html("https://www.85po.net/x"))

    fake_site(lambda req: httpx.Response(200, content="ok".encode()))
    assert asyncio.run(po85.fetch_html("https://www.85po.net/x")) == "ok"


def test_fetch_html_follows_only_same_site_redirects(fake_site):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "85po.net":
            return httpx.Response(301, headers={"location": "https://www.85po.net/ja/y"})
        if req.url.path == "/ja/y":
            return httpx.Response(200, content=b"landed")
        return httpx.Response(302, headers={"location": "https://evil.example/ja/y"})

    fake_site(handler)
    assert asyncio.run(po85.fetch_html("https://85po.net/ja/x")) == "landed"
    with pytest.raises(po85.FetchError, match="想定外の場所"):
        asyncio.run(po85.fetch_html("https://www.85po.net/ja/x"))


def test_fetch_html_reports_connection_failures(fake_site):
    def boom(req):
        raise httpx.ConnectError("no route")

    fake_site(boom)
    with pytest.raises(po85.FetchError, match="接続できません"):
        asyncio.run(po85.fetch_html("https://www.85po.net/x"))


def test_redirect_target_check():
    assert po85._is_site_redirect("https://www.85po.net/ja/x")
    assert po85._is_site_redirect("https://85po.net/ja/x")
    assert not po85._is_site_redirect("http://www.85po.net/ja/x")
    assert not po85._is_site_redirect("https://evil.example/ja/x")
    assert not po85._is_site_redirect("https://www.85po.net.evil.example/")
    assert not po85._is_site_redirect("https://www.85po.net@evil.example/")
    assert not po85._is_site_redirect("")


# ---- キャッシュ ----


def _fake_fetch(monkeypatch, calls: list, *, delay: float = 0.0, fail: str | None = None):
    async def fake(sort, period, page):
        calls.append((sort, period, page))
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise po85.FetchError(fail)
        return {"items": [], "page": page, "hasMore": False, "fetchedAt": "x"}

    monkeypatch.setattr(po85, "fetch", fake)


def test_cache_hit_within_ttl_and_miss_per_key(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls)
    clock = [1000.0]
    monkeypatch.setattr(po85, "_now", lambda: clock[0])

    async def run():
        await po85.get_ranking("popular", "week", 1)
        await po85.get_ranking("popular", "week", 1)
        await po85.get_ranking("popular", "week", 2)  # キーが違えば別
        await po85.get_ranking("rated", "week", 1)
        assert len(calls) == 3
        clock[0] += po85.CACHE_TTL - 1
        await po85.get_ranking("popular", "week", 1)
        assert len(calls) == 3
        clock[0] += 2  # 10分を過ぎた
        await po85.get_ranking("popular", "week", 1)
        assert len(calls) == 4

    asyncio.run(run())


def test_recent_shares_one_cache_entry_across_periods(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls)

    async def run():
        for period in ("today", "week", "month", "all"):
            await po85.get_ranking("recent", period, 1)
        assert len(calls) == 1
        # 人気は期間ごとに別
        await po85.get_ranking("popular", "today", 1)
        await po85.get_ranking("popular", "all", 1)
        assert len(calls) == 3

    asyncio.run(run())


def test_concurrent_requests_share_one_fetch(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls, delay=0.05)

    async def run():
        rs = await asyncio.gather(*[po85.get_ranking("popular", "week", 1) for _ in range(5)])
        assert len(calls) == 1
        assert all(r is rs[0] for r in rs)
        assert not po85._inflight

    asyncio.run(run())


def test_cancelled_waiter_does_not_cancel_the_shared_fetch(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls, delay=0.05)

    async def run():
        first = asyncio.ensure_future(po85.get_ranking("popular", "week", 1))
        await asyncio.sleep(0.01)
        first.cancel()
        r = await po85.get_ranking("popular", "week", 1)  # 進行中の取得を待つ
        assert r["page"] == 1 and len(calls) == 1

    asyncio.run(run())


def test_failure_is_remembered_briefly_then_retried(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls, fail="取れません")
    clock = [1000.0]
    monkeypatch.setattr(po85, "_now", lambda: clock[0])

    async def run():
        for _ in range(3):
            with pytest.raises(po85.FetchError, match="取れません"):
                await po85.get_ranking("popular", "week", 1)
        assert len(calls) == 1  # サイトを叩き続けない
        clock[0] += po85.FAIL_TTL + 1
        with pytest.raises(po85.FetchError):
            await po85.get_ranking("popular", "week", 1)
        assert len(calls) == 2

    asyncio.run(run())


def test_cache_prunes_expired_entries(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls)
    clock = [1000.0]
    monkeypatch.setattr(po85, "_now", lambda: clock[0])

    async def run():
        await po85.get_ranking("popular", "week", 1)
        await po85.get_ranking("popular", "week", 2)
        clock[0] += po85.CACHE_TTL + 1
        await po85.get_ranking("popular", "week", 3)
        assert set(po85._cache) == {("popular", "week", 3)}

    asyncio.run(run())


def test_cache_holds_only_the_parsed_result(monkeypatch):
    async def fake_html(url: str) -> str:
        return _block([_card("3002", "き")])

    monkeypatch.setattr(po85, "fetch_html", fake_html)
    asyncio.run(po85.get_ranking("popular", "week", 1))
    (entry,) = po85._cache.values()
    assert set(entry.value) == {"items", "page", "hasMore", "fetchedAt"}
    assert "<div" not in repr(entry)


# ---- エンドポイント ----


def test_ranking_requires_auth(client):
    assert client.get("/api/po85/ranking").status_code == 401


@pytest.mark.parametrize(
    "qs",
    [
        "sort=evil", "sort=favorites", "period=forever", "page=0", "page=51", "page=abc",
        "sort=", "page=1.5", "sort=recent&period=forever",
    ],
)
def test_ranking_rejects_bad_params_with_400(client, auth, monkeypatch, qs):
    async def boom(*a):
        raise AssertionError("不正な入力で取得に行ってはいけない")

    monkeypatch.setattr(po85, "fetch", boom)
    r = client.get(f"/api/po85/ranking?{qs}", headers=auth)
    assert r.status_code == 400
    assert "ください" in r.json()["detail"]


def test_ranking_returns_items_with_defaults(client, auth, monkeypatch):
    seen: list[str] = []

    async def fake_html(url: str) -> str:
        seen.append(url)
        return _block(
            [_card("4001", "きりん", hd="2K"), _card("4002", "ぞう", rating=None, views=None)],
            pager=_pager(1, 3),
        )

    monkeypatch.setattr(po85, "fetch_html", fake_html)
    r = client.get("/api/po85/ranking", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    # 既定は popular / week / 1
    assert seen == [po85.build_url("popular", "week", 1)]
    assert body["page"] == 1 and body["hasMore"] is True
    assert [i["id"] for i in body["items"]] == ["4001", "4002"]
    assert body["items"][0]["hd"] is True
    assert body["items"][1]["rating"] is None and body["items"][1]["views"] is None
    assert set(body["items"][0]) == {
        "id", "title", "url", "thumbnail", "duration", "views", "rating", "added", "hd",
    }
    assert body["fetchedAt"]

    # 2回目はキャッシュ（取りに行かない）
    assert client.get("/api/po85/ranking", headers=auth).status_code == 200
    assert len(seen) == 1


def test_ranking_passes_query_to_the_fixed_url(client, auth, monkeypatch):
    seen: list[str] = []

    async def fake_html(url: str) -> str:
        seen.append(url)
        return _block([_card("4003", "さる")])

    monkeypatch.setattr(po85, "fetch_html", fake_html)
    r = client.get("/api/po85/ranking?sort=rated&period=all&page=7", headers=auth)
    assert r.status_code == 200
    assert r.json()["page"] == 7
    assert seen == [po85.build_url("rated", "all", 7)]
    assert "sort_by=rating&from=7" in seen[0]


def test_ranking_recent_ignores_the_period(client, auth, monkeypatch):
    seen: list[str] = []

    async def fake_html(url: str) -> str:
        seen.append(url)
        return _block([_card("4004", "たぬき")], block="list_videos_latest_videos_list")

    monkeypatch.setattr(po85, "fetch_html", fake_html)
    a = client.get("/api/po85/ranking?sort=recent&period=today", headers=auth)
    b = client.get("/api/po85/ranking?sort=recent&period=all", headers=auth)
    assert a.status_code == b.status_code == 200
    assert seen == [po85.build_url("recent", "all", 1)]  # 同じ取得を共有
    assert "latest-updates" in seen[0] and "sort_by=post_date" in seen[0]


def test_ranking_is_502_when_the_source_fails(client, auth, monkeypatch):
    async def fake_html(url: str) -> str:
        raise po85.FetchError("85po が短時間のアクセス過多として断りました（HTTP 429）")

    monkeypatch.setattr(po85, "fetch_html", fake_html)
    r = client.get("/api/po85/ranking?sort=rated", headers=auth)
    assert r.status_code == 502
    assert "429" in r.json()["detail"]


def test_ranking_is_502_when_html_is_unexpected(client, auth, monkeypatch):
    async def fake_html(url: str) -> str:
        return "<html><body>age check</body></html>"

    monkeypatch.setattr(po85, "fetch_html", fake_html)
    assert client.get("/api/po85/ranking", headers=auth).status_code == 502


def test_ranking_empty_is_200_not_502(client, auth, monkeypatch):
    async def fake_html(url: str) -> str:
        return _block([])

    monkeypatch.setattr(po85, "fetch_html", fake_html)
    r = client.get("/api/po85/ranking?period=today", headers=auth)
    assert r.status_code == 200
    assert r.json()["items"] == [] and r.json()["hasMore"] is False


def test_ranking_past_the_last_page_is_200_empty(client, auth, monkeypatch):
    async def gone(url: str) -> str:
        raise po85.PageNotFound()

    monkeypatch.setattr(po85, "fetch_html", gone)
    r = client.get("/api/po85/ranking?page=50", headers=auth)
    assert r.status_code == 200
    assert r.json()["items"] == [] and r.json()["page"] == 50


# ---- 動画ページの URL ----


@pytest.mark.parametrize(
    "url",
    [
        "https://www.85po.net/ja/video/36313/ao-no-todai/",
        "https://www.85po.net/ja/video/36313/",
        "https://www.85po.net/ja/video/36313",
        "https://85po.net/en/video/36313/ao-no-todai/",
        "http://www.85po.net/ja/video/36313/ao-no-todai/",
        "https://www.85po.net/video/36313/ao-no-todai/",  # 言語の接頭辞なし
        "https://www.85po.net/zh-cn/video/36313/ao-no-todai/",
        "  https://WWW.85PO.NET/ja/video/36313/ao-no-todai/?x=1#y  ",
    ],
)
def test_is_site_url_accepts_video_pages(url):
    assert po85.is_site_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://www.85po.net/",
        "https://www.85po.net/ja/most-popular/",
        "https://www.85po.net/ja/video/",
        "https://www.85po.net/ja/video/abc/",
        "https://www.85po.net/ja/videos/36313/",
        "https://www.85po.net/ja/embed/36313",
        "https://www.85po.net/ja/members/1/video/36313/",
        "https://evil.example/ja/video/36313/ao-no-todai/",
        "https://evil.example/?u=https://www.85po.net/ja/video/36313/",
        "https://www.85po.net.evil.example/ja/video/36313/",
        "https://www.85po.net@evil.example/ja/video/36313/",
        "https://evil.example@www.85po.net/ja/video/36313/",
        "https://www.85po.net:8443/ja/video/36313/",
        "https://sub.85po.net/ja/video/36313/",
        "ftp://www.85po.net/ja/video/36313/",
        "//www.85po.net/ja/video/36313/",
        "https://www.85po.net/ja/video/36313/ x",
        "https://www.85po.net\\@evil.example/ja/video/36313/",
    ],
)
def test_is_site_url_rejects_everything_else(url):
    assert not po85.is_site_url(url)


def test_video_id_and_normalize_page_url():
    assert po85.video_id("https://85po.net/en/video/36313/ao-no-todai/") == "36313"
    assert po85.video_id("https://example.com/video/1/") is None
    assert po85.normalize_page_url("http://85po.net/en/video/36313/ao-no-todai/?x=1#y") == (
        "https://www.85po.net/en/video/36313/ao-no-todai/"
    )
    # slug が無ければ `-`（間違った slug は正しい slug へ 301 される）
    assert po85.normalize_page_url("https://www.85po.net/ja/video/36313/") == (
        "https://www.85po.net/ja/video/36313/-/"
    )
    assert po85.normalize_page_url("https://www.85po.net/video/36313") == "https://www.85po.net/video/36313/-/"
    with pytest.raises(po85.FetchError):
        po85.normalize_page_url("https://evil.example/ja/video/36313/")


# ---- 動画ページのパース（実サイトのプレイヤー設定と同じ作りの架空の断片） ----

CDN_BASE = "https://www.85po.net/ja/get_file/3"


def _ld(**over) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": "青い灯台の散歩",
        "description": "青い灯台の散歩",
        "duration": "PT0H2M30S",
        "contentUrl": f"{CDN_BASE}/aaaa/9000/9001/9001_720p.mp4/",
        **over,
    }
    data = {k: v for k, v in data.items() if v is not None}
    return f'<script type="application/ld+json"> {json.dumps(data, ensure_ascii=True)} </script>'


def _player(*, v480: bool = True, v720: str | None = "av1", v1080: str | None = "av1", title: str | None = "青い灯台の散歩") -> str:
    parts = ["video_id: '9001'"]
    if title is not None:
        parts.append(f"video_title: '{title}'")
    parts.append("license_code: '$123456789012345', lrc: '1', rnd: '2'")
    if v480:
        parts.append(f"video_url: '{CDN_BASE}/bbbb/9000/9001/9001.mp4/?v-acctoken=TOK480'")
        parts.append("postfix: '.mp4'")
        parts.append("video_url_text: '480p'")
    if v720:
        parts.append(f"video_alt_url: '{CDN_BASE}/aaaa/9000/9001/9001_720p.mp4/?v-acctoken=TOK720'")
        parts.append("video_alt_url_text: '720p'")
        parts.append("video_alt_url_hd: '1'")
        if v720 != "none":
            parts.append(f"video_alt_url_codec: '{v720}'")
    if v1080:
        parts.append(f"video_alt_url2: '{CDN_BASE}/cccc/9000/9001/9001_1080p.mp4/?v-acctoken=TOK1080'")
        parts.append("video_alt_url2_text: '1080p'")
        parts.append("video_alt_url2_fhd: '1'")
        if v1080 != "none":
            parts.append(f"video_alt_url2_codec: '{v1080}'")
    parts.append("preview_url: 'https://www.85po.net/contents/videos_screenshots/9000/9001/preview.jpg'")
    parts.append("timeline_screens_url: 'https://www.85po.net/contents/videos_screenshots/9000/9001/timelines/mp4/182x100/{time}.jpg'")
    parts.append("embed: '1'")
    body = ", ".join(parts)
    return (
        '<div class="player"> <div class="player-holder"> <div class="player-wrap"> <div id="kt_player"></div> '
        '<script type="text/javascript" src="https://www.85po.net/player/kt_player.js?v=15.8.3"></script> '
        f'<script type="text/javascript"> /* <![CDATA[ */ var t41695aa767 = {{ {body} }}; '
        "window['player_obj'] = kt_player('kt_player', 'https://www.85po.net/player/kt_player.swf?v=15.8.3', '100%', '100%', t41695aa767); "
        "/* ]]> */ </script> </div> </div> </div>"
    )


def _page(*, ld: str = "", player: str = "", extra_head: str = "", after_player: str = "") -> str:
    return (
        f"<html><head>{ld}{extra_head}</head><body>"
        '<div class="top"><a class="upload_btn" href="https://www.85po.net/ja/login-required/">アップロード</a></div>'
        f'{player}<div class="video-info"> <div class="btn-favourites"><a href="https://www.85po.net/ja/login-required/">お気に入り</a></div> '
        f"{after_player}</div></body></html>"
    )


def test_parse_video_page_reads_both_sources_and_sorts_best_first():
    info = po85.parse_video_page(_page(ld=_ld(), player=_player()))
    assert info["title"] == "青い灯台の散歩"
    assert info["duration"] == 150
    assert [(f["height"], f["codec"]) for f in info["formats"]] == [(1080, "av1"), (720, "av1"), (480, None)]
    assert all(f["ext"] == "mp4" for f in info["formats"])
    # JSON-LD の contentUrl（トークン無し）はプレイヤーの 720p と同じものなので重複させない
    assert len(info["formats"]) == 3
    # トークン付きのほうを使う
    assert info["formats"][1]["url"].endswith("9001_720p.mp4/?v-acctoken=TOK720")
    assert po85.pick_format(info["formats"])["height"] == 1080


def test_pick_format_prefers_non_av1_at_the_same_height():
    fm = [
        {"url": "https://x/av1", "height": 720, "codec": "av1", "ext": "mp4"},
        {"url": "https://x/h264", "height": 720, "codec": "h264", "ext": "mp4"},
        {"url": "https://x/low", "height": 480, "codec": None, "ext": "mp4"},
    ]
    assert po85.pick_format(fm)["url"] == "https://x/h264"
    # 解像度が高ければ AV1 でも高いほうを取る。同じ高さで AV1 しか無ければ AV1
    assert po85.pick_format(fm[:1] + fm[2:])["url"] == "https://x/av1"
    # codec 不明（None）は AV1 より優先
    mixed = [
        {"url": "https://x/av1", "height": 480, "codec": "av1", "ext": "mp4"},
        {"url": "https://x/unknown", "height": 480, "codec": None, "ext": "mp4"},
    ]
    assert po85.pick_format(mixed)["url"] == "https://x/unknown"
    with pytest.raises(po85.FetchError):
        po85.pick_format([])


def test_parse_video_page_with_only_the_480p_file():
    # 480p しか無い動画は JSON-LD に contentUrl も無い（実サイトで確認）
    info = po85.parse_video_page(_page(ld=_ld(contentUrl=None), player=_player(v720=None, v1080=None)))
    assert [f["height"] for f in info["formats"]] == [480]
    assert info["formats"][0]["codec"] is None


def test_parse_video_page_works_from_json_ld_alone():
    info = po85.parse_video_page(_page(ld=_ld()))
    assert info["title"] == "青い灯台の散歩" and info["duration"] == 150
    (f,) = info["formats"]
    assert f["height"] == 720 and f["ext"] == "mp4"
    assert f["url"] == f"{CDN_BASE}/aaaa/9000/9001/9001_720p.mp4/"


def test_parse_video_page_works_from_the_player_alone():
    meta = '<meta property="video:duration" content="95"/><meta property="og:title" content="og題名"/>'
    info = po85.parse_video_page(_page(player=_player(), extra_head=meta))
    assert info["title"] == "青い灯台の散歩"  # player の video_title
    assert info["duration"] == 95  # meta から
    assert len(info["formats"]) == 3
    # 題名は player にも無ければ og:title
    info2 = po85.parse_video_page(_page(player=_player(title=None), extra_head=meta))
    assert info2["title"] == "og題名"


def test_parse_video_page_handles_labels_and_unknown_codecs():
    player = _player(v720="none", v1080="hevc")
    info = po85.parse_video_page(_page(player=player))
    assert [(f["height"], f["codec"]) for f in info["formats"]] == [(1080, "hevc"), (720, None), (480, None)]


def test_parse_video_page_height_from_k_labels_and_url():
    html = (
        "<script>var t = { video_url: 'https://www.85po.net/ja/get_file/3/a/9/9_2160p.mp4/', video_url_text: '4K', "
        "video_alt_url: 'https://www.85po.net/ja/get_file/3/b/9/9_1440p.mp4/', video_alt_url_text: 'weird', "
        "video_alt_url2: 'https://www.85po.net/ja/get_file/3/c/9/9.mp4/' }; kt_player('kt_player', 'x', '100%', '100%', t);</script>"
    )
    info = po85.parse_video_page(_page(player=html))
    assert [f["height"] for f in info["formats"]] == [2160, 1440, None]


def test_parse_video_page_ignores_broken_json_ld_and_odd_urls():
    bad_ld = '<script type="application/ld+json"> { "name": "壊れた", </script>'
    player = (
        "<script>var t = { video_url: 'http://www.85po.net/ja/get_file/3/a/9/9.mp4/', video_url_text: '480p', "
        "video_alt_url: 'javascript:alert(1)', video_alt_url_text: '720p', "
        "video_alt_url2: 'https://user:pw@www.85po.net/ja/get_file/3/a/9/9.mp4/', "
        "video_alt_url3: '/ja/get_file/3/rel/9/9_1080p.mp4/', video_alt_url3_text: '1080p', "
        "video_title: 'it\\'s \\u9752' }; </script>"
    )
    info = po85.parse_video_page(_page(ld=bad_ld, player=player), "https://www.85po.net/ja/video/9001/x/")
    # https の絶対 URL と、相対（絶対にして https になるもの）だけ残る
    assert [f["url"] for f in info["formats"]] == ["https://www.85po.net/ja/get_file/3/rel/9/9_1080p.mp4/"]
    assert info["title"] == "it's 青"


def test_parse_video_page_accepts_a_list_of_json_ld_blocks():
    other = '<script type="application/ld+json">{"@type": "WebSite", "name": "サイト"}</script>'
    listing = '<script type="application/ld+json">[{"@type": ["Thing", "VideoObject"], "name": "配列の題名", "duration": "PT1H2M3S"}]</script>'
    info = po85.parse_video_page(_page(ld=other + listing, player=_player()))
    assert info["title"] == "配列の題名" and info["duration"] == 3723


@pytest.mark.parametrize(
    "iso,sec",
    [("PT0H2M30S", 150), ("PT1H0M0S", 3600), ("PT45S", 45), ("PT5M", 300), ("P1DT1H", 90000), ("PT", None), ("", None), ("abc", None), (None, None)],
)
def test_iso_duration(iso, sec):
    assert po85._iso_duration(iso) == sec


def test_parse_video_page_without_any_source_is_an_error():
    with pytest.raises(po85.FetchError, match="見つけられません"):
        po85.parse_video_page(_page(player='<div class="player"><p>なにもない</p></div>'))
    # ヘッダやお気に入りのログイン誘導だけでは「会員限定」とは言わない
    with pytest.raises(po85.FetchError) as e:
        po85.parse_video_page("<html><body><a href='/ja/login-required/'>ログイン</a><p>x</p></body></html>")
    assert "ログイン" not in str(e.value)


def test_parse_video_page_login_wall_is_reported_as_such():
    wall = (
        '<div class="player"><div class="message"> この動画は会員限定です。'
        '<a href="https://www.85po.net/ja/login-required/">ログイン</a> してください</div></div>'
    )
    with pytest.raises(po85.FetchError, match="ログインが必要な動画は取れません"):
        po85.parse_video_page(_page(player=wall))


# ---- 動画ページの取得（MockTransport） ----


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_resolve_video_normalizes_and_follows_the_slug_redirect():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path.endswith("/-/"):
            return httpx.Response(301, headers={"location": "https://www.85po.net/ja/video/9001/ao-no-todai/"})
        return httpx.Response(200, text=_page(ld=_ld(), player=_player()))

    with _client(handler) as c:
        info = po85.resolve_video("http://85po.net/ja/video/9001/", client=c)
    assert seen == [
        "https://www.85po.net/ja/video/9001/-/",
        "https://www.85po.net/ja/video/9001/ao-no-todai/",
    ]
    assert info["title"] == "青い灯台の散歩" and len(info["formats"]) == 3


def test_resolve_video_error_messages():
    for code, word in [(404, "見つかりません"), (403, "拒否"), (429, "アクセス過多"), (500, "500")]:
        with _client(lambda req, code=code: httpx.Response(code, text="x")) as c:
            with pytest.raises(po85.FetchError, match=word):
                po85.resolve_video("https://www.85po.net/ja/video/9001/x/", client=c)

    with _client(lambda req: httpx.Response(302, headers={"location": "https://evil.example/"})) as c:
        with pytest.raises(po85.FetchError, match="想定外の場所"):
            po85.resolve_video("https://www.85po.net/ja/video/9001/x/", client=c)

    def boom(req):
        raise httpx.ReadTimeout("slow")

    with _client(boom) as c:
        with pytest.raises(po85.FetchError, match="遅く"):
            po85.resolve_video("https://www.85po.net/ja/video/9001/x/", client=c)

    with pytest.raises(po85.FetchError, match="動画ページの URL ではありません"):
        po85.resolve_video("https://evil.example/ja/video/9001/x/")
