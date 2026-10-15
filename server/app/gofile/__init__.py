"""Gofileサムネイルギャラリー機能。docs/refs/gofile-thumbnail-gallery-spec.md を実装する。

パッケージ構成:
    security.py  … ホスト許可リスト・SSRF対策・安全なHTTP取得 (§11)
    client.py    … gofile-douga.com からの新着取得 (§5)
    store.py     … DB の upsert / 一覧 / ジョブロック / 統計記録
    thumbcheck.py… サムネイルのHEAD/GET検証 (§6.3)
    linkcheck.py … Playwrightによるページ検証 (§6.2)。Chromiumが要るので
                   gofile-worker コンテナからしか import しない
    hostload.py  … 空きメモリ・CPU負荷・温度 (§14.5)。system.py と同じ
                   /proc, /sys の読み方を流用する
    scheduler.py … 30分ジョブの本体。ジョブロック・手動更新の受け口・
                   負荷に応じた延期をここで束ねる
    worker_main.py … gofile-worker コンテナの起点 (`python -m app.gofile.worker_main`)

★api コンテナは linkcheck.py と scheduler.py を import しない★
両方とも Playwright（Chromium 同梱）に依存する重い経路で、§14.1 の方針
（Playwrightと既存アプリを別コンテナに分離する）に従い、api イメージには
Playwright を入れていない。api 側（routers/gofile_gallery.py）が触るのは
store.py だけで、実際の取得・検証は gofile-worker コンテナが行う。
"""
