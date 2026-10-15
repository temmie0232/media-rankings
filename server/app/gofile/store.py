"""DBアクセス層。一覧・upsert・ジョブロック・統計記録をここに集約する。

api コンテナ（routers/gofile_gallery.py）と gofile-worker コンテナの
両方から import される。Playwright には依存しない。
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import settings

# ---------------------------------------------------------------- meta キー
#
# ジョブロック・手動更新の受け口・最終同期時刻は、schema_version や
# token_version と同じ meta テーブル（key/value）に相乗りさせる。
# 専用テーブルにしない理由: 1行だけの状態を持つのにテーブルを増やすと
# init_db のマイグレーション対象が無駄に増える
_LOCK_KEY = "gofile_job_running"
_LOCK_STARTED_KEY = "gofile_job_started_at"
_REQUEST_KEY = "gofile_sync_requested_job_id"
_LAST_SYNC_KEY = "gofile_last_sync_at"
_BATCH_SIZE_KEY = "gofile_reverify_batch_size"
_BATCH_SINCE_KEY = "gofile_reverify_batch_since"
# 長押し保存が使う gofile のゲストセッション (download.py)
_GUEST_TOKEN_KEY = "gofile_guest_token"
_GUEST_TOKEN_AT_KEY = "gofile_guest_token_created_at"

_UNKNOWN_RETRY_DELAYS_MIN = (5, 15, 30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_meta(key: str) -> str | None:
    row = db.conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(key: str, value: str) -> None:
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))


def _del_meta(key: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM meta WHERE key=?", (key,))


# ---------------------------------------------------------------- 取得・upsert


def existing_codes() -> set[str]:
    rows = db.conn().execute("SELECT gofile_code FROM gofile_gallery_items").fetchall()
    return {r["gofile_code"] for r in rows}


def upsert_items(items: list[dict]) -> tuple[int, int]:
    """§5.3 重複判定。gofile_code が既知なら更新、無ければ新規。

    ★hidden_at には触れない★ 非表示は「うちの検証で無効と分かった」印であって
    「元サイトの一覧にまだ載っているか」とは別の話。取得の度に消してしまうと
    無効判定の記憶が新着取得のたびに失われる。
    """
    created = updated = 0
    with db.tx() as c:
        for it in items:
            thumbs_json = (
                json.dumps(it["thumbnail_urls_json"], ensure_ascii=False)
                if it.get("thumbnail_urls_json")
                else None
            )
            row = c.execute(
                "SELECT id FROM gofile_gallery_items WHERE gofile_code = ?",
                (it["gofile_code"],),
            ).fetchone()
            if row:
                c.execute(
                    """UPDATE gofile_gallery_items SET
                         source_item_id = COALESCE(?, source_item_id),
                         gofile_url = ?, title = ?, thumbnail_url = ?,
                         thumbnail_urls_json = ?, source_posted_at = ?,
                         file_count = ?, total_size_bytes = ?, source_status = ?,
                         source_x_account_id = ?, profile_image_url = ?,
                         last_seen_at = datetime('now'), updated_at = datetime('now')
                       WHERE id = ?""",
                    (
                        it["source_item_id"], it["gofile_url"], it["title"],
                        it["thumbnail_url"], thumbs_json, it["source_posted_at"],
                        it["file_count"], it["total_size_bytes"], it["source_status"],
                        it["source_x_account_id"], it["profile_image_url"],
                        row["id"],
                    ),
                )
                updated += 1
            else:
                c.execute(
                    """INSERT INTO gofile_gallery_items
                         (source_item_id, gofile_code, gofile_url, title,
                          thumbnail_url, thumbnail_urls_json, source_posted_at,
                          file_count, total_size_bytes, source_status,
                          source_x_account_id, profile_image_url)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        it["source_item_id"], it["gofile_code"], it["gofile_url"],
                        it["title"], it["thumbnail_url"], thumbs_json,
                        it["source_posted_at"], it["file_count"],
                        it["total_size_bytes"], it["source_status"],
                        it["source_x_account_id"], it["profile_image_url"],
                    ),
                )
                created += 1
    return created, updated


