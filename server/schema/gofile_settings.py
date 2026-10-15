"""nas-app の server/app/config.py から、Gofile ギャラリーが読む設定だけを抜いたもの（ほぼ原文）。

ホストアプリの config に `gofile: GofileSettings` として足す。ほかに gofile/ と urlfetch.py が
`settings.tmp_root`（Path）・`settings.files_root`（Path）・`settings.private_dir`（str・既定 "private"）・
`settings.max_upload_bytes`（int）を読む。AGENTS.md の「サーバの接合点」を見ること。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _int_env(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class GofileSettings:
    """Gofileサムネイルギャラリー (docs/refs/gofile-thumbnail-gallery-spec.md) の設定。

    初期導入先が Raspberry Pi 4 なので、既定値は汎用値ではなく
    仕様書 §14.2 の「Raspberry Pi 4向け軽量設定」をそのまま使う。
    """

    # 新着取得先。ホストの許可判定は app/gofile/security.py が固定リストで行う
    # （ここを書き換えても検証は緩まない。URLの組み立て用の設定でしかない）
    fetch_base_url: str = field(
        default_factory=lambda: _env("GOFILE_DOUGA_BASE_URL", "https://gofile-douga.com")
    )
    sync_interval_minutes: int = field(
        default_factory=lambda: _int_env("GOFILE_SYNC_INTERVAL_MINUTES", 30)
    )
    initial_fetch_limit: int = field(
        default_factory=lambda: _int_env("GOFILE_INITIAL_FETCH_LIMIT", 100)
    )
    max_fetch_limit: int = field(default_factory=lambda: _int_env("GOFILE_MAX_FETCH_LIMIT", 500))
    display_limit: int = field(default_factory=lambda: _int_env("GOFILE_DISPLAY_LIMIT", 100))
    # §14.2: Pi 4 は検証の並列数を 1 に絞る（Chromium 1本きり）
    verify_concurrency: int = field(
        default_factory=lambda: _int_env("GOFILE_VERIFY_CONCURRENCY", 1)
    )
    # §14.2: 既存項目の再検証は毎回5件から始め、様子を見て10件まで広げる
    reverify_batch_size: int = field(
        default_factory=lambda: _int_env("GOFILE_REVERIFY_BATCH_SIZE", 5)
    )
    reverify_batch_max: int = field(
        default_factory=lambda: _int_env("GOFILE_REVERIFY_BATCH_MAX", 10)
    )
    page_check_timeout_s: int = field(
        default_factory=lambda: _int_env("GOFILE_PAGE_TIMEOUT_S", 15)
    )
    thumb_check_timeout_s: int = field(
        default_factory=lambda: _int_env("GOFILE_THUMB_TIMEOUT_S", 10)
    )
    # 検証結果を「まだ有効」と見なす時間。**これが画面に出る件数を決める。**
    #
    # ★30分から24時間へ（2026-09-04）★ 30分は「検証が追いつく」前提の値で、
    # その前提が成立していなかった。実測:
    #
    #   検証すべき行           5,111件
    #   30分ジョブ1回の検証数  約9件（1件あたり12.2秒・Playwright）
    #   → 一周するのに        約18日
    #
    # 18日で一周するものに30分の期限を付けていたので、**表示可能な行が
    # 4,881件中1件しか無い**状態になっていた（＝ギャラリーがほぼ空）。
    # ページング・無限スクロール・display_limit=100 が全部空回りしていた。
    #
    # 24時間にすると表示可能は約135件（上限100を埋められる）。
    # 24時間前に検証したサムネの生存率は実測88%（25本中22本が200）。
    # 7日以上だと48%まで落ちるので、そこまでは伸ばさない。
    # 死んだぶんは `<img onError>` → `report-broken`（§9.3）が拾って消す。
    link_ttl_minutes: int = field(
        default_factory=lambda: _int_env("GOFILE_LINK_TTL_MINUTES", 1440)
    )
    unknown_retry_max: int = field(
        default_factory=lambda: _int_env("GOFILE_UNKNOWN_RETRY_MAX", 3)
    )
    invalid_recheck_hours: int = field(
        default_factory=lambda: _int_env("GOFILE_INVALID_RECHECK_HOURS", 24)
    )
    hidden_retention_days: int = field(
        default_factory=lambda: _int_env("GOFILE_HIDDEN_RETENTION_DAYS", 30)
    )
    # §14.2: ジョブ最大実行時間。これを超えたロックは前回異常終了とみなして解放する
    job_max_runtime_minutes: int = field(
        default_factory=lambda: _int_env("GOFILE_JOB_MAX_RUNTIME_MINUTES", 10)
    )
    # §14.5: 空きメモリがこれ未満なら Playwright 検証を開始しない
    min_free_memory_mb: int = field(
        default_factory=lambda: _int_env("GOFILE_MIN_FREE_MEMORY_MB", 500)
    )
    # §14.5: ロードアベレージ(1分)がこれを超えたら検証を延期する。
    # Pi 4 は4コアなので、1コアぶんの余裕を見て 3.0 を既定にする
    max_load_average: int = field(default_factory=lambda: _int_env("GOFILE_MAX_LOAD_AVERAGE", 3))
    # §6.5: User-Agent に入れる連絡先。空なら連絡先無しのUAになる
    contact: str = field(default_factory=lambda: _env("GOFILE_CONTACT"))

    @property
    def user_agent(self) -> str:
        base = "nas-app-gofile-gallery/0.1"
        return f"{base} (+{self.contact})" if self.contact else base

