"""TOKYOMOTION の一覧 API (docs/tokyomotion-api.md) の回帰テスト。

ネットには出ない。取得（`fetch_html` / `fetch`）を差し替え、HTML は実サイトの
カードと同じ作りで組んだ**架空の題名**の断片を使う（実在の題名は入れない）。
"""
from __future__ import annotations

import asyncio

import pytest

from app import tokyomotion as tm

CDN = "https://cdn.tokyo-motion.net/media/videos/tmb220"


@pytest.fixture(autouse=True)
def _clean_cache():
    tm.clear_cache()
    yield
    tm.clear_cache()


# ---- HTML の組み立て（実サイトのカードと同じ作り） ----


def _card(
    vid: str,
    title: str,
    *,
    thumb: str | None = None,
    hd: bool = False,
    duration: str = "00:54",
    added: str = "5 days ago",
    views: str = "434576 views",
    rating: str | None = "15%",
    slug: str = "slug",
) -> str:
    src = thumb if thumb is not None else f"{CDN}/{vid}/1.jpg"
    hd_html = '<div class="hd-text-icon">HD</div>' if hd else ""
    rating_html = (
        f'<div class="video-rating pull-right ">'
        f'<i class="fa fa-heart video-rating-heart "></i> <b>{rating}</b></div>'
        if rating is not None
        else ""
    )
    return f"""
        <div class="col-sm-4 col-md-3 col-lg-3">
          <div class="well well-sm">
            <a href="/video/{vid}/{slug}"  class="thumb-popu">
              <div class="thumb-overlay">
                <img src="{src}" title="{title}" alt="{title}" id="rotate_{vid}_20_1" class="img-responsive "/>
                {hd_html}
                <div class="duration">
                  {duration}
                </div>
              </div>
              <span class="video-title title-truncate m-t-5">{title}</span>
            </a>
            <div class="video-added">
              {added}
            </div>
            <div class="video-views pull-left">
              {views}
            </div>
            {rating_html}
            <div class="clearfix"></div>
          </div>
        </div>
    """


def _pager(active: int, last: int, *, has_next: bool = True) -> str:
    base = "https://www.tokyomotion.net/videos?o=mv&amp;t=w&page="
    lis = [f'<li class="active"><span>{active}</span></li>']
    if has_next:
        lis.append(f'<li><a href="{base}{active + 1}" class="prevnext">&raquo;</a></li>')
    lis.append(f'<li class="hidden-xs"><a href="{base}{last}">{last}</a></li>')
    return f'<ul class="pagination">{"".join(lis)}</ul>'


def _page(
    cards: list[str],
    *,
    showing: tuple[int, int, int] | None = (1, 20, 100),
    pager: str = "",
) -> str:
    sh = ""
    if showing:
        a, b, n = showing
        sh = (
            '<div class="well well-sm">Showing <span class="text-white">%d</span> to '
            '<span class="text-white">%d</span> of <span class="text-white">%d</span> videos.</div>'
            % (a, b, n)
        )
    return f"<html><body>{sh}<div class='row'>{''.join(cards)}</div>{pager}<div class='footer'>x</div></body></html>"


# ---- 入力検証と URL ----


@pytest.mark.parametrize(
    "sort,o",
    [("popular", "mv"), ("rated", "tr"), ("favorites", "tf"), ("watching", "bw"),
     ("recent", "mr"), ("longest", "lg")],
)
@pytest.mark.parametrize("period,t", [("today", "t"), ("week", "w"), ("month", "m"), ("all", "a")])
def test_build_url_is_fixed_host(sort, o, period, t):
    url = tm.build_url(sort, period, 3)
    assert url == f"https://www.tokyomotion.net/videos?o={o}&t={t}&page=3"


@pytest.mark.parametrize(
    "sort,period,page",
    [
        ("evil", "week", "1"),
        ("popular", "forever", "1"),
        ("https://evil.example/", "week", "1"),
        ("popular", "week", "0"),
        ("popular", "week", "51"),
        ("popular", "week", "-1"),
        ("popular", "week", "abc"),
        ("popular", "week", "1.5"),
        ("popular", "week", ""),
        ("popular", "week", "１"),
        ("popular", "week", "1&o=x"),
    ],
)
def test_validate_params_rejects(sort, period, page):
    with pytest.raises(tm.ParamError):
        tm.validate_params(sort, period, page)


def test_validate_params_accepts_edges():
    assert tm.validate_params("popular", "week", "1") == ("popular", "week", 1)
    assert tm.validate_params("longest", "all", "50") == ("longest", "all", 50)


# ---- パース ----


