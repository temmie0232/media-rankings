/* Gofileサムネイルギャラリー (docs/refs/gofile-thumbnail-gallery-spec.md §9)。
   取得元は gofile-worker コンテナが定期的に集めているだけで、この画面は
   /api/gofile-gallery系を読むだけ。タップでは動画ファイルを一切扱わない —
   選ぶと公式Gofileページへ新しいタブで遷移するだけの、外部一覧の窓。
   例外は長押し→保存だけ（routes/gofile/Card.tsx）。動画本体を
   NASのprivate/tmpへ落とす、この画面で唯一の「ダウンロードする」経路。

   2026-07-30〜: 新着に加えて人気(24h)ランキングと投稿者別一覧も見られる
   （docs/gofile-gallery-api.md）。取得ロジック（カーソルページング・
   無限スクロール）は3箇所で共有したいので gofile/lib.ts の
   useCursorFeed に切り出してある。投稿者別一覧はドリルダウンなので、
   Kakei.tsx が Ledger/Assets を切り替えるのと同じ流儀
   （onBack + useBackGuard）で画面ごと差し替える。 */
import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../lib/api'
import { shortDateTime } from '../lib/format'
import { usePaneActive } from '../lib/mediaPane'
import { haptic } from '../lib/motion'
import { toast } from '../lib/toast'
import { Empty, Icon, Notice, useDelayed } from '../components/ui'
import { usePullRefresh } from '../components/PullRefresh'
import { useRevalidateOnShow } from '../components/TabPage'
import { listGallery, listRankings, reportBroken, requestSync, saveUrl } from './gofile/api'
import { AccountView } from './gofile/AccountView'
import { Card } from './gofile/Card'
import { useCursorFeed, notifySaveResult } from './gofile/lib'
import type { AccountRef } from './gofile/types'

const PAGE_SIZE = 30
const FEEDS = ['new', 'ranking'] as const
type Feed = (typeof FEEDS)[number]
const FEED_LABEL: Record<Feed, string> = { new: '新着', ranking: '人気' }

function GofileGalleryInner() {
  const [account, setAccount] = useState<AccountRef | null>(null)

  if (account) {
    return <AccountView account={account} onBack={() => setAccount(null)} />
  }
  return <FeedView onOpenAccount={setAccount} />
}

