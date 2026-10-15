"""gofile-douga.com からの新着取得 (§5 データ取得仕様)。"""
from __future__ import annotations

import logging

import httpx

from ..config import settings
from . import security

log = logging.getLogger("nasapp.gofile.client")

# 1回のAPI呼び出しで要求する件数。§5.2 の「初回取得件数」「最大取得件数」は
# 総取得件数の上限であって、このページサイズとは別軸なので固定値にする
_PAGE_SIZE = 100
# 安全弁。hasMore が壊れて true を返し続けても無限ループしない
_MAX_PAGES = 20
# 連続してDB既知のコードに当たった回数。§5.2-2「連続して現れた時点で終了」の
# 「連続」を1件ではなく3件にしているのは、並び順が厳密に新着順でない
# （同時刻投稿が入れ替わる等）場合に1件のたまたまの一致で打ち切らないため
_KNOWN_STREAK_TO_STOP = 3

# 同期する人気ランキングのタブ。gofile-douga.com は 12h/24h/1d/3d を持つが、
# まずは1本に絞る（実測: 24hは60件・1ページで完結。物足りなければ増やす）
RANKING_TAB = "24h"
# 安全弁。ランキングは実測で1ページ(60件)で終わるが、新着と同程度の
# 上限は持たせておく
_RANKING_MAX_PAGES = 20


class FetchError(Exception):
    """取得失敗。§5.1: この場合は今回の同期を失敗扱いにし、既存データを消さない。"""


def _validate_item(raw: dict) -> dict | None:
    """1件ぶんの必須項目を確認し、保存用の形に整える。

    ★1件の欠落で同期全体を失敗にはしない★
    §5.1 の「必須項目欠落時は同期を失敗扱い」は応答全体（items/hasMoreの
    有無など）についての規定と解釈する。1件だけ壊れているのは元サイト側の
    よくある部分的な不備で、そのために他の正常な新着まで捨てる理由が無い。
    """
    if not isinstance(raw, dict):
        return None
    gofile_url = raw.get("gofileUrl")
    thumb = raw.get("representativeThumbnailUrl")
    if not gofile_url or not thumb:
        return None
    code = security.extract_gofile_code(gofile_url)
    if not code:
        return None
    if not security.validate_thumbnail_url(thumb):
        return None
    profile_image_url = raw.get("profileImageUrl")
    return {
        "source_item_id": str(raw["id"]) if raw.get("id") is not None else None,
        "gofile_code": code,
        "gofile_url": security.gofile_page_url(code),
        "title": (str(raw.get("contentTitle") or "").strip() or None),
        "thumbnail_url": thumb,
        "thumbnail_urls_json": raw.get("thumbnailUrls"),
        "source_posted_at": raw.get("sourcePostedAt"),
        "file_count": raw.get("fileCount"),
        "total_size_bytes": raw.get("totalSizeBytes"),
        "source_status": raw.get("status"),
        # 投稿者情報。/new・/rankings どちらの応答にも入っている。
        # ★ここでは raw の "rank" は拾わない★ /new の rank は「新着内の
        # 並び順」であって人気度ではない。人気ランキングの rank
        # (fetch_rankings 側で別途扱う) と意味が違うので混ぜない
        "source_x_account_id": (str(raw.get("sourceXAccountId") or "").strip() or None),
        "profile_image_url": (
            profile_image_url if security.is_https_url(profile_image_url) else None
        ),
    }


