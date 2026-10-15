/* 「URL から保存」のシート。貼った URL の動画を NAS に落として、private/tmp のフォルダに入れる。
 *
 * ★進捗の持ち主はこの画面ではない★ lib/urlfetch.ts が React の外で持つ。
 * シートを閉じてもタブを移っても落とし続け、終わったときの一覧の取り直しと
 * 通知は Media.tsx が聞く。ここは覗き窓。
 *
 * サーバは1本ずつ順番に落とす（Pi の RAM が 1.2GB なので）。
 * 何件貼っても「待ち」が並ぶだけで、同時には走らない。 */
import { useEffect, useState, useSyncExternalStore } from 'react'
import { ApiError } from '../../lib/api'
import { bytes } from '../../lib/format'
import { haptic } from '../../lib/motion'
import * as urlfetch from '../../lib/urlfetch'
import { Notice, Sheet } from '../../components/ui'
import type { FetchJob } from './types'

/** サーバの MAX_URLS_PER_REQUEST と同じ。超えると 422 になるので入力の時点で止める */
const MAX_URLS = 20

function parseUrls(text: string): string[] {
  return [...new Set(text.split(/\s+/).filter(Boolean))]
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname
  } catch {
    return url
  }
}

function eta(sec: number | null): string {
  if (sec == null) return ''
  if (sec < 10) return 'まもなく'
  if (sec < 60) return `残り約${Math.ceil(sec / 10) * 10}秒`
  if (sec < 3600) return `残り約${Math.ceil(sec / 60)}分`
  return `残り約${Math.round(sec / 3600)}時間`
}

function useJobs(): FetchJob[] {
  return useSyncExternalStore(urlfetch.subscribe, urlfetch.getJobs, urlfetch.getJobs)
}

export function UrlFetch({ onClose }: { onClose: () => void }) {
  const jobs = useJobs()
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rejected, setRejected] = useState<{ url: string; error: string }[]>([])

  /* 開いたら一度取りに行く。走っているものがあれば、そこから先は store が回す */
  useEffect(() => {
    void urlfetch.refresh()
  }, [])

  const urls = parseUrls(text)

  async function go() {
    if (urls.length === 0 || busy) return
    if (urls.length > MAX_URLS) {
      setError(`一度に頼めるのは ${MAX_URLS} 件までです`)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const r = await urlfetch.submit(urls)
      haptic(r.rejected.length ? 'error' : 'light')
      setRejected(r.rejected)
      /* 受け付けられなかったものだけ欄に残す。直して押し直せる */
      setText(r.rejected.map((x) => x.url).join('\n'))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '保存を始められませんでした')
    } finally {
      setBusy(false)
    }
  }

  async function stop(job: FetchJob) {
    haptic('light')
    try {
      await urlfetch.cancel(job.id)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '操作できませんでした')
    }
  }

  return (
    <Sheet title="URLから保存" onClose={onClose}>
      <div className="field">
        <textarea
          className="textarea ufetch__input"
          rows={4}
          inputMode="url"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          value={text}
          placeholder={'動画のページのURLを貼り付け\nprivate/tmpのフォルダに保存\n複数は改行で区切る'}
          onChange={(e) => {
            setText(e.target.value)
            setError(null)
          }}
        />
      </div>

      {error && <Notice alert>{error}</Notice>}
      {rejected.length > 0 && (
        <Notice alert>
          {rejected.map((r) => (
            <div key={r.url} className="ufetch__rej">
              <span className="ufetch__rej-url">{r.url}</span>
              <span>{r.error}</span>
            </div>
          ))}
        </Notice>
      )}

      <button
        type="button"
        className="btn btn--full btn--primary"
        disabled={urls.length === 0 || busy}
        onClick={() => void go()}
      >
        {urls.length > 1 ? `${urls.length}件を保存` : '保存'}
      </button>

      {jobs.length > 0 && (
        <ul className="ufetch">
          {jobs.map((j) => (
            <Row key={j.id} job={j} onStop={() => void stop(j)} />
          ))}
        </ul>
      )}
    </Sheet>
  )
}

function Row({ job, onStop }: { job: FetchJob; onStop: () => void }) {
  const live = urlfetch.isActive(job)
  const name = job.title || hostOf(job.url)

  let status: string
  let sub = ''
  if (job.state === 'queued') {
    status = '待ち'
  } else if (job.state === 'downloading') {
    status = job.total_bytes > 0 ? `${Math.floor(job.percent)}%` : bytes(job.done_bytes)
    sub = [
      job.files_total > 0 ? `${job.files_done} / ${job.files_total}本` : '',
      job.total_bytes > 0 ? `${bytes(job.done_bytes)} / ${bytes(job.total_bytes)}` : '',
      job.speed > 0 ? `${bytes(job.speed)}/秒` : '',
      eta(job.eta),
    ]
      .filter(Boolean)
      .join(' · ')
  } else if (job.state === 'processing') {
    status = 'NASで処理中'
  } else if (job.state === 'done') {
    status = job.duplicate ? '保存済み' : '完了'
    sub = job.dest
  } else if (job.state === 'cancelled') {
    status = '中止'
  } else {
    status = '失敗'
    sub = job.error
  }

  const failed = job.state === 'error'

  return (
    <li className={failed ? 'ufetch__row ufetch__row--err' : 'ufetch__row'}>
      <div className="ufetch__main">
        <span className="ufetch__name">{name}</span>
        <span className="ufetch__status">{status}</span>
        <button
          type="button"
          className="ufetch__x"
          aria-label={live ? `${name} を止める` : `${name} を一覧から消す`}
          onClick={onStop}
        >
          {live ? '止める' : '消す'}
        </button>
      </div>
      {sub && <span className="ufetch__sub selectable">{sub}</span>}
      {live && (
        <div className="upload__bar">
          <div
            className={
              job.state === 'processing' || (job.state === 'downloading' && job.total_bytes === 0)
                ? 'upload__fill upload__fill--wait'
                : 'upload__fill'
            }
            style={
              job.state === 'downloading' && job.total_bytes > 0
                ? { transform: `scaleX(${Math.min(100, job.percent) / 100})` }
                : job.state === 'queued'
                  ? { transform: 'scaleX(0)' }
                  : undefined
            }
          />
        </div>
      )}
    </li>
  )
}