def upsert_ranking_items(items: list[dict]) -> tuple[int, int]:
    """24hランキングの反映 (v13)。内容のupsertは `upsert_items` をそのまま使い、
    その後 `rank_24h` を更新する。

    ★今回のフェッチに無かった行は rank_24h を NULL に戻す★
    §6.4「安全側に倒す」と同じ考え方 — ランキングは一覧に載っている間だけ
    人気タブに出す。圏外に落ちても行自体（新着経由の検証結果等）は消さない。
    """
    created, updated = upsert_items(items)

    codes = [it["gofile_code"] for it in items]
    with db.tx() as c:
        for it in items:
            c.execute(
                "UPDATE gofile_gallery_items SET rank_24h = ? WHERE gofile_code = ?",
                (it["ranking_rank"], it["gofile_code"]),
            )
        if codes:
            placeholders = ",".join("?" * len(codes))
            c.execute(
                f"UPDATE gofile_gallery_items SET rank_24h = NULL "
                f"WHERE rank_24h IS NOT NULL AND gofile_code NOT IN ({placeholders})",
                codes,
            )
        else:
            c.execute(
                "UPDATE gofile_gallery_items SET rank_24h = NULL WHERE rank_24h IS NOT NULL"
            )
    return created, updated


# ---------------------------------------------------------------- 検証結果の反映


def candidates_for_check(limit: int) -> list[dict]:
    """このジョブ実行で検証すべき行を選ぶ (§6.4)。優先順位:

      1. priority_recheck=1 （画面からの再検証要求, §9.3）
      2. pending （新規取得直後。表示させるにはまず検証が要る）
      3. rank_24h が付いている（人気タブに表示中/表示すべきもの。v13）
      4. 現在 valid（表示中）で hidden_at が立っていないもの
         （§6.4「表示中項目: 30分ごとに再検証」。TTLが切れているかは問わず、
         このジョブが唯一の再検証機会なので毎回対象にする）
      5. unknown / invalid で再試行時刻が来たもの

    ★rank_24h を pending の次に置く理由★ 新着順(source_posted_at)で
    優先度を付けると、人気ランキングは必ずしも最新順ではないため、
    再検証バッチ（既定5件/回）が「最近の投稿」ばかりを拾い続けて
    人気タブの項目が万年TTL切れのまま埋もれる（実際に踏んだ）。
    rank_24h を専用の優先層にすることで、圏内である限り確実に
    再検証の順番が回ってくるようにする。

    表示上限件数を超えた古い項目は候補にしない（§6.4「上限外の項目は
    画面に表示しない」＝検証する意味も無い）。
    """
    rows = db.conn().execute(
        f"""SELECT id, gofile_code, gofile_url, thumbnail_url, link_status,
                   link_check_attempts, priority_recheck
           FROM gofile_gallery_items
           WHERE priority_recheck = 1
              OR link_status = 'pending'
              OR (link_status = 'valid' AND hidden_at IS NULL)
              OR (link_status IN ('invalid','unknown')
                  AND link_check_expires_at IS NOT NULL
                  AND link_check_expires_at <= datetime('now'))
           ORDER BY
             priority_recheck DESC,
             CASE link_status WHEN 'pending' THEN 0 ELSE 1 END,
             CASE WHEN rank_24h IS NOT NULL THEN 0 ELSE 1 END,
             COALESCE(source_posted_at,'') DESC,
             first_seen_at DESC
           LIMIT {int(settings.gofile.display_limit)}
        """
    ).fetchall()
    items = db.rows_to_dicts(rows)
    return items[: max(0, limit)]


