/* §9.3「画像URLをDOMへ設定する前に、許可ホストとHTTPSを検証する」。
   サーバ (app/gofile/security.py) と同じ判定を、念のためクライアント側でも
   もう一段行う。サーバが既に絞っているので通常は常に true になるが、
   ここが唯一 <img src> に値を渡す手前の場所なので、二重の壁として置く。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../../lib/api'
import { shortDateTime } from '../../lib/format'
import { haptic } from '../../lib/motion'
import { toast } from '../../lib/toast'
import type { GalleryItem, GalleryListResp, SaveResp } from './types'

const GOFILE_THUMB_HOST_SUFFIX = '.gofile.io'
const GOFILE_HOST = 'gofile.io'

export function isSafeThumbnailUrl(url: string): boolean {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return false
  }
  if (parsed.protocol !== 'https:') return false
  const host = parsed.hostname.toLowerCase()
  return host === GOFILE_HOST || host.endsWith(GOFILE_THUMB_HOST_SUFFIX)
}

export function isSafeGofileUrl(url: string): boolean {
  return /^https:\/\/gofile\.io\/d\/[A-Za-z0-9]+\/?$/.test(url)
}

/** 投稿者アバター用。twimg.com等、gofileの許可ホスト外なのでホストは見ず、
    HTTPSであることだけ確認する（サーバ側 security.is_https_url と同じ判定）。 */
export function isSafeAvatarUrl(url: string): boolean {
  try {
    return new URL(url).protocol === 'https:'
  } catch {
    return false
  }
}

/** save/save-url の応答をトーストに変換する。長押し保存とURL貼り付け保存の
    両方で使う（Card.tsx / GofileGallery.tsx）。

    過去に同じコードを保存済み（`duplicate`）なら、保存はまだ実行していない
    ので警告として出し、「もう一度保存」を押したときだけ `retry`（force付きの
    再送）を呼ぶ。 */
export function notifySaveResult(r: SaveResp, retry: () => void): void {
  if (r.duplicate) {
    haptic('error')
    const when = r.lastSavedAt ? shortDateTime(r.lastSavedAt) : '日時不明'
    toast(`既にダウンロード済みです（前回 ${when}）`, {
      alert: true,
      action: { label: 'もう一度保存', run: retry },
    })
    return
  }
  haptic('success')
  toast(
    r.queued
      ? '保存待ちに追加しました。前の保存が終わり次第、順番に処理されます'
      : '保存を開始しました。private/tmp に反映まで数分かかります',
  )
}

type Fetcher = (cursor: string | null, limit: number) => Promise<GalleryListResp>

/** カーソルページングの一覧取得を共通化するフック。「新着」「人気」
    「投稿者別一覧」の3箇所で同じ取得ロジックを書かないための切り出し
    （元は GofileGallery.tsx に1本だけあったもの）。

    `feedKey` が変わるたびに1ページ目から取り直す（新着⇔人気の切替や、
    投稿者別一覧を開いたときに使う）。取得中に古いアイテムが一瞬
    残って見えないよう、切替時は即座に空にしてから取り直す。 */
export function useCursorFeed(feedKey: string, fetcher: Fetcher, pageSize = 30) {
  const [items, setItems] = useState<GalleryItem[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [lastSyncedAt, setLastSyncedAt] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const cursorRef = useRef<string | null>(null)
  const fetching = useRef(false)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const load = useCallback(
    async (reset: boolean) => {
      if (fetching.current) return
      fetching.current = true
      if (reset) setError(null)
      try {
        const r = await fetcherRef.current(reset ? null : cursorRef.current, pageSize)
        setItems((prev) => (reset ? r.items : [...prev, ...r.items]))
        cursorRef.current = r.nextCursor
        setHasMore(r.nextCursor !== null)
        setLastSyncedAt(r.lastSyncedAt)
      } catch (e) {
        setError(e instanceof ApiError ? e.message : '読み込みに失敗しました')
      } finally {
        fetching.current = false
        setLoading(false)
      }
    },
    [pageSize],
  )

  useEffect(() => {
    cursorRef.current = null
    setItems([])
    setLoading(true)
    void load(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [feedKey])

  const loadMore = useCallback(() => {
    if (!hasMore || fetching.current) return
    void load(false)
  }, [hasMore, load])

  return { items, setItems, hasMore, lastSyncedAt, loading, error, load, loadMore }
}