async def fetch_new(existing_codes: set[str], *, is_first_run: bool) -> tuple[list[dict], int]:
    """新着を取得する。

    停止条件 (§5.2):
      1. DB既知のコードが連続して現れた
      2. hasMore が false
      3. 最大ページ数 / 最大取得件数に到達

    `is_first_run` のときは「初回取得件数」を、それ以外は「最大取得件数」を
    総件数の上限にする。

    戻り値: (妥当な項目のリスト, 取得ページ数)
    """
    max_total = settings.gofile.initial_fetch_limit if is_first_run else settings.gofile.max_fetch_limit

    out: list[dict] = []
    offset = 0
    pages = 0
    known_streak = 0

    async with httpx.AsyncClient(
        timeout=20.0, headers={"User-Agent": settings.gofile.user_agent}
    ) as client:
        while True:
            url = f"{settings.gofile.fetch_base_url}/api/new?limit={_PAGE_SIZE}&offset={offset}"
            try:
                resp = await security.safe_get(
                    client, url, allowed_host_check=security.is_allowed_fetch_host
                )
            except security.SecurityError as e:
                raise FetchError(f"許可されていない取得先です: {e}") from e
            except httpx.HTTPError as e:
                raise FetchError(f"取得に失敗しました: {e}") from e

            if resp.status_code != 200:
                raise FetchError(f"取得に失敗しました (HTTP {resp.status_code})")
            try:
                body = resp.json()
            except ValueError as e:
                raise FetchError("応答がJSONではありません") from e
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise FetchError("応答が不正です（ok=trueではありません）")

            items = body.get("items")
            if not isinstance(items, list):
                raise FetchError("応答にitemsがありません")
            if "hasMore" not in body:
                raise FetchError("応答にhasMoreがありません")

            pages += 1
            stop = False
            for raw in items:
                v = _validate_item(raw)
                if v is None:
                    log.warning("必須項目が欠けた新着を1件無視しました: %r", raw.get("id"))
                    continue
                out.append(v)
                if v["gofile_code"] in existing_codes:
                    known_streak += 1
                    if known_streak >= _KNOWN_STREAK_TO_STOP:
                        stop = True
                        break
                else:
                    known_streak = 0

            if stop or not body.get("hasMore"):
                break
            if len(out) >= max_total:
                break
            if pages >= _MAX_PAGES:
                log.warning("gofile-douga.com の取得が最大ページ数(%d)に到達しました", _MAX_PAGES)
                break
            offset += _PAGE_SIZE

    return out[:max_total], pages


async def fetch_rankings() -> tuple[list[dict], int]:
    """24h人気ランキング (`RANKING_TAB`) を取得する。

    §5.2 の「連続して既知なら打ち切り」はここでは使わない。人気順は新着順と
    違って単調ではなく、既知コードの後ろに未知のコードが続くこともあるため、
    毎回 hasMore が尽きるまで（またはページ上限まで）全件を取り直す。
    """
    out: list[dict] = []
    offset = 0
    pages = 0

    async with httpx.AsyncClient(
        timeout=20.0, headers={"User-Agent": settings.gofile.user_agent}
    ) as client:
        while True:
            url = (
                f"{settings.gofile.fetch_base_url}/api/rankings"
                f"?tab={RANKING_TAB}&limit={_PAGE_SIZE}&offset={offset}"
            )
            try:
                resp = await security.safe_get(
                    client, url, allowed_host_check=security.is_allowed_fetch_host
                )
            except security.SecurityError as e:
                raise FetchError(f"許可されていない取得先です: {e}") from e
            except httpx.HTTPError as e:
                raise FetchError(f"取得に失敗しました: {e}") from e

            if resp.status_code != 200:
                raise FetchError(f"取得に失敗しました (HTTP {resp.status_code})")
            try:
                body = resp.json()
            except ValueError as e:
                raise FetchError("応答がJSONではありません") from e
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise FetchError("応答が不正です（ok=trueではありません）")

            items = body.get("items")
            if not isinstance(items, list):
                raise FetchError("応答にitemsがありません")
            if "hasMore" not in body:
                raise FetchError("応答にhasMoreがありません")

            pages += 1
            for raw in items:
                v = _validate_item(raw)
                if v is None:
                    log.warning(
                        "必須項目が欠けたランキング項目を1件無視しました: %r", raw.get("id")
                    )
                    continue
                rank = raw.get("rank")
                if not isinstance(rank, int):
                    log.warning("rankが欠けたランキング項目を1件無視しました: %r", raw.get("id"))
                    continue
                v["ranking_rank"] = rank
                out.append(v)

            if not body.get("hasMore"):
                break
            if pages >= _RANKING_MAX_PAGES:
                log.warning(
                    "gofile-douga.com のランキング取得が最大ページ数(%d)に到達しました",
                    _RANKING_MAX_PAGES,
                )
                break
            offset += _PAGE_SIZE

    return out, pages
