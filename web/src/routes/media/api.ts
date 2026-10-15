/* nas-app の routes/media/api.ts から、ランキング一覧と「URLから保存」に要る分だけを抜いたもの。
 * （元は写真ライブラリ・アルバム・zip 等の fetcher も同居している。そちらは持ってこない）
 *
 * 通信そのものは lib/api.ts の `api`（Bearer 付与・401 処理・detail の日本語化）に任せる。
 */
import { api } from '../../lib/api'
import type { FetchJob, FetchStartResp, SiteListResp, SiteQuery } from './types'

/* ---------------------------------------------------------- URL から保存 */

/** 貼られた URL を取りに行かせる。1件ずつ順番に落とすので、返ってくるのは「受け付けた」だけ */
export function startFetch(urls: string[]): Promise<FetchStartResp> {
  return api.post<FetchStartResp>('/api/media/fetch', { urls })
}

export function listFetch(): Promise<{ jobs: FetchJob[] }> {
  return api.get<{ jobs: FetchJob[] }>('/api/media/fetch')
}

export function cancelFetch(id: number): Promise<{ ok: boolean }> {
  return api.del<{ ok: boolean }>(`/api/media/fetch/${id}`)
}

/** 動画サイトの一覧（並び替え×期間×ページ）。サーバが10分だけ覚えている。
 *  取り直しても同じものが返るのは普通（`fetchedAt` で分かる）。 */
const siteList = (path: string) => (p: SiteQuery) => {
  const q = new URLSearchParams({ sort: p.sort, period: p.period, page: String(p.page) })
  return api.get<SiteListResp>(`${path}?${q}`)
}

export const listTokyomotion = siteList('/api/tokyomotion/ranking')
export const listPo85 = siteList('/api/po85/ranking')
