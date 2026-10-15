/* 投稿者別一覧（ドリルダウン）。Kakei.tsx が Ledger/Assets を切り替えるのと
   同じ流儀 — onBack + useBackGuard（components/ui.tsx）で画面ごと差し替える。

   ★うちのDBに既にある検証済みアイテムだけを絞り込んで見せる★
   gofile-douga.com への新規ライブ取得はしない（docs/gofile-gallery-api.md
   の設計判断）。したがって見えるのは「新着/人気経由で既に見つけて
   検証済みの、この投稿者の投稿」だけで、その投稿者の全投稿ではない。 */
import { useCallback, useEffect, useRef } from 'react'
import { Empty, Icon, Notice, useBackGuard, useDelayed } from '../../components/ui'
import { listByAccount, reportBroken } from './api'
import { Card } from './Card'
import { useCursorFeed } from './lib'
import type { AccountRef } from './types'

const PAGE_SIZE = 30

export function AccountView({ account, onBack }: { account: AccountRef; onBack: () => void }) {
  // 端末の戻るで一覧へ。依存に onBack を入れない仕掛けは ui.tsx が持っている
  useBackGuard(true, onBack)

  const fetcher = useCallback(
    (cursor: string | null, limit: number) => listByAccount(account.id, cursor, limit),
    [account.id],
  )
  const { items, setItems, hasMore, loading, error, load, loadMore } = useCursorFeed(
    account.id,
    fetcher,
    PAGE_SIZE,
  )

  const scroller = useRef<HTMLDivElement>(null)
  const sentinel = useRef<HTMLDivElement>(null)

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

  const handleBroken = useCallback(
    (id: number) => {
      setItems((prev) => prev.filter((i) => i.id !== id))
      // 裏方の後始末。失敗してもこの画面の見た目には影響させない
      void reportBroken(id).catch(() => {})
    },
    [setItems],
  )

  // このビューは既に1つの投稿者に絞っているので、カードのアバターを
  // 押しても行き先は同じ画面になる。何もしない受け口で十分
  const noopOpenAccount = useCallback(() => {}, [])

  const first = loading && items.length === 0
  const showSkeleton = useDelayed(first)

  return (
    <div className="main main--padded" ref={scroller}>
      <div className="gofile">
        <div className="section__head">
          <button className="btn btn--sm btn--ghost" onClick={onBack} aria-label="一覧へ戻る">
            <Icon name="back" />
          </button>
          {account.avatarUrl && (
            <img className="gofile__accountavatar" src={account.avatarUrl} alt="" />
          )}
          <h2 className="header__title">{account.id}</h2>
        </div>

        {error && (
          <Notice alert onRetry={() => void load(true)}>
            {error}
          </Notice>
        )}

        {first ? (
          showSkeleton ? (
            <div className="gofile__grid" aria-hidden="true">
              {Array.from({ length: 8 }, (_, i) => (
                <div className="gofile__card gofile__card--skel" key={i}>
                  <span className="gofile__thumbwrap">
                    <span className="gofile__thumb gofile__thumb--skel" />
                  </span>
                </div>
              ))}
            </div>
          ) : null
        ) : items.length === 0 ? (
          <Empty page title="この投稿者の項目はまだありません" />
        ) : (
          <>
            <div className="gofile__grid">
              {items.map((it) => (
                <Card key={it.id} item={it} onBroken={handleBroken} onOpenAccount={noopOpenAccount} />
              ))}
            </div>
            {hasMore && <div ref={sentinel} aria-hidden="true" style={{ height: 1 }} />}
          </>
        )}
      </div>
    </div>
  )
}
