# media-rankings — 動画一覧3画面の切り出し（組み込み用）

nas-app（自宅 NAS の PWA）の **メディアタブにある3つの「ランキング形式の一覧」** を、他のアプリへ
持っていくために切り出したリポジトリ。**これを読んだ AI は、組み込み先のアプリにこの機能を足す**のが仕事。

| 画面 | サイト | 何が出るか |
|---|---|---|
| Tokyomotion | tokyomotion.net | 動画一覧（人気・高評価・お気に入り・視聴中・新着・長さ × 今日/週/月/全期間） |
| 85po | 85po.net | 同じ形（人気・高評価・新着 × 期間） |
| Gofile | gofile-douga.com | 新着・人気(24h)・投稿者別のサムネイルギャラリー。リンク切れを除いて出す |

切り出し元: nas-app `ae478b4`（2026-10-15 時点の master）。**ファイルは元のパス構成のまま原文をコピー**してある
（import が `from .. import po85` のまま動くように）。接合点だけ下の「ホスト側が用意するもの」に書いた。

> 1つだけ取り込むなら **Tokyomotion か 85po**（軽い）。Gofile は SQLite・別コンテナの Playwright・定期ジョブを
> 連れてくる重い機能なので、要るときだけ。**まず軽い2つ → 後から Gofile** の順を勧める。

## 読む順番

1. このファイル
2. 取り込む画面の仕様書: `docs/tokyomotion-api.md` / `docs/po85-api.md` / `docs/gofile-gallery-api.md`
   （エンドポイント・入出力の形・取得とキャッシュの方針・実測した性質・**壊れたときに直す場所**が書いてある）
3. 該当ソース（下の地図）

## 中身の地図

```
server/
  app/
    tokyomotion.py          ★一覧の取得・パース・10分キャッシュ。標準ライブラリの正規表現 + httpx だけ。依存なし
    po85.py                 ★同上 + 動画ページから画質別 URL を割り出す（yt-dlp が 85po を取れないため）
    routers/tokyomotion.py  GET /api/tokyomotion/ranking     ← app.auth.require_auth だけに依存
    routers/po85.py         GET /api/po85/ranking            ← 同上
    routers/media_fetch.py  POST/GET/DELETE /api/media/fetch ← 「保存」ボタンの行き先（任意）
    urlfetch.py             保存の本体（yt-dlp。85po は専用の道 _run_po85）。config / privatefiles / gofile に依存
    gofile/                 Gofile ギャラリー一式（store=DB, client=取得, linkcheck=Playwright 検証, scheduler=30分ジョブ,
                            download=保存, security=SSRF 対策, worker_main=別コンテナの起点）
    routers/gofile_gallery.py  /api/gofile-gallery* と /api/admin/gofile-gallery/sync
    __init__.py, routers/__init__.py   空。単体でテストを走らせるためだけにある（ホストにコピーしない）
  schema/
    gofile.sql              Gofile が使う4テーブル（meta / gofile_gallery_items / gofile_sync_runs / gofile_saved_downloads）
    gofile_settings.py      GofileSettings（config.py から抜粋）。ホストの config に `gofile` として足す
  tests/                    下の「検証」参照。conftest.py は単体用の代役（ホストに入れたら捨てる）
  Dockerfile.gofile-worker / requirements-gofile-worker.txt / gofile-worker.compose.example.yml   Gofile の worker コンテナ（Playwright）
  requirements.txt          サーバ側の外部依存（版は nas-app と同じ）
web/src/
  routes/media/Tokyomotion.tsx, Po85.tsx   各30行。SiteConfig（並びと期間の名前）を渡すだけ
  routes/media/SiteList.tsx                ★2サイト共通の一覧画面。チップ・カード・無限スクロール・保存ボタン
  routes/media/UrlFetch.tsx                「URLから保存」のシート（進捗・中止）
  routes/media/api.ts, types.ts            ランキングと保存に要る分だけ抜粋（元は写真ライブラリの型が同居）
  routes/GofileGallery.tsx + routes/gofile/  Gofile 画面（Card, AccountView, api, lib, types）
  lib/urlfetch.ts                          保存ジョブの購読ストア。lib/mediaPane.ts … 画面の前面判定（usePaneActive）
  styles/gofile.css, styles/site-list.css  CSS（site-list.css は .tm* と .ufetch* を元の3ファイルから集めたもの）
docs/                       仕様書3本（原文）
```

