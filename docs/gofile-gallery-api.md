# Gofileサムネイルギャラリー API（`/api/gofile-gallery`）

`https://gofile-douga.com/ranking` の新着を定期取得し、リンク切れでない
ものだけをサムネイル一覧として見せる。**画面はメディアタブの切替「Gofile」**
（2026-10-12 に、「ファイル」5連打の隠しページから移した。`web/src/routes/MediaHub.tsx`）。
保存先の `private/tmp/{タイトル}/` はそのタブの「プライベート」で見る。仕様書は
`docs/refs/gofile-thumbnail-gallery-spec.md`（章番号 §はそちらを指す）。

**新着に加えて、人気ランキング(24h)と投稿者別一覧も見られる**
（2026-07-30〜）。gofile-douga.com のフロントJSバンドルを読んで見つけた、
サイト自身が使っている未使用API（`/api/rankings`・`/api/x/{id}/items`）
を元にしている。いずれも既存の許可ホスト `gofile-douga.com` 内。

- **人気ランキング**: `/api/rankings?tab=24h` を新着と同じ30分ジョブで
  同期する（`app/gofile/client.py` の `fetch_rankings()`）。サイト自体は
  12h/24h/1d/3dの4タブを持つが、今回は24hだけに絞った（実測60件・
  1ページで完結。物足りなければ増やしやすい設計にしてある）。
  圏外に落ちたアイテムは `rank_24h` をNULLに戻すだけで、行自体は消さない
  （§6.4と同じ「安全側に倒す」考え方）。
- **投稿者別一覧**: `sourceXAccountId`（`/new`・`/rankings` 両方の応答に
  含まれる）で自分のDBを絞り込むだけ。**gofile-douga.com への新規の
  ライブ取得はしない** — 「検証済みしか画面に出さない」という既存の
  原則を崩さないための判断。したがって見えるのは「うちが既に新着/
  人気経由で見つけて検証済みのその投稿者の投稿」だけで、その投稿者の
  全投稿ではない。

**タップでは動画をダウンロードしない。** サムネイルを選ぶと公式Gofileページ
（`https://gofile.io/d/{code}`）へ新しいタブで遷移するだけ。

