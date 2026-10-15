"""ホスト許可リストとSSRF対策 (§11 セキュリティ)。

★許可ホストはここに固定する★ config.py の GofileSettings に持たせると
「環境変数で緩められる」経路ができてしまう。取得可能なホストを固定するのが
§11 の要求そのものなので、定数として書く。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx

# 新着取得先。fetch_base_url の設定値がこれと違うホストを指していても、
# 実際のリクエストはここでしか許可しない
FETCH_ALLOWED_HOSTS = frozenset({"gofile-douga.com"})

# 遷移先ページのホスト
GOFILE_HOST = "gofile.io"
# サムネイルが置かれる `*.gofile.io`
GOFILE_THUMB_SUFFIX = ".gofile.io"

_CODE_RE = re.compile(r"^[A-Za-z0-9]+$")
# `https://gofile.io/d/{英数字}` だけを許可する (§11)
_GOFILE_URL_RE = re.compile(r"^https://gofile\.io/d/([A-Za-z0-9]+)/?$")

MAX_REDIRECTS = 3


class SecurityError(Exception):
    """許可されていないホスト・URL・リダイレクト先。呼び側は失敗として扱う。"""


def extract_gofile_code(url: str) -> str | None:
    """`https://gofile.io/d/{code}` 形式かを見て、コードだけを返す (§6.1 条件1・2)。"""
    if not isinstance(url, str):
        return None
    m = _GOFILE_URL_RE.match(url.strip())
    return m.group(1) if m else None


def is_valid_code(code: str | None) -> bool:
    return bool(code) and bool(_CODE_RE.match(code))


def gofile_page_url(code: str) -> str:
    return f"https://gofile.io/d/{code}"


def is_allowed_thumbnail_host(hostname: str) -> bool:
    h = (hostname or "").lower()
    return h == GOFILE_HOST or h.endswith(GOFILE_THUMB_SUFFIX)


def is_allowed_fetch_host(hostname: str) -> bool:
    return (hostname or "").lower() in FETCH_ALLOWED_HOSTS


def validate_thumbnail_url(url: str) -> bool:
    """HTTPS + 許可ホストのみ (§9.3・§11)。DOMに設定する直前にも呼ぶ想定。"""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and is_allowed_thumbnail_host(parts.hostname or "")


def is_https_url(url: str) -> bool:
    """HTTPSであることだけを見る、ホスト非依存の汎用チェック。

    投稿者アバター画像（twimg.com等、gofileの許可ホスト外）向け。
    サーバはこの画像を取得しない（ブラウザが直接読み込む）ので
    SSRF対策のホスト絞り込みは不要だが、`javascript:`等の
    危険なスキームがDOMへ渡らないようにだけは最低限確認する。
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def _resolves_to_public_ip(hostname: str) -> bool:
    """ホスト名の名前解決先が、ループバック/プライベート/リンクローカルでないか。

    DNSリバインディング対策。許可ホストであっても、名前解決の結果が
    ループバック・プライベートIP・NAS内ネットワークを指すなら拒否する (§11)。
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        raw_ip = info[4][0]
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return False
    return True


async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    allowed_host_check,
    method: str = "GET",
    **kwargs,
) -> httpx.Response:
    """許可ホスト・HTTPS・プライベートIP拒否・リダイレクト再検証を行うHTTP取得。

    ★follow_redirects は使わない★ httpx にリダイレクトを任せると、
    転送先のホストを検証する隙が無くなる。1ホップごとに自前で確認する (§11)。
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme != "https":
            raise SecurityError(f"httpsではありません: {current}")
        host = parts.hostname or ""
        if not allowed_host_check(host):
            raise SecurityError(f"許可されていないホストです: {host}")
        if not _resolves_to_public_ip(host):
            raise SecurityError(f"ループバック/プライベートIPへの到達です: {host}")

        resp = await client.request(method, current, follow_redirects=False, **kwargs)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                raise SecurityError("リダイレクト応答にLocationがありません")
            current = urljoin(current, location)
            # 3xx は通常ボディを持たないが、接続を明示的に閉じてから次へ進む
            await resp.aclose()
            continue
        return resp
    raise SecurityError(f"リダイレクトが{MAX_REDIRECTS}回を超えました: {url}")


async def safe_stream(
    client: httpx.AsyncClient,
    url: str,
    *,
    allowed_host_check,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """`safe_get` のストリーミング版。動画本体のダウンロード用 (gofile保存機能)。

    検証内容は `safe_get` と同じ（1ホップごとのホスト再検証・プライベートIP拒否）。
    違うのは応答をメモリへ読み切らないこと。動画1本が数百MBあるため、
    `httpx.Response.aiter_bytes()` で少しずつ読める形のまま返す。
    呼び側は使い終わったら必ず `await resp.aclose()` すること。
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme != "https":
            raise SecurityError(f"httpsではありません: {current}")
        host = parts.hostname or ""
        if not allowed_host_check(host):
            raise SecurityError(f"許可されていないホストです: {host}")
        if not _resolves_to_public_ip(host):
            raise SecurityError(f"ループバック/プライベートIPへの到達です: {host}")

        req = client.build_request("GET", current, headers=headers)
        resp = await client.send(req, stream=True, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            await resp.aclose()
            if not location:
                raise SecurityError("リダイレクト応答にLocationがありません")
            current = urljoin(current, location)
            continue
        return resp
    raise SecurityError(f"リダイレクトが{MAX_REDIRECTS}回を超えました: {url}")