**入っていないもの**（nas-app 側にある。必要なら元を見る）: メディアタブの切替帯 `MediaHub.tsx`、パターンロックの
`MediaGate.tsx`、プライベート棚 `PrivateShelf.tsx`、写真ライブラリ `Media.tsx`、共通の殻（下の表）。

## サーバ: ホスト側が用意するもの

**Tokyomotion / 85po の一覧だけなら、要るのは `app.auth.require_auth` ひとつ。**

| 何 | 誰が使う | 契約 |
|---|---|---|
| `app.auth.require_auth` | 全ルータ | FastAPI の依存関数。未認証なら 401 を投げ、通れば何か（ユーザ ID 等）を返す。nas-app は Bearer（`<img>` 用に `?t=` も可） |
| ルータの登録 | main | `app.include_router(tokyomotion.router)` など。`prefix="/api"` はルータ側に入っている |
| `app.config.settings` | gofile/*, urlfetch | `.gofile`（`schema/gofile_settings.py` の `GofileSettings`）、`.tmp_root`（Path）、`.files_root`（Path）、`.private_dir`（str, 既定 `"private"`）、`.max_upload_bytes`（int） |
| `app.db` | gofile/store, worker_main | `conn()`=スレッドごとの `sqlite3.Connection`（`isolation_level=None`・`row_factory=sqlite3.Row`・WAL・busy_timeout）/ `tx()`=`BEGIN IMMEDIATE` の contextmanager / `rows_to_dicts(rows)` / `init_db()`（`schema/gofile.sql` を流す） |
| `app.privatefiles.forget()` | gofile/download, urlfetch | 「保存でファイルが増えたので棚の記憶を捨てる」フック。無ければ no-op の関数でよい |
| 保存先 | gofile/download, urlfetch | `settings.files_root / settings.private_dir / "tmp" / {題名}/`。変えるなら `download.private_tmp_root()` と `urlfetch._save_into_folder` |

- **ホストに `app/__init__.py` が既にあるなら上書きしない。** この repo のものは空ファイル。
- 取得先の URL は**サーバ側で固定**（許可リスト）。利用者の入力は `sort`/`period`/`page` の名前でしか URL に入らない
  （SSRF 対策。`build_url()`）。この作りを崩さない。保存先 URL は `urlfetch.check_url`（グローバル IP だけ許可）。
- 85po / Tokyomotion の一覧に DB は要らない。キャッシュはプロセス内（10分・失敗は30秒）。
- 保存まで要るなら `routers/media_fetch.py` + `urlfetch.py` + `settings`/`privatefiles`。**保存を付けないなら** web の
  `SiteList.tsx` から保存ボタン（`save` 関数・`UrlFetch` シート）を外し、`routes/media/UrlFetch.tsx` と `lib/urlfetch.ts` も要らない。
- Gofile を入れるなら: `schema/gofile.sql` をホストの DB 初期化に足す → `worker_main` を**別プロセス/別コンテナ**で常駐
  （api に Playwright を入れない。`Dockerfile.gofile-worker` と `gofile-worker.compose.example.yml`）→
  `routers/gofile_gallery.py` を登録。api と worker が**同じ SQLite を開く**（WAL）。
  新着の取得先 `gofile-douga.com` は `gofile/security.py` の固定リストが唯一の許可元。

## Web: ホスト側が用意するもの（共通の殻）

画面は **React + TypeScript**。次の import が殻に当たる。**同名・同じ形で用意するか、import を書き換える**。
殻の実物は nas-app の `web/src/` にある（括弧内）。

| import | 使う側 | 契約 |
|---|---|---|
| `lib/api` の `api` | api.ts 各種 | `api.get<T>(path)` / `.post<T>(path, body)` / `.del<T>(path)`。Bearer を付け、401 でログアウト、`!ok` なら `ApiError(detail, status)` を投げる（nas-app `lib/api.ts`） |
| `lib/api` の `ApiError` | 画面全般 | `class ApiError extends Error { status: number }`。`detail` は日本語の文言でそのまま画面に出す |
| `lib/format` の `bytes` `shortDate` `shortDateTime` | Card, UrlFetch, GofileGallery | バイト数 / `YYYY-MM-DD` 系の短い表示（nas-app `lib/format.ts`。数十行の純関数） |
| `lib/motion` の `haptic(kind)` | 押下時 | `navigator.vibrate` のラッパ。**no-op でよい**（`'light'` など文字列を1つ取る） |
| `lib/toast` の `toast(msg, opts?)` | 保存結果 | トースト。無ければ `alert` 等の代用でよい（nas-app `lib/toast.ts`） |
| `components/ui` の `Empty` `Notice` `Icon` `Sheet` `useDelayed` `useBackGuard` | 全画面 | `Empty{title,hint?,action?,page?}` / `Notice{children,alert?,onRetry?}` / `Icon{name}`（使う名前は `back` `download` `check` `external` `refresh` だけ）/ `Sheet{title,onClose,children}` / `useDelayed(active, ms=200)`=点滅防止の遅延 bool / `useBackGuard(active, onBack)`=戻る操作の横取り |
| `components/PullRefresh` の `usePullRefresh({scroller,onRefresh,disabled?,idle?})` | 一覧 | 引いて更新。**戻り値の ReactNode をそのまま JSX に置く**（`{puller}`）。無ければ `() => null` で可 |
| `components/TabPage` の `useRevalidateOnShow(reload, awayMs)` | 一覧 | 離れていた画面が前面に戻ったら `reload()`。無ければ no-op で可 |
| `lib/mediaPane` の `usePaneActive()` | 一覧 | 切替帯で前面かどうか（bool）。切替帯が無いなら Provider を置かなければ既定で `true`（このリポジトリのコピーはそのまま使える） |

## CSS

- クラスは `gofile__*`（カード・グリッド、**Tokyomotion/85po のカードも共用**）・`tm*`（一覧の上の帯・札・保存ボタン）・
  `ufetch*`（保存シート）。汎用の `.btn` `.chip` `.chips` `.field` `.textarea` `.selectable` `.main` は殻側の CSS。
- 色・間隔・角丸は **M3 Expressive のトークン**（`--primary` `--on-primary` `--surface-mid` `--secondary-container`
  `--s-1…` `--shape-*` `--t-*` `--d-*`/`--ease-*` …）を前提にしている。実物は nas-app `web/src/styles/tokens.css`、
  ほぼ同じものが **m3e-kit**（https://github.com/temmie0232/m3e-kit の `starter/styles/`）にある。
  ホストが別のデザイン体系なら、この2つの CSS の `var(--…)` を読み替える（`--font-mono` は m3e-kit には無く
  `var(--font-num)`、`--save-layer` は gofile.css が自前で定義）。
- 写真の上に乗る札・保存ボタンは**テーマに追従しない固定の黒/白**（意図的）。直さない。

## 守ること

- 画面側でもサムネ URL のホストを確かめている（`isSafeThumbnailUrl` など）。外す変更は入れない。
- 一覧に `dangerouslySetInnerHTML` を使わない（題名はサイト由来の文字列）。
- 85po の `views` は `number | null`（0 にしない）。ページをまたいで同じ動画が出るので、次のページを足すときは `id` で重複を除く。
- サイトの HTML の形が変わると 502 になる。**まず仕様書の「変更時に直す場所」** と、各 `tests/test_*.py` の HTML 断片を見る。
  取得先に負荷をかけない作り（キャッシュ・失敗記憶・ブラウザ風 UA）を崩さない。
- 保存の既定先と「タップでは動画を落とさない（Gofile は公式ページへ遷移するだけ）」は仕様。

## 検証（このリポジトリを切り出した時点）

```bash
cd server
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt   # 無ければ pip でも可
.venv/bin/python -m pytest tests/test_tokyomotion.py tests/test_po85.py          # 229 passed（ネットなし）
```

- `test_tokyomotion.py` / `test_po85.py`: パース・ルータ（400/401/502 の形）・キャッシュ。**単体で通る**。
  `tests/conftest.py` が `app.auth` の代役と `client`/`token`/`auth` fixture を作る。ホストに組み込んだらホストの
  conftest に寄せて、この conftest は捨てる。
- `test_gofile_gallery.py`（`app.db` と `settings`）、`test_urlfetch_po85.py`（`settings` と保存先）は**ホスト側の
  db/config が要るので、この repo 単体では走らない**。組み込み先で通すこと。
- web は型検査・描画ともこの repo 単体では確認していない（殻が無いため）。組み込み先で `tsc` と実機表示を確認する。

## 経緯の一次資料

文中の「§」「仕様書」は nas-app の `docs/refs/gofile-thumbnail-gallery-spec.md`（このリポジトリには無い）を指す。
判断の理由は仕様書3本に書いてあるので、**ここに無い事情が気になったら nas-app（github.com/temmie0232/nas-app）の git 履歴**を見る。
