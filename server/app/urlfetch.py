"""貼った URL の動画を取ってきて、`private/tmp/{タイトル}/` に入れる（yt-dlp）。

## 何ができるか

yt-dlp が対応する約1750サイト＋「ページに <video> が埋まっているだけのサイト」
（generic extractor）。**TOKYOMOTION は専用の extractor が無く、generic で取れる**
（2026-10-06 実測: yt-dlp 2026.08.19 で HD/SD の mp4 が見つかる）。

## 画質の選び方（実測の落とし穴）

generic が TOKYOMOTION に付ける format_id は `HD` と `SD` だけで、高さが分からない。
yt-dlp の既定（`bv*+ba/b`）は**このとき SD を選ぶ**（HD が先に並んで「劣る」扱いになる）。
そのため `HD` を最優先に書く。他のサイトで `HD` という id が無ければ読み捨てられて
`bv*+ba/b`（その場の最高画質）に落ちる。

## 流れ

    submit(url) → キュー → ワーカー1本（Pi 4 なので同時に1本）
      1. extract_info(download=False)  … 動画1本か・ライブでないか・題名を確かめる
      2. extract_info(download=True)   … TMP_ROOT/dl_<id>/ に落とす（HDD。SD に書かない）
      3. _save_into_folder(path, title) … private/tmp/{題名}/{題名}.mp4 へ move（同じ HDD なので rename）

ジョブはプロセス内にだけ持つ（workers=1 前提。routers/media.py の _pregen_state と同じ理屈）。
再起動で消えるが、tmp の dl_* は main.py が起動時に掃除する。

## 守ること

- **URL は検証してから渡す**（check_url）。ループバック・プライベート・tailnet
  （CGNAT 100.64.0.0/10）・リンクローカルへ向いたホストは弾く。NAS 自身や
  同じ LAN の機器を、ログイン済みの端末経由で叩かせないため。
  yt-dlp はページ内のリンクを辿るので**完全な塞ぎではない**（認証済みの本人だけが
  使う口である前提）。
- 再生リスト・チャンネル・ライブは受けない。1本ずつ確実に入れる。

## 置き場

どちらの道も `private/tmp/{タイトル}/` の**フォルダの中**に入る（ファイル画面から開く）。
写真・動画タブには出ない。同じ題名が既にあれば `題名 (2)` のように避ける（上書きしない）。
同じ URL をもう一度貼ると、重複判定はせず別のフォルダに落とし直す。

## 85po のリンクだけは別の道（`_run_po85`）

`https://www.85po.net/ja/video/{id}/{slug}/` は yt-dlp に渡さない。yt-dlp の generic は 85po を
KVS と判定して `Unable to extract flashvars` で失敗する（埋め込み URL も同じ）。
`po85.resolve_video` が動画ページから画質ごとの URL を割り出し（最高画質を選ぶ。同じ解像度なら
AV1 より H.264 系）、httpx でストリーム保存する。流れと置き場は yt-dlp の道と同じ
（`dl_*` に落として `_save_into_folder`）。

- 動画ファイルの URL は 302 で別ホストの CDN へ飛ぶ。**飛び先も毎回 `check_url` を通す**
  （https のみ）。Cookie も Referer も要らなかったが、Referer はサイトの origin を付けている
- 途中で切れたら、取れたところから Range で再開する（連続で進まない失敗が5回続いたら諦める）

## gofile.io のリンクだけは別の道（`_run_gofile`）

`https://gofile.io/d/{code}` は yt-dlp に渡さず、Gofile ギャラリーの保存と**同じ処理**
（app/gofile/download.py の `save_blocking`）に流す。ギャラリーの長押し保存・URL貼り付け保存と
同じ置き場・同じ保存履歴。理由は、gofile は動画以外も含む「フォルダ」を共有する形で、
yt-dlp も gofile.io を対応外にしているため。

- 排他: ギャラリーの保存キューと同じ `SAVE_LOCK` を取る。両方が同時に走って
  HDD を取り合うことはない。待っている間は「待ち」のまま。
- 保存済み（`gofile_saved_downloads` にあり、フォルダも残っている）なら落とし直さず、
  「保存済み」として終わる。フォルダを消してあれば、もう一度落とす。
- 止めた・失敗したときは、このジョブが作りかけたフォルダを片付ける
  （`_unique_dir` が必ず新しく作るので、他のものは巻き込まない）。
"""
from __future__ import annotations

