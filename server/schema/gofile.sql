-- Gofile ギャラリー（server/app/gofile/store.py・download.py・worker_main.py）が使うテーブル一式。
-- nas-app の server/app/db.py の「新規DBの全体スキーマ」から該当部分をそのまま抜いたもの。
-- どれも CREATE ... IF NOT EXISTS なので、起動時に executescript してよい。
-- 接続は WAL・foreign_keys は不要（外部キーは無い）。api と gofile-worker が同じ SQLite ファイルを開く。

-- store.py が単純な key/value に使う（同期ジョブのロック等）。ホストアプリに meta があるならそれを使う
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Gofileサムネイルギャラリー (docs/refs/gofile-thumbnail-gallery-spec.md)。
-- gofile-douga.com の新着を定期取得し、リンク切れでないものだけを画面に出す。
-- ファイル本体は一切扱わない。ここに持つのはメタデータと検証結果だけ
CREATE TABLE IF NOT EXISTS gofile_gallery_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_item_id        TEXT,
    gofile_code           TEXT NOT NULL,
    gofile_url            TEXT NOT NULL,
    title                 TEXT,
    thumbnail_url         TEXT NOT NULL,
    thumbnail_urls_json   TEXT,
    source_posted_at      TEXT,
    file_count            INTEGER,
    total_size_bytes      INTEGER,
    source_status         TEXT,
    link_status           TEXT NOT NULL DEFAULT 'pending'
                           CHECK (link_status IN ('pending','valid','invalid','unknown')),
    link_checked_at       TEXT,
    link_check_expires_at TEXT,
    link_check_attempts   INTEGER NOT NULL DEFAULT 0,
    thumbnail_status      TEXT NOT NULL DEFAULT 'pending'
                           CHECK (thumbnail_status IN ('pending','valid','invalid','unknown')),
    priority_recheck      INTEGER NOT NULL DEFAULT 0,
    first_seen_at         TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at          TEXT NOT NULL DEFAULT (datetime('now')),
    hidden_at             TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    -- 24hランキングでの現在の順位。圏外に落ちたら NULL（v13）
    rank_24h              INTEGER,
    -- 投稿者情報。/new・/rankings どちらの応答にも含まれる（v13）
    source_x_account_id   TEXT,
    profile_image_url     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gofile_code ON gofile_gallery_items(gofile_code);
CREATE INDEX IF NOT EXISTS idx_gofile_source_item ON gofile_gallery_items(source_item_id);
CREATE INDEX IF NOT EXISTS idx_gofile_display ON gofile_gallery_items(
    link_status, thumbnail_status, link_check_expires_at
);
CREATE INDEX IF NOT EXISTS idx_gofile_posted ON gofile_gallery_items(
    COALESCE(source_posted_at,'') DESC, first_seen_at DESC
);
CREATE INDEX IF NOT EXISTS idx_gofile_hidden ON gofile_gallery_items(hidden_at);
CREATE INDEX IF NOT EXISTS idx_gofile_rank24h ON gofile_gallery_items(rank_24h)
    WHERE rank_24h IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_gofile_account ON gofile_gallery_items(source_x_account_id)
    WHERE source_x_account_id IS NOT NULL;

-- 同期ジョブ1回ぶんの実行記録 (§12 ログと監視)
CREATE TABLE IF NOT EXISTS gofile_sync_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at        TEXT,
    trigger            TEXT NOT NULL DEFAULT 'schedule'
                       CHECK (trigger IN ('schedule','manual','startup')),
    ok                 INTEGER,
    pages_fetched      INTEGER NOT NULL DEFAULT 0,
    created_count      INTEGER NOT NULL DEFAULT 0,
    updated_count      INTEGER NOT NULL DEFAULT 0,
    valid_count        INTEGER NOT NULL DEFAULT 0,
    invalid_count      INTEGER NOT NULL DEFAULT 0,
    unknown_count      INTEGER NOT NULL DEFAULT 0,
    thumb_failed_count INTEGER NOT NULL DEFAULT 0,
    deferred_count     INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    duration_ms        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gofile_runs_started ON gofile_sync_runs(started_at DESC);

-- 長押し/URL貼り付け保存の「既にダウンロード済み」警告用 (routers/gofile_gallery.py)
CREATE TABLE IF NOT EXISTS gofile_saved_downloads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    gofile_code    TEXT NOT NULL,
    gofile_url     TEXT NOT NULL,
    dest_path      TEXT NOT NULL,
    save_count     INTEGER NOT NULL DEFAULT 1,
    first_saved_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_saved_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gofile_saved_code ON gofile_saved_downloads(gofile_code);

