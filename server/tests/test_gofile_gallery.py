"""Gofileサムネイルギャラリー (docs/refs/gofile-thumbnail-gallery-spec.md) の回帰テスト。

実際の取得(gofile-douga.com)・検証(Playwright)は gofile-worker コンテナの
仕事なので、ここでは api コンテナ側の薄い層（一覧・手動更新・
再検証要求のエンドポイントと、その裏の app/gofile/store.py）だけを見る。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.gofile import store


@pytest.fixture(autouse=True)
def _clean_gofile_tables():
    """テスト間で gofile_gallery_items / meta の gofile系キーを持ち越さない。"""
    with db.tx() as c:
        c.execute("DELETE FROM gofile_gallery_items")
        c.execute("DELETE FROM meta WHERE key LIKE 'gofile_%'")
    yield


def _insert(code: str, *, link_status="pending", thumb_status="pending", **extra) -> int:
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO gofile_gallery_items
                 (gofile_code, gofile_url, title, thumbnail_url, source_posted_at,
                  file_count, total_size_bytes, link_status, thumbnail_status,
                  link_checked_at, link_check_expires_at, rank_24h,
                  source_x_account_id, profile_image_url, hidden_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                code, f"https://gofile.io/d/{code}", extra.get("title"),
                extra.get("thumbnail_url", f"https://cdn.gofile.io/t/{code}.jpg"),
                extra.get("source_posted_at"), extra.get("file_count"),
                extra.get("total_size_bytes"), link_status, thumb_status,
                extra.get("link_checked_at"),
                extra.get("link_check_expires_at"), extra.get("rank_24h"),
                extra.get("source_x_account_id"), extra.get("profile_image_url"),
                extra.get("hidden_at"),
            ),
        )
        return cur.lastrowid


def _sqlite_now(minutes_ago: int = 0) -> str:
    """SQLite の `datetime('now')` と**同じ書式**の時刻。

    ★書式を揃えること★ 表示条件は文字列比較（`link_checked_at >
    datetime('now','-N minutes')`）なので、`2026-09-04T10:00:00Z` のような
    ISO 形式を混ぜると区切り文字（`T` > 空白）のぶんだけ新しい側に化ける。
    """
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _valid(**extra) -> dict:
    """表示条件を満たす valid+valid の既定値。

    ★`link_checked_at` を必ず入れること★ 表示の期限は**保存された
    `link_check_expires_at` ではなく「検証時刻＋いまの TTL」**で判定する
    （`store._display_where`。2026-09-04 に変更）。本番では
    `record_check_result` が両方を必ず同時に書くので、片方だけの行は存在しない。
    """
    return {"link_status": "valid", "thumb_status": "valid",
            "link_checked_at": _sqlite_now(),
            "link_check_expires_at": "2099-01-01T00:00:00Z", **extra}


# ---------------------------------------------------------------- 認証


def test_no_token_is_401(client):
    assert client.get("/api/gofile-gallery").status_code == 401
    assert client.post("/api/admin/gofile-gallery/sync").status_code == 401
    assert client.post("/api/gofile-gallery/1/report-broken").status_code == 401


# ---------------------------------------------------------------- 一覧 (§8.1)


def test_list_empty(client, auth):
    r = client.get("/api/gofile-gallery", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body == {"items": [], "nextCursor": None, "lastSyncedAt": None}


def test_list_only_shows_valid_and_unexpired(client, auth):
    # 表示される: valid + thumbnail valid + 期限内
    _insert(
        "SHOWME", link_status="valid", thumb_status="valid",
        source_posted_at="2026-07-27T10:00:00Z",
        link_checked_at=_sqlite_now(),
        link_check_expires_at="2099-01-01T00:00:00Z",
        title="表示される",
    )
    # 表示されない: valid だが期限切れ
    _insert(
        "EXPIRED", link_status="valid", thumb_status="valid",
        source_posted_at="2026-07-27T11:00:00Z",
        link_checked_at=_sqlite_now(minutes_ago=60 * 24 * 30),
        link_check_expires_at="2000-01-01T00:00:00Z",
    )
    # 表示されない: サムネイルだけ無効
    _insert(
        "BADTHUMB", link_status="valid", thumb_status="invalid",
        source_posted_at="2026-07-27T12:00:00Z",
        link_checked_at=_sqlite_now(),
        link_check_expires_at="2099-01-01T00:00:00Z",
    )
    # 表示されない: リンク無効
    _insert("INVALIDLINK", link_status="invalid", thumb_status="valid")
    # 表示されない: 未検証
    _insert("PENDING", link_status="pending", thumb_status="pending")

    r = client.get("/api/gofile-gallery", headers=auth)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["gofileUrl"] for i in items] == ["https://gofile.io/d/SHOWME"]
    assert items[0]["title"] == "表示される"


def test_list_falls_back_to_gofile_code_title(client, auth):
    _insert(
        "NOTITLE", link_status="valid", thumb_status="valid",
        link_checked_at=_sqlite_now(),
        link_check_expires_at="2099-01-01T00:00:00Z", title=None,
    )
    r = client.get("/api/gofile-gallery", headers=auth)
    assert r.json()["items"][0]["title"] == "Gofile NOTITLE"


def test_list_order_and_pagination(client, auth):
    for i, code in enumerate(["A", "B", "C"]):
        _insert(
            code, link_status="valid", thumb_status="valid",
            source_posted_at=f"2026-07-2{7-i}T00:00:00Z",
            link_checked_at=_sqlite_now(),
            link_check_expires_at="2099-01-01T00:00:00Z",
        )
    # 新着順 (source_posted_at 降順) = A, B, C
    r = client.get("/api/gofile-gallery?limit=1", headers=auth)
    body = r.json()
    assert [i["gofileUrl"] for i in body["items"]] == ["https://gofile.io/d/A"]
    assert body["nextCursor"] is not None

    r2 = client.get(f"/api/gofile-gallery?limit=1&cursor={body['nextCursor']}", headers=auth)
    body2 = r2.json()
    assert [i["gofileUrl"] for i in body2["items"]] == ["https://gofile.io/d/B"]

    r3 = client.get(f"/api/gofile-gallery?limit=10&cursor={body2['nextCursor']}", headers=auth)
    body3 = r3.json()
    assert [i["gofileUrl"] for i in body3["items"]] == ["https://gofile.io/d/C"]
    assert body3["nextCursor"] is None


def test_list_reports_last_synced_at(client, auth):
    store.mark_synced()
    r = client.get("/api/gofile-gallery", headers=auth)
    assert r.json()["lastSyncedAt"] is not None


def test_candidates_for_check_prioritizes_ranked_items():
    """rank_24h付きは、同じ優先層(pending/valid/invalid)の中でも新着順より先に
    再検証の順番が回ってくる。人気タブが再検証バッチの取り合いで万年TTL切れに
    ならないための優先度（2026-07-30に実運用で踏んだ）。"""
    _insert("PLAINPENDING", link_status="pending", thumb_status="pending",
            source_posted_at="2026-07-30T00:00:00Z")
    _insert("RANKEDPENDING", link_status="pending", thumb_status="pending",
            source_posted_at="2026-07-01T00:00:00Z", rank_24h=5)
    codes = [c["gofile_code"] for c in store.candidates_for_check(10)]
    assert codes.index("RANKEDPENDING") < codes.index("PLAINPENDING")

    _insert("PLAINVALID", **_valid(source_posted_at="2026-07-30T00:00:00Z"))
    _insert("RANKEDVALID", **_valid(source_posted_at="2026-07-01T00:00:00Z"), rank_24h=9)
    codes2 = [c["gofile_code"] for c in store.candidates_for_check(10)]
    assert codes2.index("RANKEDVALID") < codes2.index("PLAINVALID")


# ---------------------------------------------------------------- 再検証要求 (§9.3)


def test_report_broken_marks_priority_recheck(client, auth):
    item_id = _insert("BROKEN", link_status="valid", thumb_status="valid")
    r = client.post(f"/api/gofile-gallery/{item_id}/report-broken", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    row = db.conn().execute(
        "SELECT priority_recheck FROM gofile_gallery_items WHERE id=?", (item_id,)
    ).fetchone()
    assert row["priority_recheck"] == 1


def test_report_broken_missing_id_is_404(client, auth):
    r = client.post("/api/gofile-gallery/999999/report-broken", headers=auth)
    assert r.status_code == 404


# ---------------------------------------------------------------- 手動更新 (§8.2)


def test_request_sync_accepted(client, auth):
    r = client.post("/api/admin/gofile-gallery/sync", headers=auth)
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert body["job_id"]
    # gofile-worker が拾う想定の要求が実際に積まれている
    assert store.pop_requested_sync() == body["job_id"]


def test_request_sync_conflicts_while_running(client, auth):
    job_id = store.acquire_lock()
    assert job_id is not None
    try:
        r = client.post("/api/admin/gofile-gallery/sync", headers=auth)
        assert r.status_code == 409
    finally:
        store.release_lock()


# ---------------------------------------------------------------- 人気ランキング(24h) (v13)


def test_no_token_is_401_rankings_and_by_account(client):
    assert client.get("/api/gofile-gallery/rankings").status_code == 401
    assert client.get("/api/gofile-gallery/by-account/someone").status_code == 401


def _ranking_item(code: str, rank: int, **extra) -> dict:
    return {
        "source_item_id": None,
        "gofile_code": code,
        "gofile_url": f"https://gofile.io/d/{code}",
        "title": extra.get("title"),
        "thumbnail_url": f"https://cdn.gofile.io/t/{code}.jpg",
        "thumbnail_urls_json": None,
        "source_posted_at": extra.get("source_posted_at"),
        "file_count": None,
        "total_size_bytes": None,
        "source_status": None,
        "source_x_account_id": extra.get("source_x_account_id"),
        "profile_image_url": extra.get("profile_image_url"),
        "ranking_rank": rank,
    }


def test_upsert_ranking_items_sets_rank_and_clears_on_drop():
    store.upsert_ranking_items([_ranking_item("A", 1), _ranking_item("B", 2)])
    rows = {
        r["gofile_code"]: r["rank_24h"]
        for r in db.rows_to_dicts(
            db.conn().execute("SELECT gofile_code, rank_24h FROM gofile_gallery_items").fetchall()
        )
    }
    assert rows == {"A": 1, "B": 2}

    # 次の同期で B が圏外に落ち、C が新規に入る
    store.upsert_ranking_items([_ranking_item("A", 1), _ranking_item("C", 2)])
    rows = {
        r["gofile_code"]: r["rank_24h"]
        for r in db.rows_to_dicts(
            db.conn().execute("SELECT gofile_code, rank_24h FROM gofile_gallery_items").fetchall()
        )
    }
    assert rows == {"A": 1, "B": None, "C": 2}


def test_upsert_ranking_items_empty_clears_all_ranks():
    store.upsert_ranking_items([_ranking_item("A", 1)])
    store.upsert_ranking_items([])
    row = db.conn().execute(
        "SELECT rank_24h FROM gofile_gallery_items WHERE gofile_code='A'"
    ).fetchone()
    assert row["rank_24h"] is None


def test_upsert_items_persists_account_fields():
    store.upsert_items([_ranking_item("A", 1, source_x_account_id="acc1",
                                       profile_image_url="https://pbs.twimg.com/a.jpg")])
    row = db.conn().execute(
        "SELECT source_x_account_id, profile_image_url FROM gofile_gallery_items WHERE gofile_code='A'"
    ).fetchone()
    assert row["source_x_account_id"] == "acc1"
    assert row["profile_image_url"] == "https://pbs.twimg.com/a.jpg"


def test_list_rankings_order_and_cursor(client, auth):
    _insert("R1", rank_24h=1, **_valid())
    _insert("R2", rank_24h=2, **_valid())
    _insert("R3", rank_24h=3, **_valid())
    # ランク圏外（rank_24h無し）は人気タブに出ない
    _insert("NOTRANKED", **_valid())

    r = client.get("/api/gofile-gallery/rankings?limit=2", headers=auth)
    body = r.json()
    assert [i["gofileUrl"] for i in body["items"]] == [
        "https://gofile.io/d/R1", "https://gofile.io/d/R2",
    ]
    assert body["items"][0]["rank"] == 1
    assert body["nextCursor"] is not None

    r2 = client.get(f"/api/gofile-gallery/rankings?limit=10&cursor={body['nextCursor']}", headers=auth)
    body2 = r2.json()
    assert [i["gofileUrl"] for i in body2["items"]] == ["https://gofile.io/d/R3"]
    assert body2["nextCursor"] is None


def test_list_rankings_excludes_invalid_or_expired():
    _insert("VALIDRANKED", rank_24h=1, **_valid())
    _insert("EXPIREDRANKED", rank_24h=2, link_status="valid", thumb_status="valid",
            link_checked_at=_sqlite_now(minutes_ago=60 * 24 * 30),
            link_check_expires_at="2000-01-01T00:00:00Z")
    r = store.list_ranking(None, 30)
    assert [i["gofileUrl"] for i in r["items"]] == ["https://gofile.io/d/VALIDRANKED"]


# ---------------------------------------------------------------- 投稿者別一覧 (v13)


def test_list_by_account_filters_and_orders(client, auth):
    _insert("P1", source_x_account_id="alice", source_posted_at="2026-07-27T10:00:00Z", **_valid())
    _insert("P2", source_x_account_id="alice", source_posted_at="2026-07-28T10:00:00Z", **_valid())
    _insert("P3", source_x_account_id="bob", source_posted_at="2026-07-29T10:00:00Z", **_valid())

    r = client.get("/api/gofile-gallery/by-account/alice", headers=auth)
    body = r.json()
    # 新着順(source_posted_at 降順)のまま、他アカウントは混ざらない
    assert [i["gofileUrl"] for i in body["items"]] == [
        "https://gofile.io/d/P2", "https://gofile.io/d/P1",
    ]


def test_list_by_account_excludes_unverified():
    _insert("UNVERIFIED", source_x_account_id="alice", link_status="pending", thumb_status="pending")
    r = store.list_by_account("alice", None, 30)
    assert r["items"] == []


def test_list_by_account_empty_for_unknown_account(client, auth):
    r = client.get("/api/gofile-gallery/by-account/nobody", headers=auth)
    assert r.json()["items"] == []


def test_mark_account_priority_recheck_sets_flag_excludes_hidden():
    id_shown = _insert("SHOWN1", source_x_account_id="alice", link_status="invalid")
    id_hidden = _insert("HIDDEN1", source_x_account_id="alice", link_status="invalid",
                         hidden_at="2026-07-29T00:00:00Z")
    _insert("OTHERACC", source_x_account_id="bob", link_status="invalid")

    n = store.mark_account_priority_recheck("alice")
    assert n == 1

    def _flag(item_id: int) -> int:
        return db.conn().execute(
            "SELECT priority_recheck FROM gofile_gallery_items WHERE id=?", (item_id,)
        ).fetchone()["priority_recheck"]

    assert _flag(id_shown) == 1
    assert _flag(id_hidden) == 0  # 非表示化済みは対象外


def test_list_by_account_nudges_priority_recheck_when_thin(client, auth):
    # 検証済みは1件だけ(=閾値未満)。他の行はまだpendingで一覧には出ない
    _insert("THIN1", source_x_account_id="carol", **_valid())
    id_pending = _insert("THIN2", source_x_account_id="carol", link_status="pending")

    r = client.get("/api/gofile-gallery/by-account/carol", headers=auth)
    assert len(r.json()["items"]) == 1
    row = db.conn().execute(
        "SELECT priority_recheck FROM gofile_gallery_items WHERE id=?", (id_pending,)
    ).fetchone()
    assert row["priority_recheck"] == 1


def test_list_by_account_does_not_nudge_on_later_pages(client, auth):
    id_pending = _insert("PAGE2PENDING", source_x_account_id="dave", link_status="pending")
    # cursor付き(2ページ目扱い)のときは催促しない(壊れたカーソルでも
    # 「cursorパラメータが付いているか」だけを見るルータ側の判定は変わらない)
    client.get("/api/gofile-gallery/by-account/dave?cursor=anything", headers=auth)
    row = db.conn().execute(
        "SELECT priority_recheck FROM gofile_gallery_items WHERE id=?", (id_pending,)
    ).fetchone()
    assert row["priority_recheck"] == 0
