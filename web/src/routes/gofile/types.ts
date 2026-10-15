/* サーバ側の応答そのまま（docs/refs/gofile-thumbnail-gallery-spec.md §8.1）。
   camelCase はサーバ (app/gofile/store.py list_display) が既にその形で返す。 */

export type GalleryItem = {
  id: number
  title: string
  thumbnailUrl: string
  gofileUrl: string
  sourcePostedAt: string | null
  fileCount: number | null
  totalSizeBytes: number | null
  /* 投稿者情報。無いアイテム（移行前の古い行等）では両方 null */
  sourceXAccountId: string | null
  profileImageUrl: string | null
  /* 人気ランキング一覧でだけ値が入る（1が先頭）。他の一覧では常に null */
  rank: number | null
}

export type GalleryListResp = {
  items: GalleryItem[]
  nextCursor: string | null
  lastSyncedAt: string | null
}

/** カードのアバターチップから投稿者別一覧を開くときに渡す最小限の情報。
    アカウントの表示名等はサーバに問い合わせ直さず、タップ元のカードが
    既に持っている値をそのまま使う（store.list_by_account 参照）。 */
export type AccountRef = {
  id: string
  avatarUrl: string | null
}

/** 保存要求(save/save-url)の応答。`duplicate` のときは保存を実行していない
    （キューにも積んでいない）。前回の保存情報だけを返す。 */
export type SaveResp = {
  ok: boolean
  queued: boolean
  duplicate?: boolean
  destPath?: string
  lastSavedAt?: string
  saveCount?: number
}
