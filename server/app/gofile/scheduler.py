"""30分ジョブの本体 (§10 スケジューラー仕様)。gofile-worker コンテナから
`worker_main.py` 経由で呼ばれる。api コンテナはこのモジュールを import しない。
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..config import settings
from . import client, hostload, linkcheck, store, thumbcheck

log = logging.getLogger("nasapp.gofile.scheduler")

# 手動更新の要求をどれくらいの頻度で見に行くか。30分ジョブの合間でも
# すぐ反応してほしいので短い間隔にする（DBの1行を読むだけの軽い確認）
_POLL_SECONDS = 5


class _RateLimiter:
    """§6.5「同一ホストへのリクエスト間隔は最低1秒」を並列時にも守るための、
    全体で共有する簡易スロットラー。gofile.io / *.gofile.io をまとめて
    1つのホスト枠として扱う（分けるほどの精度は要らない）。
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._min_interval - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


async def _check_one(
    item: dict,
    session: linkcheck.BrowserSession,
    http_client: httpx.AsyncClient,
    limiter: _RateLimiter,
    stats: dict,
) -> None:
    await limiter.wait()
    link_status = await session.check(item["gofile_code"])

    if link_status == "valid":
        await limiter.wait()
        thumb_status = await thumbcheck.check_thumbnail(http_client, item["thumbnail_url"])
    else:
        thumb_status = "unknown"

    store.record_check_result(item["id"], link_status=link_status, thumb_status=thumb_status)
    stats[link_status] = stats.get(link_status, 0) + 1
    if thumb_status != "valid":
        stats["thumb_failed"] += 1


async def _run_verification(stats: dict) -> bool:
    """表示中・新規・再試行対象の検証 (§10 処理順4・5)。

    戻り値: このフェーズが健全に終わったか（§14.8 の再検証数拡張の判定に使う）。
    """
    defer, reason = hostload.should_defer_browser_checks()
    if defer:
        log.info("負荷が高いため検証を延期します: %s", reason)
        stats["deferred"] += len(store.candidates_for_check(settings.gofile.display_limit))
        return False

    batch = store.reverify_batch_size()
    candidates = store.candidates_for_check(batch)
    if not candidates:
        return True

    concurrency = max(1, settings.gofile.verify_concurrency)
    limiter = _RateLimiter(min_interval=1.0)
    healthy = True

    async with httpx.AsyncClient(
        timeout=20.0, headers={"User-Agent": settings.gofile.user_agent}
    ) as http_client:
        # §14.3: Chromiumは30分ジョブの開始時に1プロセスだけ起動し、
        # 同じブラウザ内でページを検証する（1件ごとに起動し直さない）
        async with linkcheck.BrowserSession() as session:
            for start in range(0, len(candidates), concurrency):
                chunk = candidates[start : start + concurrency]

                # §14.5: ジョブ途中で悪化したら、残りは次回へ送って正常終了する
                mid_defer, mid_reason = hostload.should_defer_browser_checks()
                if mid_defer:
                    log.info("ジョブ途中で負荷が悪化したため残りを延期します: %s", mid_reason)
                    stats["deferred"] += len(candidates) - start
                    healthy = False
                    break

                await asyncio.gather(
                    *(
                        _check_one(item, session, http_client, limiter, stats)
                        for item in chunk
                    )
                )

    return healthy


async def run_once(trigger: str) -> None:
    """§10 処理順のとおりに1回分を実行する。既に実行中なら何もしない。"""
    if store.acquire_lock() is None:
        log.info("既に実行中のため、この呼び出しはスキップします (trigger=%s)", trigger)
        return

    run_id = store.start_run(trigger)
    started = time.monotonic()
    stats = {
        "pages_fetched": 0, "created": 0, "updated": 0,
        "valid": 0, "invalid": 0, "unknown": 0,
        "thumb_failed": 0, "deferred": 0,
    }
    ok = True
    error: str | None = None

    try:
        # 1-3. 新着API取得 → DBへupsert
        codes = store.existing_codes()
        try:
            items, pages = await client.fetch_new(codes, is_first_run=not codes)
            stats["pages_fetched"] = pages
            stats["created"], stats["updated"] = store.upsert_items(items)
        except client.FetchError as e:
            # §5.1: 取得失敗は今回の同期を失敗扱いにするが、既存データは消さない。
            # 検証フェーズ（表示中項目の再検証）は取得と独立なので続行する
            ok = False
            error = str(e)
            log.warning("新着取得に失敗しました: %s", e)

        # 3.5. 人気ランキング(24h)取得 → DBへ反映
        #
        # ★失敗しても ok は倒さない★ ランキングは新着に対する補助的な
        # フィードなので、その失敗で新着側の「最終更新」まで止めたくない
        # （新着取得が成功していれば mark_synced() は呼ばれる）
        try:
            r_items, r_pages = await client.fetch_rankings()
            stats["pages_fetched"] += r_pages
            r_created, r_updated = store.upsert_ranking_items(r_items)
            stats["created"] += r_created
            stats["updated"] += r_updated
        except client.FetchError as e:
            log.warning("人気ランキング取得に失敗しました: %s", e)

        # 4-6. 検証キュー・表示中項目の再検証・期限切れの非表示化
        healthy = await _run_verification(stats)
        store.cleanup_hidden()
        store.note_run_health(healthy=healthy)

        if ok:
            store.mark_synced()
    except Exception as e:  # 想定外でもロックだけは必ず戻す
        ok = False
        error = str(e)
        log.exception("gofile同期ジョブで想定外の例外が発生しました")
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        store.finish_run(run_id, ok=ok, stats=stats, error=error, duration_ms=duration_ms)
        store.release_lock()
        log.info(
            "gofile同期ジョブ完了 trigger=%s ok=%s valid=%d invalid=%d unknown=%d "
            "deferred=%d 所要=%dms",
            trigger, ok, stats["valid"], stats["invalid"], stats["unknown"],
            stats["deferred"], duration_ms,
        )


async def run_forever() -> None:
    """§10: 30分ごとに実行し、手動更新の要求(§8.2)を随時拾う。

    起動直後: 前回実行から間隔以上経っていれば即座に1回実行する
    （「NAS再起動時に前回実行から30分以上経過していれば、起動後に1回同期する」）。
    """
    interval = settings.gofile.sync_interval_minutes * 60

    elapsed = store.seconds_since_last_sync()
    if elapsed is None or elapsed >= interval:
        await run_once("startup")

    last_scheduled = time.monotonic()
    while True:
        await asyncio.sleep(_POLL_SECONDS)

        requested = store.pop_requested_sync()
        if requested is not None:
            await run_once("manual")
            last_scheduled = time.monotonic()
            continue

        if time.monotonic() - last_scheduled >= interval:
            await run_once("schedule")
            last_scheduled = time.monotonic()