import ipaddress
import logging
import queue
import re
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import po85
from .config import settings

log = logging.getLogger("nasapp.urlfetch")

# 一度に積める本数と、覚えておく終了済みジョブの数
MAX_URLS_PER_REQUEST = 20
KEEP_FINISHED = 30
# 空きがこれを切ったら始めない（落とした先で no space になるより先に断る）
MIN_FREE_BYTES = 1024 * 1024 * 1024

# HD を最優先（上の「画質の選び方」）。mp4/m4a を好むのは端末で再生できるため
FORMAT = "HD/bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"

ACTIVE = ("queued", "downloading", "processing")

class UrlRejected(ValueError):
    """URL がそもそも受けられない。メッセージはそのまま利用者に見せる。"""


class Cancelled(Exception):
    pass


_GOFILE = re.compile(r"^https?://(?:www\.)?gofile\.io/d/([A-Za-z0-9]+)(?:[/?#]|$)", re.I)


def gofile_code(url: str) -> str | None:
    """gofile のフォルダ URL ならそのコード。`http`・`www`・末尾の `/` や `?` は許す。"""
    m = _GOFILE.match((url or "").strip())
    return m.group(1) if m else None


@dataclass
class Job:
    id: int
    url: str
    state: str = "queued"  # queued | downloading | processing | done | error | cancelled
    title: str = ""
    done_bytes: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    speed: float = 0.0
    eta: int | None = None
    error: str = ""
    duplicate: bool = False  # gofile で保存済みだったとき
    # gofile のフォルダのときだけ使う（動画1本の URL では 0）
    files_total: int = 0
    files_done: int = 0
    dest: str = ""  # 保存したフォルダ（files_root からの相対。例 private/tmp/タイトル）
    created: float = field(default_factory=time.time)
    finished: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "state": self.state,
            "title": self.title,
            "percent": round(self.percent, 1),
            "done_bytes": self.done_bytes,
            "total_bytes": self.total_bytes,
            "speed": int(self.speed),
            "eta": self.eta,
            "error": self.error,
            "duplicate": self.duplicate,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "dest": self.dest,
            "created": self.created,
            "finished": self.finished,
        }


# --------------------------------------------------------------------
# URL の検証
# --------------------------------------------------------------------
def _public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_private だけでは tailnet の 100.64.0.0/10 が通ってしまう。is_global で見る
    return addr.is_global


def check_url(raw: str) -> str:
    """http(s) で、名前解決の先が全部グローバルなら URL を返す。そうでなければ UrlRejected。"""
    url = (raw or "").strip()
    if not url:
        raise UrlRejected("URL が空です")
    if len(url) > 2000:
        raise UrlRejected("URL が長すぎます")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise UrlRejected("URL の形が正しくありません") from None
    if parts.scheme not in ("http", "https") or not host:
        raise UrlRejected("http:// か https:// で始まる URL を貼ってください")
    if parts.username or parts.password:
        raise UrlRejected("ユーザー名やパスワード入りの URL は使えません")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise UrlRejected("このアドレスは見つかりませんでした") from None
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise UrlRejected("このアドレスは使えません") from None
        if not _public(addr):
            raise UrlRejected("NAS や自宅ネットワークのアドレスは取得できません")
    return url


# --------------------------------------------------------------------
# yt-dlp のエラーを短い日本語に
# --------------------------------------------------------------------
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def explain(msg: str) -> str:
    m = _ANSI.sub("", msg or "").strip()
    m = re.sub(r"^(ERROR:\s*)+", "", m)
    low = m.lower()
    if "unsupported url" in low:
        return "このURLからは動画が見つかりませんでした"
    if "http error 403" in low:
        return "サイトに拒否されました（403）。ログインや地域制限の動画は取れません"
    if "http error 404" in low:
        return "ページが見つかりません（404）。削除された動画かもしれません"
    if "private video" in low or "sign in" in low or "login" in low:
        return "ログインが必要な動画は取れません"
    if "drm" in low:
        return "DRM で保護された動画は取れません"
    if "ffmpeg" in low:
        return "動画の結合に失敗しました"
    if "no space" in low:
        return "NAS の空きが足りません"
    return (m[:160] + "…") if len(m) > 160 else (m or "取得に失敗しました")