def test_parse_fields_of_a_card():
    text = _page(
        [_card("1001", "テスト動画 &amp; 青い灯台", hd=True, duration="01:07:42",
               views="1,234 views", rating="87%", added="2 hours ago",
               slug="テスト動画-青い灯台")],
        pager=_pager(1, 5),
    )
    r = tm.parse_listing(text, 1)
    assert r["page"] == 1
    assert r["hasMore"] is True
    assert r["items"] == [
        {
            "id": "1001",
            # 題名は実体参照を戻す。url は slug を付けず /video/{id}
            "title": "テスト動画 & 青い灯台",
            "url": "https://www.tokyomotion.net/video/1001",
            "thumbnail": f"{CDN}/1001/1.jpg",
            "duration": "01:07:42",
            "views": 1234,
            "rating": 87,
            "added": "2 hours ago",
            "hd": True,
        }
    ]


def test_parse_optional_fields_missing():
    text = _page([_card("1002", "ひなたの散歩", rating=None, views="0 views", duration="00:19")])
    (item,) = tm.parse_listing(text, 1)["items"]
    assert item["rating"] is None
    assert item["hd"] is False
    assert item["views"] == 0
    assert item["duration"] == "00:19"


def test_parse_views_without_comma():
    (item,) = tm.parse_listing(_page([_card("1003", "かもめ", views="434576 views")]), 1)["items"]
    assert item["views"] == 434576


def test_parse_drops_odd_duration():
    (item,) = tm.parse_listing(_page([_card("1004", "あおぞら", duration="LIVE")]), 1)["items"]
    assert item["duration"] is None


@pytest.mark.parametrize(
    "thumb",
    [
        "http://cdn.tokyo-motion.net/media/videos/tmb220/1/1.jpg",  # https でない
        "https://evil.example/media/videos/tmb220/1/1.jpg",  # 別ホスト
        "https://cdn.tokyo-motion.net.evil.example/x.jpg",  # 前方一致のすり抜け
        "https://cdn.tokyo-motion.net@evil.example/x.jpg",  # userinfo のすり抜け
        "https://evil.example/@cdn.tokyo-motion.net/x.jpg",
        "https://cdn.tokyo-motion.net:8443/x.jpg",  # 別ポート
        "//cdn.tokyo-motion.net/x.jpg",  # スキーム無し
        "javascript:alert(1)",
        "data:image/jpeg;base64,AAAA",
    ],
)
def test_parse_rejects_foreign_thumbnail(thumb):
    (item,) = tm.parse_listing(_page([_card("1005", "さざなみ", thumb=thumb)]), 1)["items"]
    # 項目は残し、サムネだけ落とす（保存は URL だけで足りる）
    assert item["id"] == "1005"
    assert item["thumbnail"] is None


def test_parse_dedupes_and_skips_cards_without_video_id():
    bad = '<a href="/user/abc" class="thumb-popu"><span class="video-title">x</span></a>'
    text = _page([_card("1006", "もみじ"), _card("1006", "もみじ"), bad, _card("1007", "いちょう")])
    assert [i["id"] for i in tm.parse_listing(text, 1)["items"]] == ["1006", "1007"]


def test_last_card_does_not_read_the_footer():
    # 最後のカードに rating が無いとき、ページ下部の別の要素を拾わない
    text = _page(
        [_card("1008", "つばめ", rating=None)],
        pager=_pager(1, 2) + '<div class="video-rating"><b>99%</b></div>',
    )
    (item,) = tm.parse_listing(text, 1)["items"]
    assert item["rating"] is None


# ---- hasMore / 空 / 想定外 ----


def test_has_more_from_showing():
    cards = [_card("2001", "あ")]
    assert tm.parse_listing(_page(cards, showing=(1, 20, 100)), 1)["hasMore"] is True
    assert tm.parse_listing(_page(cards, showing=(81, 100, 100)), 5)["hasMore"] is False
    # 桁区切りつき
    assert tm.parse_listing(_page(cards, showing=(1, 20, 5515)).replace("5515", "5,515"), 1)["hasMore"] is True


def test_has_more_falls_back_to_pager_links():
    cards = [_card("2002", "い")]
    assert tm.parse_listing(_page(cards, showing=None, pager=_pager(1, 9)), 1)["hasMore"] is True
    last = _pager(9, 9, has_next=False)
    assert tm.parse_listing(_page(cards, showing=None, pager=last), 9)["hasMore"] is False


def test_has_more_is_false_at_max_page():
    text = _page([_card("2003", "う")], showing=(981, 1000, 99999))
    assert tm.parse_listing(text, tm.MAX_PAGE)["hasMore"] is False


