# 85po 一覧 API（`/api/po85/ranking`）と保存

メディアタブの「85po」切替で、Tokyomotion と同じ「ランキング形式の一覧」を出すための API。
`https://www.85po.net/ja/…` の一覧ブロック（非同期形式）の1ページ分を取って整える。
**保存はこの API ではしない**。一覧の `url` を既存の `POST /api/media/fetch`（`urlfetch.py`）へ渡す。
ただし **85po は yt-dlp では取れない**ので、`urlfetch.py` の専用の道（`_run_po85`）で落とす（下の「保存」）。

- 実装: `server/app/po85.py`（一覧の取得・パース・キャッシュ、動画ページの解決）/
  `server/app/routers/po85.py` / 保存は `server/app/urlfetch.py` の `_run_po85`・`_stream_file`
- テスト: `server/tests/test_po85.py`（一覧・動画ページのパース）、`server/tests/test_urlfetch_po85.py`（保存の道）。
  どちらもネットなし。HTML は自作の架空の題名
- 手本は [tokyomotion-api.md](tokyomotion-api.md)。入出力の形・キャッシュ・エラーの流儀はそろえてある

## エンドポイント

`GET /api/po85/ranking?sort=popular&period=week&page=1`（要認証）

| パラメータ | 値 | 既定 |
|---|---|---|
| `sort` | `popular`（人気 `/most-popular/`）/ `rated`（高評価 `/top-rated/`）/ `recent`（新着 `/latest-updates/`） | `popular` |
| `period` | `today` / `week` / `month` / `all` | `week` |
| `page` | 1〜50 の整数 | `1` |

不正な値は **400**（日本語のメッセージ）。3つとも自前で検証している（型検証に任せると 422 になる）。
`recent` は期間を持たないので `period` は無視する（不正な値は 400 のまま）。

**期間の意味が Tokyomotion と違う。** Tokyomotion の `period` は「その期間に**追加された**動画の中での順位」。
85po の `popular` / `rated` の `period` は「**その期間の再生数（評価）で並べた全動画**」
（`sort_by=video_viewed_today|week|month|`（全期間）、`rating_today|week|month|`（全期間））。
だから `period=today` でも古い動画が上位に出てくる（`added` が `2年前` など）。件数（最終ページ番号）は期間によらず同じ。

```json
{
  "items": [
    {
      "id": "1001",
      "title": "…",
      "url": "https://www.85po.net/ja/video/1001/ao-no-todai/",
      "thumbnail": "https://www.85po.net/contents/videos_screenshots/1000/1001/320x180/1.jpg",
      "duration": "2:30",
      "views": 63000,
      "rating": 87,
      "added": "5日前",
      "hd": true
    }
  ],
  "page": 1,
  "hasMore": true,
  "fetchedAt": "2026-10-12T12:00:00+09:00"
}
```

- `url` は **slug 付き**。slug 無しの `/ja/video/{id}/` は **404** になる。サイトは**間違った slug を正しい slug へ
  301 で飛ばす**ので、カードに slug が無いときは `-` を入れて返す（`/ja/video/{id}/-/`）
- `views` は `63K`→63000、`1.2M`→1200000、`1,234`→1234 と数値化した**概数**。読めなければ **`null`**（0 にはしない）。
  型は `int | null`
- `thumbnail` は **https・ホスト `www.85po.net`・パスが `/contents/` 始まり**のものだけ。それ以外は `null`（項目は残す）。
  実画像はカードの `data-original`（`src` は読み込み前の 1px gif）
- `rating`（％の数字）は無ければ `null`。`hd` は `<span class="is-hd …">`（HD / 2K / 4K のどれか）の有無
- `duration` は `m:ss` / `h:mm:ss`。形が違えば `null`
- `hasMore` は「次へ」リンクの有無（無ければページャの `from:N` に今より先があるか）。**`page=50` では常に `false`**
- `fetchedAt` は取得した時刻（JST）。キャッシュに当たると前のまま
- 失敗は **502**（日本語）。サイトが落ちている・429/403・ブロックの形が想定外（ブロック名が変わると**空の 200**
  が返る）で1件も読めないとき。本当に空のページ（ブロックはあるがリンクが1つも無い）は 200 で `items: []`
- **ページをまたいで同じ動画が出ることがある**（サイト側の並びの揺れ。人気・高評価の今日/週で 1〜4 件）。
  web 側で次のページを足すときは `id` で重複を除くこと。新着は重ならなかった

## 取得とキャッシュ

- **取得先は固定**。URL は `build_url()` が
  `https://www.85po.net/ja/{most-popular|top-rated|latest-updates}/?mode=async&function=get_block&block_id=…&sort_by=…&from={page}`
  と組む。利用者の入力は上の許可リストの名前でしか URL に入らない（SSRF 対策）。リダイレクトは同じサイトの https だけ追う
- **非同期ブロック形式**を使う理由: ヘッダのおすすめカルーセル（`drop-card`）が混ざらず、1ページ分 **24 件**のブロックだけが
  返る。`from=1` は `from` 省略と同じ（`from=0` や `abc` も1ページ目）。通常ページの `/ja/most-popular/2/` 形式もあるが使わない
- 範囲外のページ（最終ページ 567 より先など）は**空の 404**。これは `items: []`・`hasMore: false` で 200 にする。
  404 でも本文があれば 502、**ブロック名が違うときの「空の 200」も 502**（取り違えて「空」にしない）