def record_check_result(item_id: int, *, link_status: str, thumb_status: str) -> None:
    """検証結果を反映する。§6.4 の再検証スケジュールとTTLをここで一元管理する。

    ★link_check_expires_at の意味は link_status によって変わる★
      valid   … この時刻まで表示してよい（TTL）
      invalid … この時刻に1回だけ再確認する（それ以降はNULL=もう自動では見ない）
      unknown … この時刻に再試行する（最大試行回数を超えたらNULL）
    表示条件（§6.1）は link_status='valid' も同時に見るので、
    invalid/unknown のときにこの列を「期限」ではなく「次回予定」に
    読み替えても表示ロジックには影響しない。
    """
    g = settings.gofile
    row = db.conn().execute(
        "SELECT link_check_attempts FROM gofile_gallery_items WHERE id=?", (item_id,)
    ).fetchone()
    if row is None:
        return
    attempts = row["link_check_attempts"] or 0

    if link_status == "valid":
        expires_expr = f"+{g.link_ttl_minutes} minutes"
        next_attempts = 0
        hidden_clear = True
    elif link_status == "invalid":
        if attempts == 0:
            expires_expr = f"+{g.invalid_recheck_hours} hours"
            next_attempts = 1
        else:
            expires_expr = None  # §6.4「再び無効なら非表示を継続」＝以後は自動再検証しない
            next_attempts = attempts
        hidden_clear = False
    else:  # unknown
        next_attempts = attempts + 1
        if next_attempts > g.unknown_retry_max:
            expires_expr = None
        else:
            idx = min(next_attempts, len(_UNKNOWN_RETRY_DELAYS_MIN)) - 1
            expires_expr = f"+{_UNKNOWN_RETRY_DELAYS_MIN[idx]} minutes"
        hidden_clear = False

    with db.tx() as c:
        c.execute(
            f"""UPDATE gofile_gallery_items SET
                  link_status = ?,
                  thumbnail_status = ?,
                  link_checked_at = datetime('now'),
                  link_check_expires_at = {"NULL" if expires_expr is None else "datetime('now', ?)"},
                  link_check_attempts = ?,
                  priority_recheck = 0,
                  hidden_at = {"NULL" if hidden_clear else "COALESCE(hidden_at, datetime('now'))"},
                  updated_at = datetime('now')
                WHERE id = ?""",
            (
                *([link_status, thumb_status] if expires_expr is None
                  else [link_status, thumb_status, expires_expr]),
                next_attempts,
                item_id,
            ),
        )


def get_gofile_url(item_id: int) -> str | None:
    """長押し→保存 が使う。DBに保存済みの gofile_url を返す（無ければ None）。

    画面から渡ってきた URL を信用せず、必ず自分の持ち物から引き直す
    （§11 の考え方と同じ。信用の起点はDBの側に置く）。
    """
    row = db.conn().execute(
        "SELECT gofile_url FROM gofile_gallery_items WHERE id=?", (item_id,)
    ).fetchone()
    return row["gofile_url"] if row else None


def get_saved_download(code: str) -> dict | None:
    """既に保存したことがあるか (`gofile_code` 単位)。長押し保存とURL貼り付け
    保存の両方が共有する保存履歴（ギャラリーのDBには載らない任意のURLも
    保存できるため、`gofile_gallery_items` とは別の表に持つ）。
    """
    row = db.conn().execute(
        "SELECT gofile_url, dest_path, save_count, first_saved_at, last_saved_at "
        "FROM gofile_saved_downloads WHERE gofile_code=?",
        (code,),
    ).fetchone()
    return dict(row) if row else None


def record_saved_download(code: str, url: str, dest_path: str) -> None:
    """ダウンロードが成功した後に呼ぶ。既存なら件数を増やして最新の保存先に更新する。"""
    with db.tx() as c:
        row = c.execute(
            "SELECT id FROM gofile_saved_downloads WHERE gofile_code=?", (code,)
        ).fetchone()
        if row:
            c.execute(
                "UPDATE gofile_saved_downloads SET save_count = save_count + 1, "
                "dest_path = ?, last_saved_at = datetime('now') WHERE id = ?",
                (dest_path, row["id"]),
            )
        else:
            c.execute(
                "INSERT INTO gofile_saved_downloads (gofile_code, gofile_url, dest_path) "
                "VALUES (?,?,?)",
                (code, url, dest_path),
            )


def mark_priority_recheck(item_id: int) -> bool:
    """画面からの「読み込みエラー→再検証要求」(§9.3)。存在すれば立てる。"""
    with db.tx() as c:
        cur = c.execute(
            "UPDATE gofile_gallery_items SET priority_recheck = 1, updated_at = datetime('now') "
            "WHERE id = ?",
            (item_id,),
        )
        return cur.rowcount > 0