def test_genuinely_empty_is_not_an_error():
    text = '<html><div class="well well-sm"><span class="text-danger">No Videos Found.</span></div></html>'
    assert tm.parse_listing(text, 1) == {"items": [], "page": 1, "hasMore": False}
    zero = _page([], showing=(0, 0, 0))
    assert tm.parse_listing(zero, 1)["items"] == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "<html><body>Please verify your age to continue</body></html>",
        "<html><body><h1>Just a moment...</h1></body></html>",
        # 件数は出ているのにカードが読めない（サイト側の作りが変わった）
        _page([], showing=(1, 20, 100)),
        # カードはあるが /video/{id} が読めない
        _page(['<a href="/watch?x=1" class="thumb-popu"></a>']),
    ],
)
def test_unexpected_html_is_an_error_not_empty(text):
    with pytest.raises(tm.FetchError):
        tm.parse_listing(text, 1)


def test_page_past_the_end_returns_empty_instead_of_the_last_page_again():
    # サイトは範囲外を頼むと最終ページを返す（今のページ番号は頼んだものと違う）
    text = _page([_card("2004", "え")], showing=(5481, 5500, 5515), pager=_pager(276, 276, has_next=False))
    assert tm.parse_listing(text, 50) == {"items": [], "page": 50, "hasMore": False}


# ---- 取得（ネットは差し替え） ----


def test_fetch_requests_only_the_built_url(monkeypatch):
    seen: list[str] = []

    async def fake_html(url: str) -> str:
        seen.append(url)
        return _page([_card("3001", "お")], pager=_pager(2, 3))

    monkeypatch.setattr(tm, "fetch_html", fake_html)
    r = asyncio.run(tm.fetch("rated", "month", 2))
    assert seen == ["https://www.tokyomotion.net/videos?o=tr&t=m&page=2"]
    assert r["page"] == 2 and r["items"][0]["id"] == "3001"
    # 例: 2026-10-12T12:00:00+09:00
    assert r["fetchedAt"].endswith("+09:00") and "T" in r["fetchedAt"]


def test_same_host_redirect_only(monkeypatch):
    assert tm._is_site_url("https://www.tokyomotion.net/videos?o=mv")
    assert not tm._is_site_url("http://www.tokyomotion.net/videos")
    assert not tm._is_site_url("https://evil.example/videos")
    assert not tm._is_site_url("https://www.tokyomotion.net.evil.example/")
    assert not tm._is_site_url("https://www.tokyomotion.net@evil.example/")
    assert not tm._is_site_url("")


# ---- キャッシュ ----


def _fake_fetch(monkeypatch, calls: list, *, delay: float = 0.0, fail: str | None = None):
    async def fake(sort, period, page):
        calls.append((sort, period, page))
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise tm.FetchError(fail)
        return {"items": [], "page": page, "hasMore": False, "fetchedAt": "x"}

    monkeypatch.setattr(tm, "fetch", fake)


def test_cache_hit_within_ttl_and_miss_per_key(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls)
    clock = [1000.0]
    monkeypatch.setattr(tm, "_now", lambda: clock[0])

    async def run():
        await tm.get_ranking("popular", "week", 1)
        await tm.get_ranking("popular", "week", 1)
        await tm.get_ranking("popular", "week", 2)  # キーが違えば別
        await tm.get_ranking("rated", "week", 1)
        assert len(calls) == 3
        clock[0] += tm.CACHE_TTL - 1
        await tm.get_ranking("popular", "week", 1)
        assert len(calls) == 3
        clock[0] += 2  # 10分を過ぎた
        await tm.get_ranking("popular", "week", 1)
        assert len(calls) == 4

    asyncio.run(run())


def test_concurrent_requests_share_one_fetch(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls, delay=0.05)

    async def run():
        rs = await asyncio.gather(*[tm.get_ranking("popular", "week", 1) for _ in range(5)])
        assert len(calls) == 1
        assert all(r is rs[0] for r in rs)
        assert not tm._inflight

    asyncio.run(run())


def test_cancelled_waiter_does_not_cancel_the_shared_fetch(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls, delay=0.05)

    async def run():
        first = asyncio.ensure_future(tm.get_ranking("popular", "week", 1))
        await asyncio.sleep(0.01)
        first.cancel()
        r = await tm.get_ranking("popular", "week", 1)  # 進行中の取得を待つ
        assert r["page"] == 1 and len(calls) == 1

    asyncio.run(run())


def test_failure_is_remembered_briefly_then_retried(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls, fail="取れません")
    clock = [1000.0]
    monkeypatch.setattr(tm, "_now", lambda: clock[0])

    async def run():
        for _ in range(3):
            with pytest.raises(tm.FetchError, match="取れません"):
                await tm.get_ranking("popular", "week", 1)
        assert len(calls) == 1  # サイトを叩き続けない
        clock[0] += tm.FAIL_TTL + 1
        with pytest.raises(tm.FetchError):
            await tm.get_ranking("popular", "week", 1)
        assert len(calls) == 2

    asyncio.run(run())