def _save_into_folder(path: Path, title: str) -> Path:
    """落とし終えた動画を `private/tmp/{題名}/{題名}.拡張子` に移す。戻り値はフォルダ。

    フォルダ名はギャラリーの保存と同じ作り方（`sanitize_component`・`_unique_dir`）。
    名前の長さは**バイト**で切る（日本語は1文字3バイトで、ext4 の名前上限 255 を超えうる）。
    tmp と files_root は同じ HDD なので move は rename で済む（コピーは走らない）。
    """
    from . import privatefiles
    from .gofile import download

    stem = download.sanitize_component(title)
    while len(stem.encode()) > 200:
        stem = stem[:-1]
    stem = stem.rstrip(" .") or "_"
    folder = download._unique_dir(download.private_tmp_root(), stem)
    try:
        shutil.move(str(path), str(folder / f"{stem}{path.suffix.lower()}"))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)  # 作りかけのフォルダを残さない
        raise
    privatefiles.forget()
    return folder


# --------------------------------------------------------------------
# httpx でのストリーム保存（85po の道。yt-dlp を通さない）
# --------------------------------------------------------------------
_REDIRECTS = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 5
# 1バイトも進まない失敗が続いたら諦める回数と、再開までの待ち（秒。倍々で最大10倍）
_STALL_LIMIT = 5
_BACKOFF = 1.0


class _Retry(Exception):
    """途中で切れた・サーバが一時的に失敗した。取れたところから再開できる。"""


def _http_error(code: int) -> RuntimeError:
    if code in (403, 404):
        return RuntimeError(f"HTTP Error {code}")  # explain() が短い日本語にする
    return RuntimeError(f"サイトから動画を取得できませんでした（HTTP {code}）")