def mark_account_priority_recheck(x_account_id: str) -> int:
    """投稿者別一覧 (v13) 用。この投稿者の行をまとめて最優先の再検証対象にする。

    ★なぜ要るか★ list_by_account は他の一覧と同じ「valid かつ未失効」条件で
    絞る。再検証バッチ（既定10件/30分）は新着順・人気ランキング優先で回すので、
    直近の投稿でも人気ランキング入りでもない古い投稿は順番が回ってこず、
    TTL(30分)切れのまま表示されない状態が続く（実運用で発覚）。
    一覧を開いた投稿者の行だけ priority_recheck を立てて、次の検証サイクルで
    確実に順番を回す（`mark_priority_recheck` と同じ仕組みを口座単位に広げたもの）。
    hidden（非表示化済み＝リンク切れ確定）の行は対象外。
    """
    with db.tx() as c:
        cur = c.execute(
            "UPDATE gofile_gallery_items SET priority_recheck = 1, updated_at = datetime('now') "
            "WHERE source_x_account_id = ? AND hidden_at IS NULL AND priority_recheck = 0",
            (x_account_id,),
        )
        return cur.rowcount


def cleanup_hidden() -> int:
    """§13 非表示データ保持期間を過ぎた行を消す。"""
    days = settings.gofile.hidden_retention_days
    with db.tx() as c:
        cur = c.execute(
            "DELETE FROM gofile_gallery_items WHERE hidden_at IS NOT NULL "
            "AND hidden_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        return cur.rowcount


# ---------------------------------------------------------------- 一覧 (§8.1)


def _encode_cursor(parts: list[str]) -> str:
    # URLクエリにそのまま乗せる値なので、区切り文字に生の制御文字を使わず
    # base64url にする（`\x1f` 区切りのままだと httpx 等の一部クライアントが
    # 「非表示ASCII文字を含むURL」として弾く。実測で踏んだ）。
    # `list_display`（3パーツ）と `list_ranking`（2パーツ）で共有する汎用形
    raw = "\x1f".join(parts)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None, n: int) -> list[str] | None:
    """base64url を復号する。ちょうど `n` パーツに割れなければ壊れたカーソル扱い（None）。"""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    parts = raw.split("\x1f")
    return parts if len(parts) == n else None


def _decode_recency_cursor(cursor: str | None) -> tuple[str, str, int] | None:
    """`list_display`/`list_by_account` が共有する (source_posted_at, first_seen_at, id) カーソル。"""
    parts = _decode_cursor(cursor, 3)
    if parts is None:
        return None
    posted, seen, raw_id = parts
    try:
        return posted, seen, int(raw_id)
    except ValueError:
        return None


def _row_to_item(r: dict, *, rank: int | None) -> dict:
    return {
        "id": r["id"],
        "title": r["title"] or f"Gofile {r['gofile_code']}",
        "thumbnailUrl": r["thumbnail_url"],
        "gofileUrl": r["gofile_url"],
        "sourcePostedAt": r["source_posted_at"],
        "fileCount": r["file_count"],
        "totalSizeBytes": r["total_size_bytes"],
        "sourceXAccountId": r["source_x_account_id"],
        "profileImageUrl": r["profile_image_url"],
        "rank": rank,
    }


def _display_where() -> list[str]:
    """画面に出してよい行の条件。**ここ1箇所だけ**（§6.1）。

    ★期限は保存値ではなく「検証時刻＋いまの TTL」で見る★（2026-09-04）
    `link_check_expires_at` は検証したその瞬間に「その時刻＋TTL」として
    焼き込まれる。つまり `GOFILE_LINK_TTL_MINUTES` を変えても
    **過去の行には一切効かない**。30分→24時間にしたのに表示が 0 件のまま
    だったのがこれ（4,881件すべてが「30分で切れる」印を持っていた）。

    しかもこの列は `link_status` で意味が変わる（valid=期限 /
    invalid・unknown=次回の予定 — `record_check_result` 参照）ので、
    期限として読めるのは valid のときだけという二重の分かりにくさがあった。
    検証時刻から測れば、設定がそのまま生きた値になる。

    ★3箇所にコピーしないこと★ 以前は新着・人気・投稿者別に同じ4行が
    貼られていた（docs は「1箇所だけ」と書いてあったのに実際は3箇所）。
    片方だけ直すと「新着には出るのに投稿者別には出ない」になる。
    """
    ttl = int(settings.gofile.link_ttl_minutes)
    return [
        "link_status = 'valid'",
        "thumbnail_status = 'valid'",
        "link_checked_at IS NOT NULL",
        f"link_checked_at > datetime('now', '-{ttl} minutes')",
    ]


