/* メディアタブの動画サイト（Tokyomotion・85po）の一覧。サイトの動画一覧を
 * 並び替え×期間で引いて、気に入ったものを NAS に保存する。見るのは「プライベート」
 * （保存先はどちらも Gofile と同じ `private/tmp/{題名}/`）。
 *
 * ★サイトごとの違いは `SiteConfig` に出す★ 並び替えと期間の名前・使える組み合わせ・
 * 画像のホスト・取得 API。カードの形・無限スクロール・保存の扱いは共通。
 *
 * 一覧はサーバが取る（10分覚える）。この画面は読むだけで、保存は一覧の `url` を既存の
 * 「URLから保存」（lib/urlfetch.ts → /api/media/fetch）に渡すだけ。進捗・中止・失敗の理由は
 * そちらの一覧（シート）に出る。
 *
 * ★タップはサイトのページを開くだけ★ Gofile のカードと同じ作法（外へ出る矢印つき）。
 * 保存は各カードの右上の明示ボタン（長押しは使わない）。 */
import { memo, useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { ApiError } from '../../lib/api'
import { usePaneActive } from '../../lib/mediaPane'
import { haptic } from '../../lib/motion'
import { toast } from '../../lib/toast'
import * as urlfetch from '../../lib/urlfetch'
import { Empty, Icon, Notice, useDelayed } from '../../components/ui'
import { usePullRefresh } from '../../components/PullRefresh'
import { useRevalidateOnShow } from '../../components/TabPage'
import { UrlFetch } from './UrlFetch'
import type { FetchJob, SiteItem, SiteListResp, SiteQuery } from './types'

export type SiteConfig = {
  /** 画面の識別（key と、ログ・テストの名前に使う） */
  id: string
  list: (q: SiteQuery) => Promise<SiteListResp>
  sorts: { id: string; label: string }[]
  periods: { id: string; label: string }[]
  /** 並び替えによっては期間が意味を持たない（85po の新着）。false の間は期間の帯を出さない */
  usesPeriod?: (sort: string) => boolean
  defaultSort: string
  defaultPeriod: string
  /** サムネの https ホスト（と、あれば先頭のパス）。外れたカードは出さない */
  thumbHost: string
  thumbPath?: string
  /** 動画ページのホスト。カードのリンクに入れる前に確かめる */
  pageHosts: string[]
}

function safe(url: string | null, hosts: string[], path = ''): url is string {
  if (!url) return false
  try {
    const u = new URL(url)
    return u.protocol === 'https:' && hosts.includes(u.hostname) && u.pathname.startsWith(path)
  } catch {
    return false
  }
}

function views(n: number | null): string {
  if (n == null) return ''
  if (n >= 10_000) return `${(n / 10_000).toFixed(n >= 100_000 ? 0 : 1)}万回`
  return `${n.toLocaleString()}回`
}

type Feed = {
  items: SiteItem[]
  page: number
  hasMore: boolean
  loading: boolean
  error: string | null
}

const EMPTY: Feed = { items: [], page: 0, hasMore: false, loading: true, error: null }

function SiteListInner({ site }: { site: SiteConfig }) {
  const [sort, setSort] = useState(site.defaultSort)
  const [period, setPeriod] = useState(site.defaultPeriod)
  const [feed, setFeed] = useState<Feed>(EMPTY)
  const [showUrl, setShowUrl] = useState(false)

  const scroller = useRef<HTMLDivElement>(null)
  const sentinel = useRef<HTMLDivElement>(null)
  /** 古い応答が新しい絞り込みの一覧を上書きしないための札 */
  const gen = useRef(0)

  const jobs = useSyncExternalStore(urlfetch.subscribe, urlfetch.getJobs, urlfetch.getJobs)
  const running = useSyncExternalStore(urlfetch.subscribe, urlfetch.activeJobs, urlfetch.activeJobs)

  /** 1ページ読む。1ページ目は置き換え、2ページ目以降は継ぎ足し（同じ id は落とす） */
  const load = useCallback(
    async (page: number, keep = false) => {
      const mine = ++gen.current
      setFeed((f) => (page === 1 && !keep ? { ...EMPTY } : { ...f, loading: true, error: null }))
      try {
        const r = await site.list({ sort, period, page })
        if (mine !== gen.current) return
        setFeed((f) => {
          const base = page === 1 ? [] : f.items
          const seen = new Set(base.map((i) => i.id))
          return {
            items: [...base, ...r.items.filter((i) => !seen.has(i.id))],
            page: r.page,
            hasMore: r.hasMore,
            loading: false,
            error: null,
          }
        })
      } catch (e) {
        if (mine !== gen.current) return
        const msg = e instanceof ApiError ? e.message : '一覧を取得できませんでした'
        setFeed((f) => ({ ...f, loading: false, error: msg }))
      }
    },
    [site, sort, period],
  )

  useEffect(() => {
    void load(1)
  }, [load])

  /* 保存の進み具合は裏で取り直す（lib/urlfetch.ts は走っているものがあれば自分で追う） */
  useEffect(() => {
    void urlfetch.refresh()
  }, [])

  const loadMore = useCallback(() => {
    if (feed.loading || !feed.hasMore || feed.error) return
    void load(feed.page + 1)
  }, [feed.loading, feed.hasMore, feed.error, feed.page, load])

  /* 一覧の終わりが見えたら続きを取る（Gofile と同じ作法。root は実際にスクロールする .main） */
  useEffect(() => {
    const el = sentinel.current
    if (!el || !feed.hasMore) return
    const io = new IntersectionObserver((es) => es[0]?.isIntersecting && loadMore(), {
      root: scroller.current,
      rootMargin: `${Math.round(window.innerHeight * 0.8)}px`,
    })
    io.observe(el)
    return () => io.disconnect()
  }, [feed.hasMore, loadMore, feed.items.length])

  const refresh = useCallback(() => load(1, true), [load])
  const paneActive = usePaneActive()
  const puller = usePullRefresh({ scroller, onRefresh: refresh, idle: !paneActive })
  useRevalidateOnShow(() => void refresh(), 10 * 60_000)

  const save = useCallback(async (item: SiteItem) => {
    haptic('light')
    try {
      const r = await urlfetch.submit([item.url])
      const bad = r.rejected[0]
      if (bad) toast(bad.error, { alert: true })
      else toast('保存を始めました。プライベートで見られます')
    } catch (e) {
      haptic('error')
      toast(e instanceof ApiError ? e.message : '保存を始められませんでした', { alert: true })
    }
  }, [])

  const first = feed.loading && feed.items.length === 0
  const showSkeleton = useDelayed(first)
  const stateOf = (item: SiteItem): FetchJob | undefined => jobs.find((j) => j.url === item.url)

  return (
    <>
      <div className="main" ref={scroller}>
        <div className="tm">
          <div className="tm__top">
            <button
              type="button"
              className="btn btn--sm tm__url"
              onClick={() => {
                haptic('light')
                setShowUrl(true)
              }}
            >
              <Icon name="download" />
              {running.length > 0 ? `URLから保存（${running.length}件を処理中）` : 'URLから保存'}
            </button>
          </div>

          <div className="chips tm__chips">
            {site.sorts.map((s) => (
              <button
                key={s.id}
                type="button"
                className="chip"
                aria-pressed={sort === s.id}
                onClick={() => {
                  if (sort !== s.id) haptic('selection')
                  setSort(s.id)
                }}
              >
                {s.label}
              </button>
            ))}
          </div>
          {(site.usesPeriod?.(sort) ?? true) && (
            <div className="chips tm__chips">
              {site.periods.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className="chip"
                  aria-pressed={period === p.id}
                  onClick={() => {
                    if (period !== p.id) haptic('selection')
                    setPeriod(p.id)
                  }}
                >
                  {p.label}
                </button>
              ))}
            </div>
          )}

          {feed.error && (
            <Notice alert onRetry={() => void load(feed.items.length === 0 ? 1 : feed.page + 1)}>
              {feed.error}
            </Notice>
          )}

          {first ? (
            showSkeleton ? (
              <SiteSkeleton />
            ) : null
          ) : feed.error && feed.items.length === 0 ? null : feed.items.length === 0 ? (
            <Empty page title="表示できるものがまだありません" />
          ) : (
            <>
              <div className="gofile__grid">
                {feed.items.map((it) => (
                  <SiteCard key={it.id} site={site} item={it} job={stateOf(it)} onSave={save} />
                ))}
              </div>
              {feed.hasMore && <div ref={sentinel} aria-hidden="true" style={{ height: 1 }} />}
            </>
          )}
        </div>
      </div>
      {puller}
      {showUrl && <UrlFetch onClose={() => setShowUrl(false)} />}
    </>
  )
}

