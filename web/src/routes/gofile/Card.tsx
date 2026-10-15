/* 1件ぶんのカード。仕様書 §9.2。
   タップでは動画を落とさず、公式Gofileページへ新しいタブで遷移する。
   長押しだけ例外 — 「保存」ボタンを出し、押すと private/tmp へ動画本体を落とす
   （routes/gofile/api.ts の saveItem、実体は app/gofile/download.py）。 */
import { memo, useState } from 'react'
import { ApiError } from '../../lib/api'
import { bytes, shortDate } from '../../lib/format'
import { haptic } from '../../lib/motion'
import { toast } from '../../lib/toast'
import { Icon } from '../../components/ui'
import { saveItem } from './api'
import { isSafeGofileUrl, isSafeThumbnailUrl, isSafeAvatarUrl, notifySaveResult } from './lib'
import type { AccountRef, GalleryItem } from './types'

function CardInner({
  item,
  onBroken,
  onOpenAccount,
}: {
  item: GalleryItem
  onBroken: (id: number) => void
  onOpenAccount: (a: AccountRef) => void
}) {
  const [failed, setFailed] = useState(false)
  const [showSave, setShowSave] = useState(false)

  // §9.3: DOMへ設定する前にホストとHTTPSを検証する。サーバは既に絞っているが、
  // <img src> / <a href> に値を渡す最後の場所としてもう一段確認する
  if (failed || !isSafeThumbnailUrl(item.thumbnailUrl) || !isSafeGofileUrl(item.gofileUrl)) {
    return null
  }

  const meta = [
    item.sourcePostedAt ? shortDate(item.sourcePostedAt) : '',
    item.fileCount != null ? `${item.fileCount}件` : '',
    item.totalSizeBytes != null ? bytes(item.totalSizeBytes) : '',
  ].filter(Boolean)

  // 投稿者アバター。無いアイテム（移行前の古い行等）では出さない
  const accountId = item.sourceXAccountId
  const avatarUrl =
    item.profileImageUrl && isSafeAvatarUrl(item.profileImageUrl) ? item.profileImageUrl : null

  async function doSave(force = false) {
    setShowSave(false)
    try {
      const r = await saveItem(item.id, force)
      notifySaveResult(r, () => void doSave(true))
    } catch (e) {
      haptic('error')
      toast(e instanceof ApiError ? e.message : '保存を開始できませんでした', { alert: true })
    }
  }

  return (
    <div className="gofile__card">
      <a
        className="gofile__cardlink"
        href={item.gofileUrl}
        target="_blank"
        rel="noopener noreferrer"
        onContextMenu={(ev) => {
          // ★ネイティブメニューを止めて「保存」に差し替える★
          // App.tsx が既定のコンテキストメニューを全体で止めているので競合しない
          ev.preventDefault()
          haptic('medium')
          setShowSave(true)
        }}
      >
        <span className="gofile__thumbwrap">
          <img
            className="gofile__thumb"
            src={item.thumbnailUrl}
            alt=""
            loading="lazy"
            decoding="async"
            onError={() => {
              // §9.3: 読み込みエラーは即時非表示にし、裏でバックエンドへ再検証を頼む
              setFailed(true)
              onBroken(item.id)
            }}
          />
          <span className="gofile__ext" aria-hidden="true">
            <Icon name="external" />
          </span>
        </span>
        <span className="gofile__title">{item.title}</span>
        {meta.length > 0 && (
          <span className="gofile__meta">
            {meta.map((m, i) => (
              <span key={i}>{m}</span>
            ))}
          </span>
        )}
      </a>
      {accountId && avatarUrl && (
        // <a>の中に<button>を置けないので（HTML的に不正・タップ判定も怪しくなる）
        // .gofile__savescrim と同じ考え方で <a> の外、.gofile__card 直下に
        // 絶対配置で重ねる。当たり判定の敷地(zone)自体は pointer-events:none
        // にして、アバター本体だけ有効にする（それ以外はカード本体のタップに通す）
        // ★aria-hidden を付けない★ 中の <button> はフォーカスできるので、
        // aria-hidden の配下に置くと ARIA 違反（focusable な子を隠している）に
        // なり、TalkBack / VoiceOver から「投稿者の他の投稿を見る」が丸ごと
        // 消える。素通しにしたいだけなら pointer-events で足りていて、それは
        // すでに gofile.css の .gofile__avatarzone / .gofile__avatar が
        // none / auto でやっている（読み上げの木から外す必要は無い）
        <span className="gofile__avatarzone">
          <button
            type="button"
            className="gofile__avatar"
            aria-label="投稿者の他の投稿を見る"
            onClick={(ev) => {
              ev.preventDefault()
              ev.stopPropagation()
              haptic('light')
              onOpenAccount({ id: accountId, avatarUrl })
            }}
          >
            <img src={avatarUrl} alt="" loading="lazy" decoding="async" />
          </button>
        </span>
      )}
      {showSave && (
        // サムネイルと同じ範囲を覆う暗幕。ボタンの外をタップすれば閉じる
        <div className="gofile__savescrim" onClick={() => setShowSave(false)}>
          <button
            type="button"
            className="gofile__savebtn"
            onClick={(ev) => {
              ev.stopPropagation()
              void doSave()
            }}
          >
            <Icon name="download" />
            保存
          </button>
        </div>
      )}
    </div>
  )
}

/* ★memo で包むこと★ フィードは上限なく積む（30件ずつ無限スクロール）。
   親（GofileGallery）は URL 貼り付け欄の入力を state で持っているので、
   1打鍵ごとに全カードが再描画される。props（onBroken / onOpenAccount）は
   既に identity が安定しているので、包むだけで素通りする */
export const Card = memo(CardInner)