def list_display(cursor: str | None, limit: int) -> dict:
    """§8.1 一覧取得（新着順）。表示条件と並び順はここに1箇所だけ実装する。"""
    limit = max(1, min(limit, 100))
    after = _decode_recency_cursor(cursor)

    where = [
        *_display_where(),
    ]
    params: list = []
    if after is not None:
        posted, seen, rid = after
        where.append(
            "(COALESCE(source_posted_at,'') < ? OR "
            "(COALESCE(source_posted_at,'') = ? AND first_seen_at < ?) OR "
            "(COALESCE(source_posted_at,'') = ? AND first_seen_at = ? AND id < ?))"
        )
        params += [posted, posted, seen, posted, seen, rid]

    sql = f"""
        WITH ranked AS (
            SELECT id, title, gofile_code, gofile_url, thumbnail_url,
                   source_posted_at, first_seen_at, file_count, total_size_bytes,
                   source_x_account_id, profile_image_url
            FROM gofile_gallery_items
            WHERE {' AND '.join(where[:4])}
            ORDER BY COALESCE(source_posted_at,'') DESC, first_seen_at DESC, id DESC
            LIMIT ?
        )
        SELECT * FROM ranked
        {"WHERE " + where[4] if len(where) > 4 else ""}
        ORDER BY COALESCE(source_posted_at,'') DESC, first_seen_at DESC, id DESC
        LIMIT ?
    """
    display_cap = settings.gofile.display_limit
    args = [display_cap, *params, limit + 1]
    rows = db.rows_to_dicts(db.conn().execute(sql, args).fetchall())

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_cursor([rows[-1]["source_posted_at"] or "", rows[-1]["first_seen_at"], str(rows[-1]["id"])])
        if has_more and rows
        else None
    )

    items = [_row_to_item(r, rank=None) for r in rows]
    return {"items": items, "nextCursor": next_cursor, "lastSyncedAt": last_sync_at()}


def list_ranking(cursor: str | None, limit: int) -> dict:
    """24h人気ランキング一覧 (v13)。表示条件は `list_display` と同じに
    `rank_24h IS NOT NULL` を足し、`rank_24h` 昇順（1位が先頭）に並べる。
    """
    limit = max(1, min(limit, 100))
    after = _decode_cursor(cursor, 2)
    if after is not None:
        try:
            after = (int(after[0]), int(after[1]))
        except ValueError:
            after = None

    where = [
        *_display_where(),
        "rank_24h IS NOT NULL",
    ]
    params: list = []
    if after is not None:
        rank, rid = after
        where.append("(rank_24h > ? OR (rank_24h = ? AND id > ?))")
        params += [rank, rank, rid]

    sql = f"""
        WITH ranked AS (
            SELECT id, title, gofile_code, gofile_url, thumbnail_url,
                   source_posted_at, file_count, total_size_bytes,
                   source_x_account_id, profile_image_url, rank_24h
            FROM gofile_gallery_items
            WHERE {' AND '.join(where[:5])}
            ORDER BY rank_24h ASC, id ASC
            LIMIT ?
        )
        SELECT * FROM ranked
        {"WHERE " + where[5] if len(where) > 5 else ""}
        ORDER BY rank_24h ASC, id ASC
        LIMIT ?
    """
    display_cap = settings.gofile.display_limit
    args = [display_cap, *params, limit + 1]
    rows = db.rows_to_dicts(db.conn().execute(sql, args).fetchall())

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_cursor([str(rows[-1]["rank_24h"]), str(rows[-1]["id"])])
        if has_more and rows
        else None
    )

    items = [_row_to_item(r, rank=r["rank_24h"]) for r in rows]
    return {"items": items, "nextCursor": next_cursor, "lastSyncedAt": last_sync_at()}


