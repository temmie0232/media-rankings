/* メディアタブの「85po」。一覧と保存の画面は SiteList.tsx と共通。
 *
 * 並び替えは人気（再生数）・高評価・新着の3つ（server/app/po85.py、docs/po85-api.md）。
 * ★期間は Tokyomotion と意味が違う★ こちらは「その期間の再生数／評価で並べる」。
 * 新着には期間が無いので、期間の帯は出さない。
 * 動画の取得は yt-dlp ではなく専用の処理（urlfetch.py の `_run_po85`）。 */
import { memo } from 'react'
import { listPo85 } from './api'
import { SiteList, type SiteConfig } from './SiteList'

const SITE: SiteConfig = {
  id: 'po85',
  list: listPo85,
  sorts: [
    { id: 'popular', label: '人気' },
    { id: 'rated', label: '高評価' },
    { id: 'recent', label: '新着' },
  ],
  periods: [
    { id: 'today', label: '今日' },
    { id: 'week', label: '今週' },
    { id: 'month', label: '今月' },
    { id: 'all', label: '全期間' },
  ],
  usesPeriod: (sort) => sort !== 'recent',
  defaultSort: 'popular',
  defaultPeriod: 'week',
  /* サムネは動画ページと同じホストの /contents/ の下 */
  thumbHost: 'www.85po.net',
  thumbPath: '/contents/',
  pageHosts: ['www.85po.net', '85po.net'],
}

export const Po85 = memo(function Po85() {
  return <SiteList site={SITE} />
})