def test_cache_prunes_expired_entries(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, calls)
    clock = [1000.0]
    monkeypatch.setattr(tm, "_now", lambda: clock[0])

    async def run():
        await tm.get_ranking("popular", "week", 1)
        await tm.get_ranking("popular", "week", 2)
        clock[0] += tm.CACHE_TTL + 1
        await tm.get_ranking("popular", "week", 3)
        assert set(tm._cache) == {("popular", "week", 3)}

    asyncio.run(run())


def test_cache_holds_only_the_parsed_result(monkeypatch):
    async def fake_html(url: str) -> str:
        return _page([_card("3002", "か")])

    monkeypatch.setattr(tm, "fetch_html", fake_html)
    asyncio.run(tm.get_ranking("popular", "week", 1))
    (entry,) = tm._cache.values()
    assert set(entry.value) == {"items", "page", "hasMore", "fetchedAt"}
    assert "<html" not in repr(entry)


# ---- エンドポイント ----


def test_ranking_requires_auth(client):
    assert client.get("/api/tokyomotion/ranking").status_code == 401


@pytest.mark.parametrize(
    "qs",
    ["sort=evil", "period=forever", "page=0", "page=51", "page=abc", "sort=", "page=1.5"],
)
def test_ranking_rejects_bad_params_with_400(client, auth, monkeypatch, qs):
    async def boom(*a):
        raise AssertionError("不正な入力で取得に行ってはいけない")

    monkeypatch.setattr(tm, "fetch", boom)
    r = client.get(f"/api/tokyomotion/ranking?{qs}", headers=auth)
    assert r.status_code == 400
    assert "ください" in r.json()["detail"]


def test_ranking_returns_items_with_defaults(client, auth, monkeypatch):
    seen: list[str] = []

    async def fake_html(url: str) -> str:
        seen.append(url)
        return _page(
            [_card("4001", "きりん", hd=True), _card("4002", "ぞう", rating=None)],
            pager=_pager(1, 3),
        )

    monkeypatch.setattr(tm, "fetch_html", fake_html)
    r = client.get("/api/tokyomotion/ranking", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    # 既定は popular / week / 1
    assert seen == ["https://www.tokyomotion.net/videos?o=mv&t=w&page=1"]
    assert body["page"] == 1 and body["hasMore"] is True
    assert [i["id"] for i in body["items"]] == ["4001", "4002"]
    assert body["items"][0]["hd"] is True and body["items"][1]["rating"] is None
    assert set(body["items"][0]) == {
        "id", "title", "url", "thumbnail", "duration", "views", "rating", "added", "hd",
    }
    assert body["fetchedAt"]

    # 2回目はキャッシュ（取りに行かない）
    assert client.get("/api/tokyomotion/ranking", headers=auth).status_code == 200
    assert len(seen) == 1


def test_ranking_passes_query_to_the_fixed_url(client, auth, monkeypatch):
    seen: list[str] = []

    async def fake_html(url: str) -> str:
        seen.append(url)
        return _page([_card("4003", "さる")])

    monkeypatch.setattr(tm, "fetch_html", fake_html)
    r = client.get("/api/tokyomotion/ranking?sort=longest&period=all&page=7", headers=auth)
    assert r.status_code == 200
    assert r.json()["page"] == 7
    assert seen == ["https://www.tokyomotion.net/videos?o=lg&t=a&page=7"]


def test_ranking_is_502_when_the_source_fails(client, auth, monkeypatch):
    async def fake_html(url: str) -> str:
        raise tm.FetchError("TOKYOMOTION が短時間のアクセス過多として断りました（HTTP 429）")

    monkeypatch.setattr(tm, "fetch_html", fake_html)
    r = client.get("/api/tokyomotion/ranking?sort=rated", headers=auth)
    assert r.status_code == 502
    assert "429" in r.json()["detail"]


def test_ranking_is_502_when_html_is_unexpected(client, auth, monkeypatch):
    async def fake_html(url: str) -> str:
        return "<html><body>age check</body></html>"

    monkeypatch.setattr(tm, "fetch_html", fake_html)
    assert client.get("/api/tokyomotion/ranking", headers=auth).status_code == 502


def test_ranking_empty_is_200_not_502(client, auth, monkeypatch):
    async def fake_html(url: str) -> str:
        return '<html><span class="text-danger">No Videos Found.</span></html>'

    monkeypatch.setattr(tm, "fetch_html", fake_html)
    r = client.get("/api/tokyomotion/ranking?period=today", headers=auth)
    assert r.status_code == 200
    assert r.json()["items"] == [] and r.json()["hasMore"] is False
