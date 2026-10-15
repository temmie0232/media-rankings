/* メディアタブの「Tokyomotion」。一覧と保存の画面は SiteList.tsx と共通。
 *
 * 並び替えはサイトの動画一覧のもの（server/app/tokyomotion.py）。
 * ★期間は「その期間に追加された動画」に絞る意味★（再生数の集計期間ではない）。
 * 詳しくは docs/tokyomotion-api.md。 */
import { memo } from 'react'
import { listTokyomotion } from './api'
import { SiteList, type SiteConfig } from './SiteList'

const SITE: SiteConfig = {
  id: 'tokyomotion',
  list: listTokyomotion,
  sorts: [
    { id: 'popular', label: '人気' },
    { id: 'rated', label: '高評価' },
    { id: 'favorites', label: 'お気に入り' },
    { id: 'watching', label: '視聴中' },
    { id: 'recent', label: '新着' },
    { id: 'longest', label: '長尺' },
  ],
  periods: [
    { id: 'today', label: '今日' },
    { id: 'week', label: '今週' },
    { id: 'month', label: '今月' },
    { id: 'all', label: '全期間' },
  ],
  defaultSort: 'popular',
  defaultPeriod: 'week',
  /* 画像の置き場。`<img src>` に渡す最後の場所なので、サーバが絞っていても確かめる */
  thumbHost: 'cdn.tokyo-motion.net',
  pageHosts: ['www.tokyomotion.net'],
}

export const Tokyomotion = memo(function Tokyomotion() {
  return <SiteList site={SITE} />
})