def _open_stream(client: httpx.Client, url: str, headers: dict[str, str]) -> httpx.Response:
    """リダイレクトを手で追って、最後の応答を（本文を読まずに）返す。閉じるのは呼び出し側。

    **飛び先ごとに `check_url`**（グローバルアドレスだけ）。https 以外へは飛ばない。
    動画ファイルの URL は別ホストの CDN へ 302 で飛ぶので、httpx 任せの追従は使わない。
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        check_url(current)
        if urlsplit(current).scheme != "https":
            raise RuntimeError("https 以外の場所へは転送できません")
        resp = client.send(client.build_request("GET", current, headers=headers), stream=True)
        if resp.status_code not in _REDIRECTS:
            return resp
        loc = resp.headers.get("location")
        resp.close()
        if not loc:
            raise RuntimeError("サイトが転送先を示しませんでした")
        current = urljoin(current, loc)
    raise RuntimeError("サイトの転送が多すぎます")


def _content_range(value: str | None) -> tuple[int, int] | None:
    """`bytes 100-999/1000` の (開始, 全体の大きさ)。全体が `*` のときは大きさ 0。"""
    m = re.match(r"^\s*bytes\s+(\d+)-\d+/(\d+|\*)\s*$", value or "", re.I)
    if not m:
        return None
    return int(m.group(1)), (0 if m.group(2) == "*" else int(m.group(2)))


def _stream_file(
    client: httpx.Client, url: str, dest: Path, job: Job, *, max_bytes: int, referer: str
) -> int:
    """`url` を `dest` に落とす。書いたバイト数を返す。

    `job` の `done_bytes / total_bytes / percent / speed / eta` を更新し、`job.cancel` が立てば
    `Cancelled`。大きさが分かれば、空き容量（`MIN_FREE_BYTES` を残す）と `max_bytes` を先に確かめる。
    切れたら取れたところから Range で再開する。サーバが Range を無視したら最初からやり直す。
    """
    done = 0
    total = 0
    stalls = 0
    speed = 0.0
    last_t = time.monotonic()
    last_done = 0
    dest.write_bytes(b"")

    while True:
        if job.cancel.is_set():
            raise Cancelled()
        headers = {"Referer": referer, "Accept": "*/*", "Accept-Encoding": "identity"}
        if done:
            headers["Range"] = f"bytes={done}-"
        before = done
        try:
            resp = _open_stream(client, url, headers)
            try:
                code = resp.status_code
                if code == 416 and total and done >= total:
                    break  # 再開しようとしたら、もう全部落ちていた
                if code >= 500 or code == 429:
                    raise _Retry(f"HTTP {code}")
                if code not in (200, 206):
                    raise _http_error(code)
                if "text/html" in resp.headers.get("content-type", "").lower():
                    raise RuntimeError("動画ではないページが返りました（アクセス制限かもしれません）")

                if code == 206:
                    rng = _content_range(resp.headers.get("content-range"))
                    if rng is None or rng[0] != done:
                        raise _Retry("再開位置がずれました")
                    total = rng[1] or total
                else:
                    if done:  # Range を無視された。最初から
                        done = last_done = 0
                        dest.write_bytes(b"")
                    total = int(resp.headers.get("content-length") or 0)
                if total:
                    if total > max_bytes:
                        raise RuntimeError(
                            f"ファイルが大きすぎます（{total // 1024 // 1024} MB。上限 {max_bytes // 1024 // 1024} MB）"
                        )
                    if shutil.disk_usage(dest.parent).free - (total - done) < MIN_FREE_BYTES:
                        raise RuntimeError("no space left")
                    job.total_bytes = total

                with dest.open("ab") as f:
                    for chunk in resp.iter_raw():
                        if job.cancel.is_set():
                            raise Cancelled()
                        f.write(chunk)
                        done += len(chunk)
                        if done > max_bytes:
                            raise RuntimeError("ファイルが大きすぎます")
                        job.done_bytes = done
                        if total:
                            job.percent = min(99.0, done * 100 / total)
                        now = time.monotonic()
                        if now - last_t >= 0.5:
                            inst = (done - last_done) / (now - last_t)
                            speed = inst if not speed else speed * 0.7 + inst * 0.3
                            last_t, last_done = now, done
                            job.speed = speed
                            job.eta = int((total - done) / speed) if total and speed > 0 else None
                if total and done < total:
                    raise _Retry("途中で切れました")
            finally:
                resp.close()
        except (httpx.RequestError, _Retry) as e:
            stalls = 0 if done > before else stalls + 1
            if stalls >= _STALL_LIMIT:
                raise RuntimeError("ダウンロードが途中で切れました。時間をおいて試してください") from e
            log.info("85po: 取得が途切れたので再開します (%s)", e)
            job.cancel.wait(min(_BACKOFF * 2 ** (stalls - 1), _BACKOFF * 10))  # 中止はすぐ効く
            continue
        break

    if done == 0:
        raise RuntimeError("動画を取得できませんでした（空の応答）")
    if total and done != total:
        raise RuntimeError("ダウンロードが途中で切れました")
    return done


class _QuietLogger:
    """yt-dlp の出力はログに残さない（URL に認証情報が混ざることがある）。警告だけ拾う。"""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)


# --------------------------------------------------------------------
# ジョブ管理
# --------------------------------------------------------------------
class Manager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._next = 1
        self._q: queue.Queue[int] = queue.Queue()
        self._worker: threading.Thread | None = None

    # --- 外から ---
    def submit(self, raw: str) -> Job:
        url = check_url(raw)
        code = gofile_code(url)
        if code:
            url = f"https://gofile.io/d/{code}"  # 同じフォルダを別の書き方で積み直さない
        with self._lock:
            for j in self._jobs.values():
                if j.url == url and j.state in ACTIVE:
                    return j  # 同じ URL を積み直さない
            job = Job(id=self._next, url=url)
            self._next += 1
            self._jobs[job.id] = job
            self._prune()
            self._q.put(job.id)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True, name="urlfetch")
                self._worker.start()
        return job

    def list(self) -> list[dict]:
        with self._lock:
            return [j.as_dict() for j in sorted(self._jobs.values(), key=lambda j: -j.id)]

    def cancel(self, job_id: int) -> bool:
        """動いている・待っているものは中止、終わったものは一覧から消す。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.state in ACTIVE:
                job.cancel.set()
                if job.state == "queued":
                    job.state = "cancelled"
                    job.finished = time.time()
            else:
                del self._jobs[job_id]
        return True

    def _prune(self) -> None:
        done = sorted((j for j in self._jobs.values() if j.state not in ACTIVE), key=lambda j: j.id)
        for j in done[: max(0, len(done) - KEEP_FINISHED)]:
            del self._jobs[j.id]

    # --- ワーカー ---
    def _loop(self) -> None:
        while True:
            try:
                job_id = self._q.get(timeout=60)
            except queue.Empty:
                return  # 暇になったら畳む。次の submit で作り直される
            with self._lock:
                job = self._jobs.get(job_id)
            if job is None or job.state != "queued":
                continue  # 待っている間に中止・削除された
            try:
                self._run(job)
            except Cancelled:
                self._end(job, "cancelled")
            except Exception as e:
                log.warning("URL 取得に失敗 job=%s: %s", job.id, e)
                job.error = explain(str(e))
                self._end(job, "error")
            finally:
                self._q.task_done()

    def _end(self, job: Job, state: str) -> None:
        job.state = state
        job.finished = time.time()
        job.speed = 0.0
        job.eta = None

    def _run(self, job: Job) -> None:
        if po85.is_site_url(job.url):
            self._run_po85(job)
            return
        code = gofile_code(job.url)
        if code:
            self._run_gofile(job, code)
            return

        import yt_dlp  # 重いので初回まで読まない
        from yt_dlp.utils import DownloadCancelled

        settings.tmp_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(settings.tmp_root).free < MIN_FREE_BYTES:
            raise RuntimeError("no space left")

        workdir = settings.tmp_root / f"dl_{job.id}_{int(time.time())}"
        workdir.mkdir(parents=True)

        base = {"done": 0}  # 映像と音声を別々に落とすとき、終わった側のぶん

        def hook(d: dict) -> None:
            if job.cancel.is_set():
                raise DownloadCancelled()
            if d.get("status") == "finished":
                base["done"] += int(d.get("total_bytes") or d.get("downloaded_bytes") or 0)
                return
            if d.get("status") != "downloading":
                return
            cur = int(d.get("downloaded_bytes") or 0)
            job.done_bytes = base["done"] + cur
            part = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if job.total_bytes:
                job.percent = min(99.0, job.done_bytes * 100 / job.total_bytes)
            elif part:
                job.percent = min(99.0, cur * 100 / part)
            job.speed = float(d.get("speed") or 0)
            job.eta = int(d["eta"]) if d.get("eta") is not None else None

        opts = {
            "format": FORMAT,
            "merge_output_format": "mp4",
            "outtmpl": str(workdir / "dl.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _QuietLogger(),
            "progress_hooks": [hook],
            "max_filesize": settings.max_upload_bytes,
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            # コンテナの $HOME に書かせない／設定ファイルを読ませない
            "cachedir": False,
            "ignoreconfig": True,
            "no_color": True,
        }
        try:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(job.url, download=False)
                    if not info:
                        raise RuntimeError("動画の情報を取れませんでした")
                    if info.get("_type") in ("playlist", "multi_video") or info.get("entries"):
                        raise RuntimeError("再生リストは取れません。動画1本のURLを貼ってください")
                    if info.get("is_live"):
                        raise RuntimeError("ライブ配信は取れません")
                    job.title = str(info.get("title") or "video")[:200]
                    sizes = [
                        f.get("filesize") or f.get("filesize_approx") or 0
                        for f in (info.get("requested_formats") or [info])
                    ]
                    if all(sizes):
                        job.total_bytes = int(sum(sizes))

                    job.state = "downloading"
                    info = ydl.extract_info(job.url, download=True)
                    path = Path((info.get("requested_downloads") or [{}])[0].get("filepath") or "")
            except DownloadCancelled:
                raise Cancelled() from None

            if job.cancel.is_set():
                raise Cancelled()
            if not path.is_file():
                raise RuntimeError("保存した動画が見つかりません")
            job.state = "processing"
            job.percent = 99.0
            folder = _save_into_folder(path, job.title)
            job.dest = str(Path(settings.private_dir) / "tmp" / folder.name)
            job.percent = 100.0
            self._end(job, "done")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _run_po85(self, job: Job) -> None:
        """85po の動画ページを、yt-dlp を通さずに `private/tmp/{題名}/{題名}.mp4` へ。"""
        settings.tmp_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(settings.tmp_root).free < MIN_FREE_BYTES:
            raise RuntimeError("no space left")

        workdir = settings.tmp_root / f"dl_{job.id}_{int(time.time())}"
        workdir.mkdir(parents=True)
        try:
            # 動画ページの取得とファイルの取得で同じクライアント（Cookie も共有）を使う
            with po85.new_client() as client:
                info = po85.resolve_video(job.url, client=client)
                if job.cancel.is_set():
                    raise Cancelled()
                job.title = (info.get("title") or f"85po {po85.video_id(job.url) or ''}".strip())[:200]
                fmt = po85.pick_format(info["formats"])
                ext = re.sub(r"[^a-z0-9]", "", str(fmt.get("ext") or "").lower()) or "mp4"
                path = workdir / f"dl.{ext}"

                job.state = "downloading"
                _stream_file(
                    client, fmt["url"], path, job,
                    max_bytes=settings.max_upload_bytes, referer=po85.SITE_URL + "/",
                )

            if job.cancel.is_set():
                raise Cancelled()
            job.state = "processing"
            job.percent = 99.0
            folder = _save_into_folder(path, job.title)
            job.dest = str(Path(settings.private_dir) / "tmp" / folder.name)
            job.percent = 100.0
            self._end(job, "done")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _run_gofile(self, job: Job, code: str) -> None:
        """gofile のフォルダを、ギャラリーの保存と同じ処理で `private/tmp/{タイトル}/` へ。"""
        from . import privatefiles
        from .gofile import download, store

        prev = store.get_saved_download(code)
        if prev and (settings.files_root / prev["dest_path"]).exists():
            job.title = Path(prev["dest_path"]).name
            job.dest = prev["dest_path"]
            job.duplicate = True
            job.percent = 100.0
            self._end(job, "done")
            return

        made: dict[str, Path] = {}
        stat = {"t0": 0.0, "got": 0}

        def progress(ev: dict) -> None:
            if job.cancel.is_set():
                raise Cancelled()
            kind = ev["kind"]
            if kind == "start":
                job.state = "downloading"
            elif kind == "plan":
                made["dir"] = ev["dir"]
                job.title = str(ev["title"])[:200]
                job.files_total = int(ev["files"])
                job.total_bytes = int(ev["bytes"])
                stat["t0"] = time.time()
                if shutil.disk_usage(ev["dir"]).free - job.total_bytes < MIN_FREE_BYTES:
                    raise RuntimeError("no space left")
            elif kind == "bytes":
                stat["got"] += ev["n"]
                job.done_bytes += ev["n"]
                job.speed = stat["got"] / max(0.5, time.time() - stat["t0"])
                if job.total_bytes:
                    job.percent = min(99.0, job.done_bytes * 100 / job.total_bytes)
                    left = max(0, job.total_bytes - job.done_bytes)
                    job.eta = int(left / job.speed) if job.speed > 0 else None
            elif kind == "file":
                job.files_done += 1
                if not job.total_bytes and job.files_total:
                    job.percent = min(99.0, job.files_done * 100 / job.files_total)

        try:
            dest = download.save_blocking(job.url, progress=progress)
        except BaseException:
            if "dir" in made:
                shutil.rmtree(made["dir"], ignore_errors=True)  # 作りかけを残さない
                privatefiles.forget()
            raise
        job.title = dest.name[:200]
        job.dest = str(Path(settings.private_dir) / "tmp" / dest.name)
        job.percent = 100.0
        self._end(job, "done")


manager = Manager()