function FeedView({ onOpenAccount }: { onOpenAccount: (a: AccountRef) => void }) {
  const [feed, setFeed] = useState<Feed>('new')
  const [syncing, setSyncing] = useState(false)
  const [pasteUrl, setPasteUrl] = useState('')
  const [pasting, setPasting] = useState(false)

  const { items, setItems, hasMore, lastSyncedAt, loading, error, load, loadMore } = useCursorFeed(
    feed,
    feed === 'new' ? listGallery : listRankings,
    PAGE_SIZE,
  )

  const scroller = useRef<HTMLDivElement>(null)
  const sentinel = useRef<HTMLDivElement>(null)

  /* 一覧の終わりが見えたら勝手に続きを取る（routes/Media.tsx と同じ作法）。
     root は .main（実際にスクロールする要素）にする */
  useEffect(() => {
    const el = sentinel.current
    if (!el || !hasMore) return
    const io = new IntersectionObserver((es) => es[0]?.isIntersecting && loadMore(), {
      root: scroller.current,
      rootMargin: `${Math.round(window.innerHeight * 0.8)}px`,
    })
    io.observe(el)
    return () => io.disconnect()
  }, [hasMore, loadMore, items.length])

  /* しばらく離れていたら黙って取り直す。まだ1ページ目しか見ていない時だけ
     （読み進めた位置を壊さないため。routes/Media.tsx と同じ考え方） */
  useRevalidateOnShow(() => {
    if (items.length <= PAGE_SIZE) void load(true)
  }, 90_000)

  const paneActive = usePaneActive()
  const puller = usePullRefresh({ scroller, onRefresh: () => load(true), idle: !paneActive })

  const handleBroken = useCallback(
    (id: number) => {
      setItems((prev) => prev.filter((i) => i.id !== id))
      // 裏方の後始末。失敗してもこの画面の見た目には影響させない
      void reportBroken(id).catch(() => {})
    },
    [setItems],
  )

  async function doSync() {
    setSyncing(true)
    try {
      await requestSync()
      haptic('success')
      toast('更新を開始しました。反映まで数分かかります')
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast('既に更新処理が実行中です', { alert: true })
      } else {
        haptic('error')
        toast(e instanceof ApiError ? e.message : '更新の開始に失敗しました', { alert: true })
      }
    } finally {
      setSyncing(false)
    }
  }

  /* URL貼り付け→保存。ギャラリーのDBに載っていない任意のgofile.ioリンクも
     保存できる（長押し保存はギャラリーの項目にしか使えないため）。
     `url` を引数で受けるのは、再送（「もう一度保存」）の時点で入力欄が
     既に空/別の値になっていても、最初に渡した対象へ正しく再送するため。 */
  async function doPasteSave(url: string, force = false) {
    setPasting(true)
    try {
      const r = await saveUrl(url, force)
      notifySaveResult(r, () => void doPasteSave(url, true))
      if (!r.duplicate) setPasteUrl('')
    } catch (e) {
      haptic('error')
      if (e instanceof ApiError && e.status === 400) {
        toast('gofileのURLの形式が正しくありません', { alert: true })
      } else {
        toast(e instanceof ApiError ? e.message : '保存を開始できませんでした', { alert: true })
      }
    } finally {
      setPasting(false)
    }
  }

  const first = loading && items.length === 0
  const showSkeleton = useDelayed(first)

  return (
    <>
      <div className="main" ref={scroller}>
        <div className="gofile">
          <div className="gofile__paste">
            <input
              className="input"
              type="text"
              inputMode="url"
              autoCapitalize="off"
              autoCorrect="off"
              placeholder="gofile.io/d/... のURL"
              value={pasteUrl}
              onChange={(e) => setPasteUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && pasteUrl.trim()) {
                  e.preventDefault()
                  void doPasteSave(pasteUrl.trim())
                }
              }}
            />
            <button
              type="button"
              className="btn btn--sm"
              disabled={pasting || !pasteUrl.trim()}
              onClick={() => void doPasteSave(pasteUrl.trim())}
            >
              保存
              {pasting && <span className="btn__busy" aria-hidden="true" />}
            </button>
          </div>

          <div className="seg" style={{ ['--seg-n' as string]: FEEDS.length }}>
            <span
              className="seg__ind"
              aria-hidden="true"
              style={{ transform: `translateX(calc(${FEEDS.indexOf(feed)} * 100%))` }}
            />
            {FEEDS.map((f) => (
              <button
                key={f}
                type="button"
                className="seg__btn"
                aria-pressed={feed === f}
                onClick={() => {
                  if (feed !== f) haptic('selection')
                  setFeed(f)
                }}
              >
                {FEED_LABEL[f]}
              </button>
            ))}
          </div>

          <div className="gofile__bar">
            <span className="gofile__synced muted">
              {lastSyncedAt ? `最終更新 ${shortDateTime(lastSyncedAt)}` : '未同期'}
            </span>
            <button
              type="button"
              className="btn btn--sm"
              disabled={syncing}
              onClick={() => void doSync()}
            >
              <Icon name="refresh" />
              更新
            </button>
          </div>

          {error && (
            <Notice alert onRetry={() => void load(true)}>
              {error}
            </Notice>
          )}

          {first ? (
            showSkeleton ? (
              <GallerySkeleton />
            ) : null
          ) : /* エラーのときは Notice だけにする（空と読めなかったは別物） */
          error && items.length === 0 ? null : items.length === 0 ? (
            <Empty page title="表示できるものがまだありません" />
          ) : (
            <>
              <div className="gofile__grid">
                {items.map((it) => (
                  <Card key={it.id} item={it} onBroken={handleBroken} onOpenAccount={onOpenAccount} />
                ))}
              </div>
              {hasMore && <div ref={sentinel} aria-hidden="true" style={{ height: 1 }} />}
            </>
          )}
        </div>
      </div>
      {puller}
    </>
  )
}

function GallerySkeleton() {
  return (
    <div className="gofile__grid" aria-hidden="true">
      {Array.from({ length: 8 }, (_, i) => (
        <div className="gofile__card gofile__card--skel" key={i}>
          <span className="gofile__thumbwrap">
            <span className="gofile__thumb gofile__thumb--skel" />
          </span>
        </div>
      ))}
    </div>
  )
}

/* ★memo で包むこと★（2026-09-04）
 *
 * タブは一度開いたら DOM に残る（components/TabPage.tsx）。表示を
 * `display:none` で切っているだけなので、**React の突き合わせは止まらない。**
 * App.tsx は navbar の板をスクロールで出し入れするために `solid` を持っており、
 * しきい値をまたぐたびに再レンダする。つまり memo が無いと
 * **「写真を上下に送っただけ」で裏に居る全タブが丸ごと作り直される。**
 *
 * 実測（Pi 4 の Chromium・本番ビルド・裏のファイルタブが 2,000 行のとき）:
 *   メディアを上下に10往復  … Script 15ms → 5,274ms（+263ms/回）
 *   家計↔メディアを10回切替 … Script 289ms → 4,592ms（+430ms/回）
 *
 * props を増やすときは identity の安定に気をつけること。渡すものが
 * 毎レンダ作り直されると memo は素通りする。 */
export const GofileGallery = memo(GofileGalleryInner)
