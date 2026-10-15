"""gofile-worker コンテナの起点。

    python -m app.gofile.worker_main

api プロセス（uvicorn）とは別プロセス・別コンテナで動く（§14.1・§14.7）。
DBファイルは api と共有する（kakei_db。SQLite の WAL + busy_timeout で
複数プロセスからの読み書きに耐える設計は db.py 側で既に済んでいる）。
"""
from __future__ import annotations

import asyncio
import logging

from .. import db
from ..config import settings
from . import scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nasapp.gofile.worker_main")


def main() -> None:
    db.init_db()
    log.info(
        "gofile-worker 起動 (同期間隔=%d分, 検証並列数=%d, 再検証バッチ=%d件, "
        "ジョブ最大実行時間=%d分)",
        settings.gofile.sync_interval_minutes,
        settings.gofile.verify_concurrency,
        settings.gofile.reverify_batch_size,
        settings.gofile.job_max_runtime_minutes,
    )
    try:
        asyncio.run(scheduler.run_forever())
    except KeyboardInterrupt:
        log.info("gofile-worker を終了します")


if __name__ == "__main__":
    main()