def list_by_account(x_account_id: str, cursor: str | None, limit: int) -> dict:
    """投稿者別一覧 (v13)。表示条件・並び順は `list_display` と同じ（新着順）に
    `source_x_account_id` の絞り込みを足すだけ。

    ★新規の外部フェッチはしない★ うちのDBに既にある検証済み（valid）の
    アイテムだけを絞り込んで返す。投稿者のアバター等は呼び出し側（フロント）
    が既に持っているので、このAPIはアイテム一覧だけを返せばよい。
    """
    limit = max(1, min(limit, 100))
    after = _decode_recency_cursor(cursor)

    where = [
        *_display_where(),
        "source_x_account_id = ?",
    ]
    # ★バインド順はSQL文中に ? が現れる順そのまま★ CTE内のWHERE(account_id)
    # → CTEのLIMIT(display_cap) → 外側WHEREのカーソル条件 → 外側LIMIT、の順。
    # list_display/list_ranking と違い、CTE内WHEREに自前のプレースホルダ
    # (source_x_account_id = ?) があるので display_cap を先頭には置けない
    cursor_params: list = []
    if after is not None:
        posted, seen, rid = after
        where.append(
            "(COALESCE(source_posted_at,'') < ? OR "
            "(COALESCE(source_posted_at,'') = ? AND first_seen_at < ?) OR "
            "(COALESCE(source_posted_at,'') = ? AND first_seen_at = ? AND id < ?))"
        )
        cursor_params += [posted, posted, seen, posted, seen, rid]

    sql = f"""
        WITH ranked AS (
            SELECT id, title, gofile_code, gofile_url, thumbnail_url,
                   source_posted_at, first_seen_at, file_count, total_size_bytes,
                   source_x_account_id, profile_image_url
            FROM gofile_gallery_items
            WHERE {' AND '.join(where[:5])}
            ORDER BY COALESCE(source_posted_at,'') DESC, first_seen_at DESC, id DESC
            LIMIT ?
        )
        SELECT * FROM ranked
        {"WHERE " + where[5] if len(where) > 5 else ""}
        ORDER BY COALESCE(source_posted_at,'') DESC, first_seen_at DESC, id DESC
        LIMIT ?
    """
    display_cap = settings.gofile.display_limit
    args = [x_account_id, display_cap, *cursor_params, limit + 1]
    rows = db.rows_to_dicts(db.conn().execute(sql, args).fetchall())

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_cursor([rows[-1]["source_posted_at"] or "", rows[-1]["first_seen_at"], str(rows[-1]["id"])])
        if has_more and rows
        else None
    )

    items = [_row_to_item(r, rank=None) for r in rows]
    return {"items": items, "nextCursor": next_cursor, "lastSyncedAt": last_sync_at()}


# ---------------------------------------------------------------- ジョブロック・手動更新


def is_job_running() -> bool:
    if _get_meta(_LOCK_KEY) != "1":
        return False
    started = _get_meta(_LOCK_STARTED_KEY)
    if not started:
        return True
    try:
        started_dt = datetime.fromisoformat(started)
    except ValueError:
        return True
    # 異常終了して解放し損ねたロックを掃除する（§14.2 ジョブ最大実行時間 + 猶予2分）
    max_runtime = timedelta(minutes=settings.gofile.job_max_runtime_minutes + 2)
    return _now() - started_dt <= max_runtime


def acquire_lock() -> str | None:
    """ロックを取る。既に動いていれば None。

    gofile-worker は単一プロセス・単一スレッドでジョブを直列実行するので、
    ここでの競合は起きない（呼び出しは常にそのプロセス自身から）。
    """
    if is_job_running():
        return None
    job_id = uuid.uuid4().hex[:12]
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (_LOCK_KEY, "1"))
        c.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            (_LOCK_STARTED_KEY, _now().isoformat()),
        )
    return job_id


def release_lock() -> None:
    _set_meta(_LOCK_KEY, "0")


def request_manual_sync() -> str:
    """管理者が手動更新を要求する (§8.2)。実行は gofile-worker が拾う。"""
    job_id = uuid.uuid4().hex[:12]
    _set_meta(_REQUEST_KEY, job_id)
    return job_id


def pop_requested_sync() -> str | None:
    """worker が要求を1回だけ消費する。"""
    job_id = _get_meta(_REQUEST_KEY)
    if job_id:
        _del_meta(_REQUEST_KEY)
    return job_id


