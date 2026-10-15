# TOKYOMOTION 一覧 API（`/api/tokyomotion/ranking`）

メディアタブの「TOKYOMOTION」切替で、Gofile ギャラリーと同じ「ランキング形式の一覧」を出すための API。
`https://www.tokyomotion.net/videos?o=..&t=..&page=..` の1ページ分を取って整える。
**保存はこの API ではしない**。一覧の `url` を既存の `POST /api/media/fetch`（`urlfetch.py`・yt-dlp、
`private/tmp/{題名}/` に保存。[media-api-new.md](media-api-new.md) の「URL から保存」）へ渡す。

- 実装: `server/app/tokyomotion.py`（取得・パース・キャッシュ）/ `server/app/routers/tokyomotion.py`
- テスト: `server/tests/test_tokyomotion.py`（ネットなし。HTML は自作の架空の題名）

## エンドポイント

`GET /api/tokyomotion/ranking?sort=popular&period=week&page=1`（要認証）

| パラメータ | 値 | 既定 |
|---|---|---|
| `sort` | `popular`(o=mv) / `rated`(o=tr) / `favorites`(o=tf) / `watching`(o=bw) / `recent`(o=mr) / `longest`(o=lg) | `popular` |
| `period` | `today`(t=t) / `week`(t=w) / `month`(t=m) / `all`(t=a) | `week` |
| `page` | 1〜50 の整数 | `1` |

不正な値は **400**（日本語のメッセージ）。型検証に任せると 422 になるので、3つとも自前で検証している。

```json
{
  "items": [
    {
      "id": "7046843",
      "title": "…",
      "url": "https://www.tokyomotion.net/video/7046843",
      "thumbnail": "https://cdn.tokyo-motion.net/media/videos/tmb220/7046843/1.jpg",
      "duration": "00:54",
      "views": 434576,
      "rating": 15,
      "added": "5 days ago",
      "hd": true
    }
  ],
  "page": 1,
  "hasMore": true,
  "fetchedAt": "2026-10-12T12:00:00+09:00"
}
```

- `url` は slug を付けず `/video/{id}` に正規化（yt-dlp はそれで取れる）
- `thumbnail` は **https かつ `cdn.tokyo-motion.net`** のものだけ。それ以外は `null`（項目は残す）
- `rating`（％の数字）は無ければ `null`。`hd` はサムネ内の HD アイコンの有無。`views` は桁区切りの有無どちらも読む
- `hasMore` は `Showing a to b of N videos.` の b < N（無ければページャの先のページ有無）。
  **`page=50` では常に `false`**（web が頼めるのは 50 まで）
- `fetchedAt` は取得した時刻（JST）。キャッシュに当たると前のまま
- 失敗は **502**（日本語）。サイトが落ちている・429/403・HTML の形が想定外（年齢確認ページなど）で1件も
  読めないとき。本当に空のページ（`No Videos Found.`）は 200 で `items: []`

## 取得とキャッシュ

- **取得先は固定**。URL は `build_url()` が `https://www.tokyomotion.net/videos?o=..&t=..&page=..` と組む。
  利用者の入力は上の許可リストの名前でしか URL に入らない（SSRF 対策）。リダイレクトは同じホストの https だけ追う
- User-Agent はブラウザ風（`Mozilla/5.0 (X11; Linux aarch64) …`）、timeout 20 秒
- **プロセス内 TTL キャッシュ**（キー = sort/period/page、10 分）。同じキーが同時に来ても取りに行くのは 1 回。
  残すのは整形済みの結果だけで生 HTML は持たない。api は uvicorn 1 プロセスなので再起動で消える
- **失敗は 30 秒だけ覚える**。429 のときに画面の再試行でサイトを叩き続けないため
- パースは標準ライブラリの正規表現（bs4/lxml は入っていない）。サイトの作りが変わって読めなくなると
  502 になる。まず `_parse_item` / `parse_listing` と `test_tokyomotion.py` の HTML 断片を見直す

## 実測した性質（2026-10-12）

- 1 ページは **20 件固定**。`page` はそのままクエリ名（`&page=2`）で、1 ページ目と重ならない
- 範囲外のページを頼むとサイトは**最終ページを返す**（空ではない）。現在のページ番号
  （ページャの `active`）が頼んだ番号と違うときは、重複を返さず `items: []` にしている
- ブラウザ風の UA だけで 200 が返り、年齢確認ページは出なかった（Pi から直接）。出るようになれば 502