/** カード1枚。**サムネの失敗は出さない**（サイト側の都合で消えた動画の枠が空くより、詰めたほうが良い） */
const SiteCard = memo(function SiteCard({
  site,
  item,
  job,
  onSave,
}: {
  site: SiteConfig
  item: SiteItem
  job: FetchJob | undefined
  onSave: (i: SiteItem) => void
}) {
  const [failed, setFailed] = useState(false)
  if (
    failed ||
    !safe(item.thumbnail, [site.thumbHost], site.thumbPath) ||
    !safe(item.url, site.pageHosts)
  ) {
    return null
  }

  const live = job ? urlfetch.isActive(job) : false
  const done = job?.state === 'done'
  const meta = [
    views(item.views),
    item.rating != null ? `${item.rating}%` : '',
    item.added,
  ].filter(Boolean)

  return (
    <div className="gofile__card">
      <a className="gofile__cardlink" href={item.url} target="_blank" rel="noopener noreferrer">
        <span className="gofile__thumbwrap">
          <img
            className="gofile__thumb"
            src={item.thumbnail}
            alt=""
            loading="lazy"
            decoding="async"
            referrerPolicy="no-referrer"
            onError={() => setFailed(true)}
          />
          {item.hd && <span className="tm__hd">HD</span>}
          <span className="tm__dur">{item.duration}</span>
        </span>
        <span className="gofile__title">{item.title}</span>
        <span className="gofile__meta">
          {meta.map((m, i) => (
            <span key={i}>{m}</span>
          ))}
        </span>
      </a>
      {/* <a> の中に <button> は置けないので、カード直下に重ねる（Gofile のアバターと同じ） */}
      <button
        type="button"
        className={done ? 'tm__save tm__save--done' : 'tm__save'}
        aria-label={live ? '保存中' : done ? '保存済み（もう一度保存）' : '保存'}
        disabled={live}
        onClick={() => onSave(item)}
      >
        {live ? <span className="btn__busy" aria-hidden="true" /> : <Icon name={done ? 'check' : 'download'} />}
      </button>
    </div>
  )
})

function SiteSkeleton() {
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

/* ★memo で包む★ 親（MediaHub）は切替のたびに再レンダする。タブは残す作りなので
   包まないと裏の一覧まで毎回突き合わせ直しになる（GofileGallery と同じ理由）。
   site は呼び出し側のモジュール直下の定数なので identity が安定している */
export const SiteList = memo(SiteListInner)
