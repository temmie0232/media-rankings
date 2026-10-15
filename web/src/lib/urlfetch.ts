/* URL から保存の状態を **React の外** に置く store。
 *
 * uploads.ts と同じ理由（タブを移っても進捗と完了が消えない）に加えて、
 * ここは進捗の出どころがサーバなので、走っている間だけ自分で取りに行く。
 * 走っているものが無ければ何もしない（端末の電池と Pi の往復を使わない）。
 *
 * ★完了の通知は「走っているのを見たもの」だけ★ 画面を開いたら昨日終わった
 * ジョブが並んでいた、で毎回トーストを出さないため。 */
import { ApiError } from './api'
import { cancelFetch, listFetch, startFetch } from '../routes/media/api'
import type { FetchJob, FetchState } from '../routes/media/types'

const ACTIVE: FetchState[] = ['queued', 'downloading', 'processing']
export const isActive = (j: FetchJob): boolean => ACTIVE.includes(j.state)

const POLL_MS = 1500

type Listener = () => void
type DoneListener = (job: FetchJob) => void

let jobs: FetchJob[] = []
let active: FetchJob[] = []
let timer: number | null = null
let inflight = false
const listeners = new Set<Listener>()
const doneListeners = new Set<DoneListener>()
/** 走っているのを見た id。ここから終わりに変わった瞬間だけ通知する */
const watching = new Set<number>()

function emit() {
  active = jobs.filter(isActive)
  for (const fn of listeners) fn()
}

export function subscribe(fn: Listener): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function getJobs(): FetchJob[] {
  return jobs
}

/** 走っているものだけ。**参照は emit のたびにしか変わらない**（useSyncExternalStore 用） */
export function activeJobs(): FetchJob[] {
  return active
}

export function onFinished(fn: DoneListener): () => void {
  doneListeners.add(fn)
  return () => doneListeners.delete(fn)
}

function apply(next: FetchJob[]) {
  for (const j of next) {
    if (isActive(j)) {
      watching.add(j.id)
    } else if (watching.delete(j.id) && j.state !== 'cancelled') {
      for (const fn of doneListeners) fn(j)
    }
  }
  jobs = next
  emit()
}

function schedule() {
  if (timer !== null || active.length === 0) return
  timer = window.setTimeout(() => {
    timer = null
    void refresh()
  }, POLL_MS)
}

/** 取り直す。走っているものがあれば次の回も自分で予約する。失敗しても黙って続ける */
export async function refresh(): Promise<void> {
  if (inflight) return
  inflight = true
  try {
    apply((await listFetch()).jobs)
  } catch (e) {
    // 401 は api が画面を戻す。通信断は次の回でまた試す
    if (e instanceof ApiError && e.status === 401) return
  } finally {
    inflight = false
  }
  schedule()
}

export async function submit(urls: string[]) {
  const r = await startFetch(urls)
  // 返ってきた jobs は「受け付けた時点」の写し。一覧に混ぜて、あとは取り直しに任せる
  const ids = new Set(r.jobs.map((j) => j.id))
  apply([...r.jobs, ...jobs.filter((j) => !ids.has(j.id))])
  schedule()
  return r
}

export async function cancel(id: number): Promise<void> {
  await cancelFetch(id)
  await refresh()
}