# ---------------------------------------------------------------- ゲストセッション
#
# ★プロセスのメモリではなく DB に置く★ gofile の `POST /accounts`
# （ゲストアカウント作成）は**厳しくレート制限されていて、一度踏むと
# 数時間単位で解除されない**（2026-08-16 に実際に踏んで半日近く保存が
# できなくなった）。メモリ保持だと `docker compose up -d api` のたびに
# トークンを捨てて作り直しに行くので、api を再起動するだけで貴重な
# 作成回数を1回消費してしまう。DB に置けば再起動をまたいで使い回せる。


def get_guest_session() -> tuple[str, datetime] | None:
    """保存済みのゲストトークンと、その作成時刻を返す。無ければ None。"""
    token = _get_meta(_GUEST_TOKEN_KEY)
    raw_at = _get_meta(_GUEST_TOKEN_AT_KEY)
    if not token or not raw_at:
        return None
    try:
        created = datetime.fromisoformat(raw_at)
    except ValueError:
        return None
    return token, created


def save_guest_session(token: str) -> None:
    _set_meta(_GUEST_TOKEN_KEY, token)
    _set_meta(_GUEST_TOKEN_AT_KEY, _now().isoformat())


def clear_guest_session() -> None:
    _del_meta(_GUEST_TOKEN_KEY)
    _del_meta(_GUEST_TOKEN_AT_KEY)


def mark_synced() -> None:
    _set_meta(_LAST_SYNC_KEY, _now().isoformat())


def last_sync_at() -> str | None:
    return _get_meta(_LAST_SYNC_KEY)


def seconds_since_last_sync() -> float | None:
    raw = last_sync_at()
    if not raw:
        return None
    try:
        return (_now() - datetime.fromisoformat(raw)).total_seconds()
    except ValueError:
        return None


# ---------------------------------------------------------------- 再検証バッチサイズ (§14.8)


def reverify_batch_size() -> int:
    raw = _get_meta(_BATCH_SIZE_KEY)
    if raw is None:
        return settings.gofile.reverify_batch_size
    try:
        return int(raw)
    except ValueError:
        return settings.gofile.reverify_batch_size


def note_run_health(*, healthy: bool) -> None:
    """§14.8: 7日間問題が無ければ再検証数を広げる。問題があれば据え置いて計り直す。"""
    g = settings.gofile
    current = reverify_batch_size()
    since_raw = _get_meta(_BATCH_SINCE_KEY)
    since = None
    if since_raw:
        try:
            since = datetime.fromisoformat(since_raw)
        except ValueError:
            since = None

    if not healthy:
        _set_meta(_BATCH_SINCE_KEY, _now().isoformat())
        return

    if since is None:
        _set_meta(_BATCH_SINCE_KEY, _now().isoformat())
        return

    if current < g.reverify_batch_max and _now() - since >= timedelta(days=7):
        _set_meta(_BATCH_SIZE_KEY, str(g.reverify_batch_max))
        _set_meta(_BATCH_SINCE_KEY, _now().isoformat())


# ---------------------------------------------------------------- ジョブ実行記録 (§12)


def start_run(trigger: str) -> int:
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO gofile_sync_runs(trigger) VALUES (?)", (trigger,)
        )
        return cur.lastrowid


def finish_run(run_id: int, *, ok: bool, stats: dict, error: str | None, duration_ms: int) -> None:
    with db.tx() as c:
        c.execute(
            """UPDATE gofile_sync_runs SET
                 finished_at = datetime('now'), ok = ?, pages_fetched = ?,
                 created_count = ?, updated_count = ?, valid_count = ?,
                 invalid_count = ?, unknown_count = ?, thumb_failed_count = ?,
                 deferred_count = ?, error = ?, duration_ms = ?
               WHERE id = ?""",
            (
                1 if ok else 0,
                stats.get("pages_fetched", 0),
                stats.get("created", 0),
                stats.get("updated", 0),
                stats.get("valid", 0),
                stats.get("invalid", 0),
                stats.get("unknown", 0),
                stats.get("thumb_failed", 0),
                stats.get("deferred", 0),
                error,
                duration_ms,
                run_id,
            ),
        )
