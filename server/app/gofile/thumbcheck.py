"""サムネイル確認 (§6.3)。"""
from __future__ import annotations

import logging

import httpx

from ..config import settings
from . import security

log = logging.getLogger("nasapp.gofile.thumbcheck")


async def check_thumbnail(client: httpx.AsyncClient, url: str) -> str:
    """HEAD → (非対応なら) Range付きGET の順で確認する。

    戻り値は thumbnail_status: 'valid' | 'invalid' | 'unknown'。
    """
    if not security.validate_thumbnail_url(url):
        return "invalid"

    timeout = httpx.Timeout(settings.gofile.thumb_check_timeout_s)
    try:
        resp = await security.safe_get(
            client, url, allowed_host_check=security.is_allowed_thumbnail_host,
            method="HEAD", timeout=timeout,
        )
        if resp.status_code in (405, 501):
            # HEAD非対応。先頭バイトだけ Range で取る
            resp = await security.safe_get(
                client, url, allowed_host_check=security.is_allowed_thumbnail_host,
                method="GET", timeout=timeout, headers={"Range": "bytes=0-2047"},
            )
    except security.SecurityError:
        return "invalid"
    except httpx.TimeoutException:
        return "unknown"
    except httpx.HTTPError as e:
        log.info("サムネイル確認で通信エラー %s: %s", url, e)
        return "unknown"

    if resp.status_code not in (200, 206):
        return "invalid"
    content_type = resp.headers.get("content-type", "")
    if not content_type.split(";")[0].strip().lower().startswith("image/"):
        return "invalid"
    return "valid"