- User-Agent はブラウザ風（`Mozilla/5.0 (X11; Linux aarch64) …`）、timeout 20 秒
- **プロセス内 TTL キャッシュ**（キー = sort/period/page、10 分。新着は期間をキーに入れない）。同じキーが同時に来ても
  取りに行くのは 1 回。残すのは整形済みの結果だけ。**失敗は 30 秒だけ覚える**
- パースは標準ライブラリの正規表現（bs4/lxml は入っていない）。1件は `<div class="item ">` の中の
  `<a href>` / `<img data-original>` / `<strong class="title">` / `div.duration` / `div.views` / `div.rating` / `div.added`

## 保存（専用の道）

`POST /api/media/fetch` に 85po の動画ページの URL（`https://www.85po.net/ja/video/{id}/{slug}/`。
`85po.net`・`http`・`/en/` なども可。判定は `po85.is_site_url`）を渡すと、`urlfetch._run` が yt-dlp ではなく
`_run_po85` に流す。置き場・進捗・中止・後片付けは yt-dlp の道と同じ（`private/tmp/{題名}/{題名}.mp4`）。

- **yt-dlp は 85po を取れない**: generic が KVS と判定して `Unable to extract flashvars` で失敗する
  （埋め込み URL も同じ）。そこで `po85.resolve_video` が動画ページから画質ごとの URL を割り出す
  - JSON-LD の `VideoObject`（`name` / `duration` / `contentUrl`＝720p の1本だけ）
  - インライン JS のプレイヤー設定（kt_player。`flashvars` という語は無い）の `video_url`（480p）・`video_alt_url`（720p）・
    `video_alt_url2`（1080p）…。`*_text` が画質、`*_codec` が `av1` など。URL に `?v-acctoken=…` が付く
  - どちらかが欠けても取れる方で動く（同じファイルは重複させない）。480p しか無い動画もある（`contentUrl` も無い）
- **最高画質を選ぶ**（`po85.pick_format`）。同じ解像度なら AV1 より H.264 系を優先、無ければ AV1
- **AV1 の注意**: 実測では **480p だけが H.264、720p・1080p は AV1**（`video_alt_url_codec: 'av1'`）。つまり最高画質を
  選ぶと**ほぼ AV1 が入る**。AV1 は端末にハードウェアデコーダが無いと再生が重い・再生できないことがある
  （ファイル画面の再生で詰まるなら、`pick_format` / `_format_key` を H.264 優先に変えて 480p を取る）
- **Cookie も Referer も要らない**（実測）。`get_file` は 302 で**別ホストの CDN**（`remote_control.php?file=…&acctoken=…`）
  へ飛ばし、そこが本体（`206`/`200`、`Content-Length`、`Range` 対応）を返す。`v-acctoken` が無くても取れた。
  コードは Referer（サイトの origin）を付け、動画ページと同じ httpx クライアントを使う（Cookie は自然に共有される）
- **SSRF**: 動画ファイルの URL は**飛び先ごとに `check_url`**（グローバルアドレスだけ。LAN・tailnet・ループバックは弾く）、
  https 以外へは飛ばない。転送は5回まで。httpx に任せず `urlfetch._open_stream` が手で追う
- 流れ: 動画ページの解決（`queued` のまま）→ `downloading`（`total_bytes` は `Content-Length`。`percent` `speed` `eta` を更新）→
  `processing` → `done`。`dl_{id}_{epoch}` に落として `_save_into_folder`、作業ディレクトリは必ず消す
- 取り込み前に空きを見る（`MIN_FREE_BYTES` + 大きさ）。`settings.max_upload_bytes` を超えれば失敗（大きさが分からない
  ストリームでも書きながら止める）。HTML が返ったら失敗
- **途中で切れたら取れたところから `Range` で再開**する（サーバが無視したら最初から）。1バイトも進まない失敗が
  5回続いたら諦める（待ちは 1→2→4→8→10 秒。中止はその待ちの間も即効く）
- **会員限定の動画**: プレイヤー欄にログインへの誘導があれば「ログインが必要な動画は取れません」。公開の一覧には
  出てこず、**実物では未確認**（KVS の作りからの推測）。ヘッダ等には常に `login-required` のリンクがあるので、
  ページ全体では見ていない
- slug 無しの URL（`/ja/video/{id}/`）を貼っても、`resolve_video` が `-` を入れて 301 を追うので取れる

## 実測した性質（2026-10-12）

- 一覧は 24 件固定、最終ページは 567（期間・並びによらず）。ブラウザ風の UA だけで 200（年齢確認ページは出なかった）。
  HTTP/1.1 の httpx で通る
- 動画ページは `/ja/video/{id}/{slug}/`。`/video/{id}/{slug}/`（言語なし）と `http` はそのまま 200、`85po.net`（www なし）は 301
- 画質: 480p は H.264、720p・1080p は AV1。1分の動画（1080p・AV1）が約 2.7MB で、Pi から約 3 秒で保存できた（サイズは `Content-Range` の全体と一致）

## 変更時に直す場所

- 一覧が読めなくなった（502）: まず `po85._parse_item` / `parse_listing` と `test_po85.py` の `_card` / `_block`
- ブロック名・並び（`sort_by`）が変わった: `po85._LISTS`
- 動画が取れなくなった: `po85.parse_video_page` / `_player_formats`（プレイヤー設定のキー名）と `test_po85.py` の `_player` / `_ld`
- 画質の選び方（AV1 を避けたい）: `po85.pick_format` / `_format_key`
- 保存の挙動（再開・容量・転送）: `urlfetch._stream_file` / `_open_stream` と `test_urlfetch_po85.py`
- web 側の型: `views` は `number | null`
