/* Gofileギャラリー API の型付き fetcher。media/api.ts と同じ流儀
   （通信そのものは lib/api.ts の `api` に任せ、ここはクエリ組み立てと型付けだけ）。 */
import { api } from '../../lib/api'
import type { GalleryListResp, SaveResp } from './types'

export function listGallery(cursor: string | null, limit = 30): Promise<GalleryListResp> {
  const q = new URLSearchParams({ limit: String(limit) })
  if (cursor) q.set('cursor', cursor)
  return api.get<GalleryListResp>(`/api/gofile-gallery?${q}`)
}

/** 人気(24h)ランキング一覧。応答の形は listGallery と同じ（`rank` に順位が入る）。 */
export function listRankings(cursor: string | null, limit = 30): Promise<GalleryListResp> {
  const q = new URLSearchParams({ limit: String(limit) })
  if (cursor) q.set('cursor', cursor)
  return api.get<GalleryListResp>(`/api/gofile-gallery/rankings?${q}`)
}

/** 投稿者別一覧。うちのDBに既にある検証済みアイテムだけを絞り込んで返す
    （gofile-douga.com への新規ライブ取得はしない）。 */
export function listByAccount(
  accountId: string,
  cursor: string | null,
  limit = 30,
): Promise<GalleryListResp> {
  const q = new URLSearchParams({ limit: String(limit) })
  if (cursor) q.set('cursor', cursor)
  return api.get<GalleryListResp>(
    `/api/gofile-gallery/by-account/${encodeURIComponent(accountId)}?${q}`,
  )
}

/** §9.3: 画像読込エラーを再検証要求に変える。失敗しても画面上は無視してよい
    （カード自体は既に非表示にした後の、あくまで裏方の後始末のため）。 */
export function reportBroken(id: number): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>(`/api/gofile-gallery/${id}/report-broken`)
}

/** §8.2 手動更新。実行中なら 409（lib/api.ts の ApiError で拾える）。 */
export function requestSync(): Promise<{ ok: boolean; job_id: string }> {
  return api.post<{ ok: boolean; job_id: string }>('/api/admin/gofile-gallery/sync')
}

/** 長押し→保存。動画本体を private/tmp 配下の新しい連番フォルダへ落とす。
    他に実行中の保存が無ければ `queued:false`（即開始）、あれば `queued:true`
    （キューで順番待ち）。過去に同じコードを保存済みなら `duplicate:true` を
    返すだけで保存はしない（`force:true` で承知の上もう一度保存する）。
    積みすぎなら429（lib/api.ts の ApiError で拾える）。 */
export function saveItem(id: number, force = false): Promise<SaveResp> {
  const q = force ? '?force=true' : ''
  return api.post<SaveResp>(`/api/gofile-gallery/${id}/save${q}`)
}

/** URL貼り付け→保存。ギャラリーのDBに載っていない任意のgofile.ioリンクも
    保存できる。形式不正なら400、積みすぎなら429（どちらもApiErrorで拾える）。
    `duplicate`/`force` の扱いは saveItem と同じ。 */
export function saveUrl(url: string, force = false): Promise<SaveResp> {
  return api.post<SaveResp>('/api/gofile-gallery/save-url', { url, force })
}
