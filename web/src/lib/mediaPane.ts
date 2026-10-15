/* メディアタブの中の「いまどの画面か」を、React の外で持つ小さな store。
 *
 * メディアタブは「プライベート / Gofile / Tokyomotion / 85po / ライブラリ」の切替で、
 * それぞれが自前の .main（スクローラ）を持つ。App.tsx は navbar の板色や
 * 「下へ送っている間は FAB を退避」を**前面の .main** の scroll から決めているので、
 * タブの中で画面が替わったら、そちらを掴み直させる必要がある。
 * App が切替の中身（routes/MediaHub.tsx）を import せずに済むよう、ここに置く。
 *
 * 選んだ画面は端末に残す（次に開いたとき、前に見ていたところから始まる）。 */
import { createContext, useContext, useSyncExternalStore } from 'react'

export type MediaPane = 'private' | 'gofile' | 'tokyomotion' | 'po85' | 'library'

export const MEDIA_PANES: { id: MediaPane; label: string }[] = [
  { id: 'private', label: 'プライベート' },
  { id: 'gofile', label: 'Gofile' },
  { id: 'tokyomotion', label: 'Tokyomotion' },
  { id: 'po85', label: '85po' },
  { id: 'library', label: 'ライブラリ' },
]

const KEY = 'nasapp.media.pane'

function load(): MediaPane {
  try {
    const v = localStorage.getItem(KEY)
    if (MEDIA_PANES.some((p) => p.id === v)) return v as MediaPane
  } catch {
    /* プライベートブラウズ等で読めなくても既定で始める */
  }
  return 'private'
}

let current: MediaPane = load()
const listeners = new Set<() => void>()

export function getMediaPane(): MediaPane {
  return current
}

export function setMediaPane(next: MediaPane): void {
  if (next === current) return
  current = next
  try {
    localStorage.setItem(KEY, next)
  } catch {
    /* 覚えられなくても動く */
  }
  for (const fn of listeners) fn()
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function useMediaPane(): MediaPane {
  return useSyncExternalStore(subscribe, getMediaPane, getMediaPane)
}

/* ---------------------------------------------------------- 入口のロック

   メディアタブ全体はパターンロックの向こう（routes/media/MediaGate.tsx）。
   鍵が開く／閉じるとき、前面の .main が「鍵の画面のもの」と「各画面のもの」で替わるので、
   App.tsx に掴み直させる合図としてここに持つ。 */
let gateOpen = false

export function setMediaGateOpen(open: boolean): void {
  if (open === gateOpen) return
  gateOpen = open
  for (const fn of listeners) fn()
}

/** 「どの画面を、鍵が開いた状態で見ているか」。前面の .main が替わるたびに値が変わる。 */
export function useMediaViewKey(): string {
  const pane = useMediaPane()
  const open = useSyncExternalStore(subscribe, () => gateOpen, () => gateOpen)
  return `${pane}:${open ? 'open' : 'locked'}`
}

/** 自分がいま前面の画面か。MediaHub が各画面を包んで渡す。
 *
 *  ★タブの Active（components/TabPage.tsx）とは別物★ そちらは「メディアタブが前面か」。
 *  メディアタブの中では、裏に回した画面も生きたままなので、「引いて更新」の登録
 *  （タブ単位で最後に入れたものが勝つ）だけはこの札で前面の画面に限る。 */
const PaneActive = createContext(true)
export const PaneActiveProvider = PaneActive.Provider
export function usePaneActive(): boolean {
  return useContext(PaneActive)
}
