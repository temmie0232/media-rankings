"""Gofileサムネイルギャラリー (docs/refs/gofile-thumbnail-gallery-spec.md §8)。

実際の取得・検証は gofile-worker コンテナが行う（app/gofile/scheduler.py）。
ここは DB を読み書きするだけの薄い層で、Playwright には依存しない。

## 長押し→保存だけは例外（動画本体を触る）

一覧・再検証要求・手動更新は「動画は一切ダウンロードしない」という
このギャラリーの原則どおり DB を読み書きするだけだが、`/save` 系だけは
動画本体を落とす（app/gofile/download.py）。Playwright は要らない
（gofile.io 公式APIへの素のHTTPアクセスだけで済む）ので、gofile-worker
コンテナへは分離せず、この api コンテナの中で完結させる。

### 保存ジョブはキュー（直列・1本ずつ）

最初は「実行中なら409で弾く」だけだったが、複数件を続けて保存したい
という要望を受けてキューに変えた（2026-07-29）。★同時には1本しか
実行しない★。動画は数百MBあり、置き場は microSD ではなく Pi 4 に
USB接続した HDD（`private/`）で、同時に複数本を書き込むとヘッドの
シークが競合してどちらも遅くなる（回転式ディスクの特性。SSD なら
問題にならない）。回線帯域も家庭用で共有なので、並列実行のメリットより
デメリットのほうが大きいと判断した。

キューへ積むだけなら一瞬で終わるので、応答はスレッドに逃がさず即座に
返せる。実際のダウンロードは唯一のワーカースレッドが順番に捌く
（`_drain_queue`。空になったら終了し、次に積まれたときに立て直す）。
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from ..auth import require_auth
from ..gofile import download, security, store

log = logging.getLogger("nasapp.gofile_gallery")

router = APIRouter(prefix="/api", tags=["gofile-gallery"])

# 積みすぎ防止の安全弁。個人利用なのでここまで積むことは想定していないが、
# 二重タップの連打やフロントの不具合で無限に増え続けないようにする
_MAX_QUEUE = 30

_queue_lock = threading.Lock()
_save_queue: queue.Queue[tuple[str, str]] = queue.Queue()
_worker_running = False


def _run_one(gofile_url: str, *, label: str) -> None:
    try:
        dest = download.save_blocking(gofile_url)
        log.info("gofile保存が完了しました: %s dest=%s", label, dest)
    except Exception:
        log.exception("gofile保存に失敗しました: %s", label)


def _drain_queue() -> None:
    """キューが空になるまで1本ずつ実行し続ける裏スレッド本体。

    ★終了判定は `_queue_lock` の中でやる★ ロックの外で「空だった」と
    見てから `_worker_running` を倒すまでの間に別リクエストが積むと、
    次のワーカーが立たないまま誰も拾わない行が残る（取りこぼし）。
    """
    global _worker_running
    while True:
        try:
            gofile_url, label = _save_queue.get_nowait()
        except queue.Empty:
            with _queue_lock:
                if _save_queue.empty():
                    _worker_running = False
                    return
            continue
        _run_one(gofile_url, label=label)


def _start_save_job(gofile_url: str, *, label: str, force: bool = False) -> dict:
    """保存ジョブをキューへ積む。空いていれば即座に、埋まっていれば順番待ちに。

    長押し保存（DBの既知アイテム）とURL貼り付け保存（任意のリンク）の
    どちらもここを通す。実行そのものの中身は同じ（`download.save_content`）。

    ★過去に同じコードを保存済みなら、キューに積まずに警告として返す★
    （`force=True` のときだけ、承知の上でもう一度保存する）。
    無駄なダウンロードでHDDと回線を消費させないためのガード
    """
    if not force:
        code = security.extract_gofile_code(gofile_url)
        existing = store.get_saved_download(code) if code else None
        if existing:
            return {
                "ok": True,
                "duplicate": True,
                "queued": False,
                "destPath": existing["dest_path"],
                "lastSavedAt": existing["last_saved_at"],
                "saveCount": existing["save_count"],
            }

    global _worker_running
    with _queue_lock:
        if _save_queue.qsize() >= _MAX_QUEUE:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "保存待ちが多すぎます。しばらく待ってから試してください"
            )
        _save_queue.put((gofile_url, label))
        if _worker_running:
            return {"ok": True, "queued": True}
        _worker_running = True

    threading.Thread(target=_drain_queue, name="gofile-save", daemon=True).start()
    return {"ok": True, "queued": False}


@router.get("/gofile-gallery")
def list_gallery(
    _: Annotated[str, Depends(require_auth)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict:
    return store.list_display(cursor, limit)


@router.get("/gofile-gallery/rankings")
def list_rankings(
    _: Annotated[str, Depends(require_auth)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict:
    """24h人気ランキング一覧。同期は `gofile-worker` の `run_once()` が
    `/new` と同じジョブの中でまとめて行う（`app/gofile/scheduler.py`）。
    """
    return store.list_ranking(cursor, limit)


# 投稿者別一覧で「これ以上なら催促しなくてよい」とみなす件数。
# これ未満のときだけ mark_account_priority_recheck を呼ぶ（無条件に毎回
# 呼ぶと、一覧を開くたびDBに無駄な書き込みが走る）
_ACCOUNT_NUDGE_THRESHOLD = 5


@router.get("/gofile-gallery/by-account/{account_id}")
def list_by_account(
    account_id: str,
    _: Annotated[str, Depends(require_auth)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict:
    """投稿者別一覧。うちのDBに既にある検証済みアイテムだけを絞り込む
    （gofile-douga.com への新規ライブ取得はしない。`store.list_by_account`
    のdocstring参照）。

    表示件数が少ないとき（1ページ目に限る。読み進めた途中では騒がない）は
    この投稿者の行をまとめて優先再検証に回す。古い投稿は新着順・人気順の
    再検証優先度では順番が回ってこず、TTL切れのまま表示されない状態が
    続くため（`store.mark_account_priority_recheck` のdocstring参照）。
    """
    result = store.list_by_account(account_id, cursor, limit)
    if cursor is None and len(result["items"]) < _ACCOUNT_NUDGE_THRESHOLD:
        store.mark_account_priority_recheck(account_id)
    return result


@router.post("/gofile-gallery/{item_id}/report-broken")
def report_broken(item_id: int, _: Annotated[str, Depends(require_auth)]) -> dict:
    """§9.3: 画面表示後の画像読込エラーを、バックエンドの再検証要求に変える。"""
    if not store.mark_priority_recheck(item_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "見つかりません")
    return {"ok": True}


@router.post("/gofile-gallery/{item_id}/save", status_code=status.HTTP_202_ACCEPTED)
def save_item(
    item_id: int,
    _: Annotated[str, Depends(require_auth)],
    force: Annotated[bool, Query()] = False,
) -> dict:
    """サムネイルの長押し→保存。動画本体をNASの private/tmp 配下、コンテンツ名の
    フォルダへ丸ごと落とす（app/gofile/download.py、yyyywaiwai/gofile-dl
    相当）。他に実行中の保存が無ければ即座に、あればキューに積んで順番に
    処理する（`queued: true` が返る。積みすぎなら429）。

    過去に同じコードを保存済みなら `duplicate: true` を返すだけでキューには
    積まない。承知の上でもう一度保存したいときだけ `?force=true` を付ける。
    """
    gofile_url = store.get_gofile_url(item_id)
    if gofile_url is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "見つかりません")

    return _start_save_job(gofile_url, label=f"item={item_id}", force=force)


class SaveUrlIn(BaseModel):
    url: str
    force: bool = False


@router.post("/gofile-gallery/save-url", status_code=status.HTTP_202_ACCEPTED)
def save_url(body: SaveUrlIn, _: Annotated[str, Depends(require_auth)]) -> dict:
    """URLを直接貼って保存する版。長押し保存と違い、ギャラリーのDBに載って
    いない任意の gofile.io リンク（他所で貼られたもの等）を保存できる。
    実体・キューの扱い・重複警告は長押し保存と同じ（`_start_save_job`）。
    """
    url = body.url.strip()
    if not security.extract_gofile_code(url):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "gofileのURLの形式が正しくありません")

    return _start_save_job(url, label="url", force=body.force)


@router.post("/admin/gofile-gallery/sync", status_code=status.HTTP_202_ACCEPTED)
def request_sync(_: Annotated[str, Depends(require_auth)]) -> dict:
    """§8.2 手動更新。認証は既存アプリと同じ単一ユーザーのものを使う
    （このアプリに複数権限レベルは無く、ログインできる=管理者という前提）。

    実行そのものは gofile-worker が拾う。ここではジョブが既に動いているかを
    見て、動いていれば 409、そうでなければ要求を書いて受理する。
    """
    if store.is_job_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "既に同期処理が実行中です")
    job_id = store.request_manual_sync()
    return {"ok": True, "job_id": job_id}
