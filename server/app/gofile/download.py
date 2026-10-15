"""長押し→保存 (§9.2の外側の機能)。gofile.io の共有コンテンツを丸ごと落とす。

yyyywaiwai/gofile-dl (https://github.com/yyyywaiwai/gofile-dl) というNode製CLIの
ダウンロード手順をこのアプリのPython/非同期実装に移植したもの。
「ゲストのWebセッションを作る→コンテンツ木を取得する→ファイルを1つずつ保存する」
という流れは向こうと同じ。ホストの許可判定・リダイレクト再検証は
`security.py` の `safe_get`/`safe_stream` にそのまま乗る
（ダウンロード先は `*.gofile.io` で、サムネイル取得と同じ許可ホストで足りる）。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import security, store

# 公式サイトが実際に送る値と完全に一致させる必要がある。
# X-Website-Token はこの User-Agent 文字列込みでハッシュ化するため、
# 実際に送信する User-Agent ヘッダをここと食い違わせるとサーバ側の
# ハッシュ照合に落ちる（gofile-dl の実装をそのまま踏襲）
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_BROWSER_LANGUAGE = "en-US"
# ★保存が急に全滅したら、まずこの salt のずれを疑う★
#
# gofile.io は `https://gofile.io/js/wt.obf.js` に埋め込んだこの salt を
# 不定期に変更する。古い salt だとハッシュが一致せず、サーバは
# （実際はトークン不一致なのに）**"error-notPremium"** を返してくる。
# 「プレミアム限定になった」という意味ではないので、エラー文言に釣られないこと
# （2026-08-16: 保存が全滅していた原因がこれだった）。
#
# 現在値は実物の wt.obf.js を実行して抽出・照合したもの
# （`scripts/gofile-wt-salt.sh` が同じ手順を自動化している）。
# ★他所のスクレイパーの値を当てにしない★ 2026-08-16 時点で
# gallery-dl が積んでいた "5d4f7g8sd45fsd" は既に古く、それを信じて
# 一度間違った修正をした。必ず上のスクリプトで実物から取り直すこと。
#
# 環境変数で上書きできるので、次のローテーション時はイメージを焼き直さずに
# `.env` に GOFILE_WT_SALT=... を足して api を再起動するだけでよい。
_WEBSITE_TOKEN_SALT = _env_salt if (_env_salt := os.environ.get("GOFILE_WT_SALT", "").strip()) else "12af056dacea0b"
_API_ROOT = "https://api.gofile.io"
_REFERER = "https://gofile.io/"

_PAGE_SIZE = 1000
_CHUNK_SIZE = 256 * 1024

# 保存の本体は同時に1本だけ（HDD のシークが競合するので。docs/gofile-gallery-api.md）。
# ギャラリーの保存キュー（routers/gofile_gallery.py）と「URLから保存」（urlfetch.py）は
# 別々のスレッドで回っているので、ここで1本に揃える
SAVE_LOCK = threading.Lock()

# 進捗の知らせ。`progress(event)` が例外を投げれば保存はそこで止まる（中止の合図）。
#   {"kind": "start"}                                  … 排他を取って始めた
#   {"kind": "plan", "title", "files", "bytes", "dir"} … 木を読み終え、保存先を作った（bytes は 0 なら不明）
#   {"kind": "bytes", "n"}                             … n バイト書いた
#   {"kind": "file"}                                   … 1ファイル終わった
Progress = Callable[[dict], None]


class DownloadError(Exception):
    """取得・保存の失敗。呼び側はジョブ失敗として扱う。

    `api_status` には gofile が返した `status`（`"error-rateLimit"` 等）が入る。
    ★セッションを捨ててよいかの判定に使う★（`_should_drop_session`）。
    """

    def __init__(self, message: str, *, api_status: str | None = None) -> None:
        super().__init__(message)
        self.api_status = api_status


# ★このエラーのときだけゲストセッションを捨てる★
#
# 「失敗したらとりあえず作り直す」はここでは**やってはいけない**。
# `POST /accounts` は数時間解除されないレート制限があり、作成回数が
# 一番の希少資源だから。無関係な失敗（レート制限・混雑・パスワード保護）で
# 有効なトークンを捨てると、次の保存が作成し直しに行って自分の首を絞める。
#
# notPremium はトークン不一致でも返る（salt ずれ）が、その場合は作り直しても
# 同じ salt で作るので直らず、作成回数だけ減る。だから**ここには入れない**。
# 入れてよいのは「そのトークンがもう通らない」と分かる合図だけ。
_SESSION_DEAD_STATUSES = frozenset({
    "error-auth",
    "error-noAuth",
    "error-notFound-account",
    "error-account-notFound",
})


def _should_drop_session(e: DownloadError) -> bool:
    return e.api_status in _SESSION_DEAD_STATUSES


def _website_token(account_token: str) -> str:
    period = str(int(time.time() // 14400))
    raw = f"{_USER_AGENT}::{_BROWSER_LANGUAGE}::{account_token}::{period}::{_WEBSITE_TOKEN_SALT}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sanitize_component(name: str) -> str:
    """ファイル/フォルダ名として安全な形にする。区切り文字・制御文字を潰す。"""
    cleaned = re.sub(r'[\/\\:*?"<>|\x00-\x1f\x7f]', "_", (name or "").strip())
    cleaned = cleaned.strip(".")
    return cleaned or "_"


def _headers(token: str | None = None) -> dict[str, str]:
    h = {"User-Agent": _USER_AGENT, "Referer": _REFERER}
    if token:
        h["Authorization"] = f"Bearer {token}"
        h["X-Website-Token"] = _website_token(token)
        h["X-BL"] = _BROWSER_LANGUAGE
    return h


async def _api_json(
    client: httpx.AsyncClient, url: str, *, token: str | None = None, method: str = "GET"
) -> dict:
    resp = await security.safe_get(
        client,
        url,
        method=method,
        allowed_host_check=security.is_allowed_thumbnail_host,
        headers=_headers(token),
    )
    try:
        body = resp.json()
    except ValueError as e:
        raise DownloadError(f"応答がJSONではありません: {url}") from e
    api_status = body.get("status")
    if resp.status_code >= 400 or api_status != "ok":
        hint = ""
        if api_status == "error-notPremium":
            # ★文言に釣られないための申し送り★ プレミアム限定になった
            # という意味ではなく、X-Website-Token のハッシュが合わない
            # ときにもこれが返る。salt のローテーションが原因のことが多い
            hint = "（salt がずれている可能性。server/scripts/gofile-wt-salt.sh で取り直す）"
        raise DownloadError(
            f"gofile APIが失敗を返しました ({api_status or resp.status_code}): {url}{hint}",
            api_status=api_status,
        )
    return body.get("data") or {}


async def _create_guest_session(client: httpx.AsyncClient) -> str:
    data = await _api_json(client, f"{_API_ROOT}/accounts", method="POST")
    token = data.get("token")
    if not token:
        raise DownloadError("ゲストセッションの作成に失敗しました")
    # website セッションの確立。失敗すればここで気付ける
    await _api_json(client, f"{_API_ROOT}/accounts/website", token=token)
    return token


# ゲストセッションの使い回し。★保存のたびに新規作成しない★
#
# 以前は save_content() を呼ぶたびに `POST /accounts` で新しいゲスト
# アカウントを作っていたが、gofile はこの口を**厳しくレート制限していて、
# 一度踏むと数時間単位で解除されない**（2026-08-15〜16 に実際に踏み、
# 半日近く保存できなくなった）。作成回数は貴重な資源として扱う。
#
# ★保存先は DB★（`store.get_guest_session`）。プロセスのメモリだと
# `docker compose up -d api` のたびに作り直しに行き、再起動1回で
# 貴重な作成回数を1回消費してしまう。
#
# トークンの実際の有効期限は公開されていないので、保守的に短めで切る。
# 期限内でも拒否されたら `_invalidate_session()` で捨てて次回作り直す。
_SESSION_MAX_AGE_S = 3600 * 6
_session_lock = threading.Lock()


async def _get_session_token(client: httpx.AsyncClient) -> str:
    with _session_lock:
        saved = store.get_guest_session()
        if saved is not None:
            token, created = saved
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if 0 <= age < _SESSION_MAX_AGE_S:
                return token
    token = await _create_guest_session(client)
    with _session_lock:
        store.save_guest_session(token)
    return token


def _invalidate_session() -> None:
    """次回の保存でゲストセッションを作り直させる。

    ★ここでは新規作成しない★ 失敗した保存のリトライ内で作り直すと、
    そのリトライ自体が再び notPremium/rateLimit で失敗したときに
    アカウント作成の試行回数が余計に増える。次の保存呼び出しに任せる。
    """
    with _session_lock:
        store.clear_guest_session()


async def _fetch_folder(client: httpx.AsyncClient, content_id: str, token: str) -> dict:
    """1フォルダぶんの中身を取得する。ページングして全件をまとめる。"""
    merged: dict | None = None
    page = 1
    while True:
        url = (
            f"{_API_ROOT}/contents/{content_id}"
            f"?contentFilter=&page={page}&pageSize={_PAGE_SIZE}"
            f"&sortField=name&sortDirection=1"
        )
        data = await _api_json(client, url, token=token)
        if data.get("password") and data.get("passwordStatus") == "passwordWrong":
            raise DownloadError("パスワード保護されたコンテンツです")
        children = data.get("children") or {}
        if merged is None:
            merged = data
        else:
            merged.setdefault("children", {}).update(children)
        if len(children) < _PAGE_SIZE:
            break
        page += 1
    return merged or {}


async def _walk(client: httpx.AsyncClient, content_id: str, token: str) -> dict:
    node = await _fetch_folder(client, content_id, token)
    children: dict = {}
    for child_id, child in (node.get("children") or {}).items():
        if child.get("type") == "folder":
            nested_id = child.get("id") or child.get("code") or child_id
            children[child_id] = await _walk(client, nested_id, token)
        else:
            children[child_id] = child
    node["children"] = children
    return node


def _flatten(node: dict, base: str) -> list[dict]:
    out: list[dict] = []
    if node.get("type") == "file":
        link = node.get("link")
        if link:
            out.append({
                "url": link,
                "rel_path": base or sanitize_component(node.get("id") or "file"),
                "size": int(node.get("size") or 0),
            })
        return out
    for child_id, child in (node.get("children") or {}).items():
        raw_name = (child.get("name") or "").strip() or child_id
        name = sanitize_component(raw_name)
        child_base = f"{base}/{name}" if base else name
        out.extend(_flatten(child, child_base))
    return out


def _unique_dir(root: Path, name: str) -> Path:
    """`root/name` を確保する。同名のフォルダが既にあれば ` (2)` のように避ける。

    ★連番フォルダは挟まない★（2026-07-31）。以前は `root` 直下に
    `1/`, `2/`, … を作り、その中にコンテンツ名のフォルダを置く2階層に
    していたが、動画を見るのに番号フォルダとコンテンツ名フォルダを
    続けて2回開く必要があり、私的鑑賞用途では単純に手間だった（本人の指摘）。
    ここでは `root` 直下に直接コンテンツ名のフォルダを作る。

    ★中身までは比べない★ 同じタイトルの投稿を2回保存しても、
    「上書きで消える」よりは「並べて残る」ほうが安全側（gofileのタイトルが
    同じでも別コンテンツのことがあるため）。実際に mkdir が通った名前を
    採用する（旧 `_next_seq_dir` が連番でやっていたのと同じ発想）。
    """
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / name
    n = 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = root / f"{name} ({n})"
            n += 1


async def _download_file(
    client: httpx.AsyncClient, url: str, dest: Path, token: str, progress: Progress | None = None
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    headers = {"User-Agent": _USER_AGENT, "Referer": _REFERER, "Cookie": f"accountToken={token}"}
    resp = await security.safe_stream(
        client, url, allowed_host_check=security.is_allowed_thumbnail_host, headers=headers
    )
    try:
        if resp.status_code >= 400:
            raise DownloadError(f"ダウンロードに失敗しました (HTTP {resp.status_code}): {url}")
        with open(tmp, "wb") as f:
            async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                f.write(chunk)
                if progress:
                    progress({"kind": "bytes", "n": len(chunk)})
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        await resp.aclose()
    tmp.rename(dest)
    if progress:
        progress({"kind": "file"})


async def save_content(gofile_url: str, dest_root: Path, progress: Progress | None = None) -> Path:
    """gofile.io の共有コンテンツを `dest_root` 直下へ、コンテンツ名のフォルダとして保存する。

    `dest_root/コンテンツのタイトル/...実際のファイル` の1階層。
    戻り値は実際に書き込んだフォルダそのもの（`_unique_dir` の申し送り参照）。
    """
    code = security.extract_gofile_code(gofile_url)
    if not code:
        raise DownloadError(f"不正なgofile URLです: {gofile_url}")

    timeout = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        token = await _get_session_token(client)
        try:
            tree = await _walk(client, code, token)
            root_name = sanitize_component(tree.get("name") or code)
            # ★フォルダ名は既に dest_dir が持つ★ 通常コンテンツはフォルダ型
            # （children を持つ）ので base="" で子の名前だけを積む。
            # 稀にコンテンツ自体が単独ファイルのときだけ、その1件の名前として
            # root_name を使う（base="" だと id 由来の無意味な名前になるため）
            base = root_name if tree.get("type") == "file" else ""
            files = _flatten(tree, base)
            if not files:
                raise DownloadError("保存できるファイルがありませんでした")

            dest_dir = _unique_dir(dest_root, root_name)
            if progress:
                sizes = [f["size"] for f in files]
                progress({
                    "kind": "plan",
                    "title": dest_dir.name,
                    "files": len(files),
                    "bytes": sum(sizes) if all(sizes) else 0,
                    "dir": dest_dir,
                })
            for f in files:
                await _download_file(client, f["url"], dest_dir / f["rel_path"], token, progress)
        except DownloadError as e:
            # ★無関係な失敗でトークンを捨てない★ レート制限や
            # パスワード保護で捨てると、次の保存が `POST /accounts` に
            # 行き、一番希少な作成回数を無駄に減らす（_SESSION_DEAD_STATUSES 参照）
            if _should_drop_session(e):
                _invalidate_session()
            raise

    return dest_dir


def private_tmp_root() -> Path:
    from ..config import settings

    return settings.files_root / settings.private_dir / "tmp"


def save_blocking(gofile_url: str, *, progress: Progress | None = None) -> Path:
    """保存の本体＋後始末＋保存履歴。スレッドから呼ぶ（中で asyncio.run する）。

    ギャラリーの長押し・URL貼り付け保存と、「URLから保存」の gofile リンクの
    **共通の出口**。排他（`SAVE_LOCK`）・棚の記憶の破棄・保存履歴の記録を
    ここに1箇所だけ置く。戻り値は書き込んだフォルダ（`private/tmp/{タイトル}`）。
    """
    from .. import privatefiles
    from ..config import settings

    with SAVE_LOCK:
        if progress:
            progress({"kind": "start"})
        dest = asyncio.run(save_content(gofile_url, private_tmp_root(), progress))
    privatefiles.forget()
    code = security.extract_gofile_code(gofile_url)
    if code:
        rel = str(Path(settings.private_dir) / "tmp" / dest.name)
        store.record_saved_download(code, gofile_url, rel)
    return dest