例外は**長押し→保存**（`web/src/routes/gofile/Card.tsx`）と**URL貼り付け→
保存**（`web/src/routes/GofileGallery.tsx` 上部の入力欄）の2つ。どちらも
動画本体を NAS の `private/tmp/` 配下、コンテンツのタイトルをそのまま
フォルダ名にして丸ごと落とす（`private/tmp/{タイトル}/`。連番フォルダは
挟まない — 2026-07-31、動画を見るのに番号とタイトルの2階層を開く手間を
無くした。同名衝突だけ ` (2)` で避ける）。前者はギャラリー
のDBに載っている項目だけ、後者はDBに関係なく任意の `gofile.io/d/{code}`
リンクを保存できる（他所で貼られたリンクなど）。実装は
`server/app/gofile/download.py`。yyyywaiwai/gofile-dl
(https://github.com/yyyywaiwai/gofile-dl、Node製CLI) のダウンロード手順
（ゲストWebセッション作成→コンテンツ木の取得→ファイルを1つずつ保存）を
このアプリのPython/非同期実装へ移植したもの。

**保存ジョブは同時に1本だけ、残りはキューで順番待ち**（2026-07-29〜）。
動画は数百MBあり置き場は Pi 4 に USB接続した回転式HDD なので、複数本を
同時に書き込むとヘッドのシークが競合してどちらも遅くなる（回線帯域も
家庭用で共有）。積みすぎ（既定30件）なら429。実装は
`server/app/routers/gofile_gallery.py` の `_start_save_job`/`_drain_queue`。

保存の本体は `gofile/download.py` の `save_blocking`（排他 `SAVE_LOCK` → 保存 → 棚の更新 →
保存履歴）。**メディアタブの「URLから保存」に gofile のリンクを貼った場合も同じ出口**を通るので、
ギャラリーの保存とシートの保存は互いに順番待ちになる（`docs/media-api-new.md` の「gofile のフォルダ」）。

## 2プロセス構成

```
api コンテナ            gofile-worker コンテナ
  routers/gofile_gallery.py   app/gofile/scheduler.py (30分ジョブ)
        |                           |
        +------ 同じ kakei.sqlite3 (SQLite / WAL) ------+
```

**api イメージに Playwright は入れない**（§14.1・§14.7）。実際の新着取得
（`app/gofile/client.py`）とヘッドレスブラウザによるリンク検証
（`app/gofile/linkcheck.py`）は `gofile-worker` コンテナだけが行う。
api 側が触るのは `app/gofile/store.py`（DB読み書き）だけで、Playwrightの
importすら無い。分離した理由: Chromiumのメモリ膨張・クラッシュが
写真・家計・チャットの応答を巻き込まないようにするため。

手動更新（後述）は「実行」ではなく「要求」を書くだけ。実行するのは
gofile-worker 側の `run_forever()` ループで、5秒おきに要求の有無を見ている
（`app/gofile/scheduler.py`）。

## エンドポイント

| メソッド | パス | 認証 | 中身 |
|---|---|---|---|
| GET | `/api/gofile-gallery?cursor=&limit=` | 要 | 一覧・新着順（§8.1） |
| GET | `/api/gofile-gallery/rankings?cursor=&limit=` | 要 | 一覧・人気(24h)順 |
| GET | `/api/gofile-gallery/by-account/{accountId}?cursor=&limit=` | 要 | 投稿者別一覧（DB内の検証済みのみ） |
| POST | `/api/gofile-gallery/{id}/report-broken` | 要 | 画像読込エラーの再検証要求（§9.3） |
| POST | `/api/gofile-gallery/{id}/save` | 要 | 長押し→保存。動画本体をprivate/tmpへ。空いていれば即実行、実行中ならキューに追加（`queued:true`）。積みすぎなら429 |
| POST | `/api/gofile-gallery/save-url` | 要 | URL貼り付け→保存。body `{url}`。形式不正なら400。キューの扱いは上と同じ |
| POST | `/api/admin/gofile-gallery/sync` | 要 | 手動更新の要求（§8.2）。実行中なら409 |

このアプリに「管理者」という別ロールは無い（`auth.py` は単一パスワードの
単一ユーザー）。ログインできる=手動更新もできる、という前提でよい
（既存の `logout-all` 等と同じ扱い）。CSRF対策を別途足していないのは、
認証が Cookie ではなく Bearer トークンだから（CSRFはCookie自動送信が
前提の攻撃で、ここには当てはまらない）。

`list`（新着・人気・投稿者別いずれも同じ形）の応答:

```json
{
  "items": [
    {
      "id": 1,
      "title": "表示タイトル",
      "thumbnailUrl": "https://...",
      "gofileUrl": "https://gofile.io/d/ABC123",
      "sourcePostedAt": "2026-07-27T10:10:14Z",
      "fileCount": 3,
      "totalSizeBytes": 588075525,
      "sourceXAccountId": "JacobLinto43427",
      "profileImageUrl": "https://pbs.twimg.com/profile_images/.../normal.jpg",
      "rank": null
    }
  ],
  "nextCursor": "base64...",
  "lastSyncedAt": "2026-07-27T12:00:00Z"
}
```

`rank` は `/rankings` の応答でだけ順位（1が先頭）が入り、新着・投稿者別
一覧では常に `null`。`sourceXAccountId`/`profileImageUrl` は無いアイテム
（移行前の古い行、または投稿者不明）では両方 `null` になりうる。

`cursor` は不透明な文字列。新着・投稿者別一覧は
`(source_posted_at, first_seen_at, id)`、人気ランキングは `(rank_24h, id)`
を base64url化したもの（`store.py` の `_encode_cursor`/`_decode_cursor`が
両方の形を共通で扱う）。生の区切り文字（`\x1f`）のままだと一部の
HTTPクライアントが「非表示ASCII文字を含むURL」として弾く実測を踏んだので、
必ず base64 を通す。

## 表示条件は `store.list_display()` の1箇所だけ

§6.1 の5条件のうち、DBの状態で判定できるもの（gofile.ioホスト・
`/d/{code}`形式・直近の検証結果がvalid・期限内）は SQL の WHERE 句に
集約してある。「代表サムネイルを正常に取得できる」は検証時
（`thumbcheck.py`）に別途確認済みで、その結果が `thumbnail_status='valid'`
に入っている。判定不能・期限切れは安全側に倒して単に表示しない
（例外を投げない。§6.1「安全側に倒し、画面へ表示しない」）。

### 「期限内」の長さは検証の速さで決まる（2026-09-04 に 30分 → 24時間）

表示条件の1つ「直近の検証結果が期限内」の長さは `GOFILE_LINK_TTL_MINUTES`。
**ここは検証の処理能力と釣り合っていないと、画面がまるごと空になる。**

実測（2026-09-04）:

| | |
|---|---|
| 検証すべき行 | 5,111件 |
| 30分ジョブ1回の検証数 | 約9件（1件 12.2秒・Playwright） |
| 全部を一周するのに | **約18日** |

18日で一周するものに **30分**の期限を付けていたので、`link_status='valid'` な
行が 4,881件あるのに **画面に出せるのは1件**だった（＝カードが1枚だけの画面）。
`display_limit=100`・カーソルページング・無限スクロールが全部空回りしていた。

24時間にすると表示可能は約135件。24時間前に検証したサムネの生存率は
実測 **88%**（25本中22本が 200）。7日以上だと 48% まで落ちるので伸ばさない。
すり抜けた死にリンクは `<img onError>` → `report-broken`（§9.3）が拾って消す。

**期限を縮めたいなら、先に検証の本数を増やすこと。** 逆順にすると画面が空になる。

## セキュリティ（§11）— `app/gofile/security.py`

- 取得先ホストは `FETCH_ALLOWED_HOSTS = {"gofile-douga.com"}` の固定リスト。
  `config.py` の `GOFILE_DOUGA_BASE_URL` を書き換えても、実際のリクエストは
  ここでしか許可しない（設定でホスト検証を緩められる経路を作らない）
- `gofileUrl` は `^https://gofile\.io/d/[A-Za-z0-9]+/?$` だけを許可
- サムネイルURLは `https` + `gofile.io` / `*.gofile.io` だけ
- **リダイレクトはHTTPクライアント任せにしない。** `security.safe_get()` が
  1ホップごとに手動でホスト・スキームを確認してから次へ進む
  （`follow_redirects=False` + 自前ループ、最大3回）
- 名前解決の結果がループバック/プライベート/リンクローカルIPなら拒否する
  （`_resolves_to_public_ip`）。DNSリバインディング対策
- タイトルはReactが素のテキストとして描画するのでHTMLとして解釈されない
  （`dangerouslySetInnerHTML` を使っていない）

## Raspberry Pi 4向け軽量設定（§14）

`config.py` の `GofileSettings` は**汎用値ではなく最初から §14.2 の値**を
既定にしてある（検証並列数1・再検証5→10件・ジョブ最大10分）。理由は
初期導入先がPi 4だから。他機種で動かす場合だけ環境変数で緩める。

- Chromiumは30分ジョブの開始時に1プロセスだけ起動し、終了時に必ず閉じる
  （`linkcheck.BrowserSession`。常駐させない・1件ごとに起動し直さない）
- ジョブ開始前とジョブ途中の両方で負荷を見る（`hostload.py` /
  `scheduler._run_verification`）。空きメモリ不足・高負荷・高温なら
  Playwright検証を延期し、対象は `pending`（障害件数に含めない）のまま残す
- Chromiumの一時データはコンテナの `/tmp`（tmpfs）に置く。
  `docker-compose.yml` の `gofile-worker.tmpfs` 参照。microSDには蓄積しない
- 動画・音声・広告・アクセス解析・Webフォントは `linkcheck._route_filter`
  で遮断する。画像は遮断しない（判定に影響しうるものは残す側に倒す、
  という §14.4 末尾の注記に従う）

## 関連

- 実装: `server/app/gofile/`（`store.py` `client.py` `security.py`
  `thumbcheck.py` `linkcheck.py` `hostload.py` `scheduler.py`
  `worker_main.py` `download.py`）/ `server/app/routers/gofile_gallery.py`
- テスト: `server/tests/test_gofile_gallery.py`
- 画面: `web/src/routes/GofileGallery.tsx` + `web/src/routes/gofile/` /
  `web/src/styles/gofile.css`
- Docker: `server/Dockerfile.gofile-worker` /
  `server/requirements-gofile-worker.txt` / `docker-compose.yml` の
  `gofile-worker` サービス
