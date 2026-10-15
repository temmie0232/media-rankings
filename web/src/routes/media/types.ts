/* nas-app の routes/media/types.ts から、ランキング一覧と「URLから保存」の型だけを抜いたもの。 */


/* ---------------------------------------------------------- URL から保存
   server/app/urlfetch.py の Job.as_dict()。 */

export type FetchState = 'queued' | 'downloading' | 'processing' | 'done' | 'error' | 'cancelled'

export type FetchJob = {
  id: number
  url: string
  state: FetchState
  /** 取れるまでは空文字 */
  title: string
  percent: number
  done_bytes: number
  /** 分からないときは 0 */
  total_bytes: number
  /** bytes/sec */
  speed: number
  eta: number | null
  error: string
  /** gofile が保存済みだったとき true。動画1本の URL では常に false */
  duplicate: boolean
  /** gofile のフォルダのときだけ。動画1本の URL では 0 */
  files_total: number
  files_done: number
  /** 保存したフォルダ（例 private/tmp/タイトル）。done のとき必ず入る。写真・動画の棚ではなくファイルの private/tmp */
  dest: string
  created: number
  finished: number
}

export type FetchStartResp = {
  jobs: FetchJob[]
  rejected: { url: string; error: string }[]
}

/* ---------------------------------------------------------- 動画サイトの一覧
   Tokyomotion（server/app/tokyomotion.py）と 85po（server/app/po85.py）は同じ形で返す。
   サイトの「動画一覧」を並び替えと期間で引いたもの。保存は一覧の `url` を
   「URLから保存」（/api/media/fetch）に渡すだけ。 */

export type SiteItem = {
  id: string
  title: string
  /** 動画ページ。保存にも外のページにもこれを使う */
  url: string
  /** サイトの画像置き場の https だけ（画面側でももう一度ホストを確かめる） */
  thumbnail: string | null
  /** サイトの表記のまま（`00:54` / `01:07:42`） */
  duration: string
  /** 再生数。略記（63K）しか取れないサイトでは概数。取れなければ null */
  views: number | null
  /** 好評率（%）。取れなければ null */
  rating: number | null
  /** サイトの表記のまま（`5 days ago` / `5日前`） */
  added: string
  hd: boolean
}

export type SiteListResp = {
  items: SiteItem[]
  page: number
  hasMore: boolean
  fetchedAt: string
}

/** 一覧の引き方。並び替えと期間の名前はサイトごとに違う（SiteList.tsx の設定が持つ） */
export type SiteQuery = { sort: string; period: string; page: number }
