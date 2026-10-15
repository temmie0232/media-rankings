"""TOKYOMOTION の一覧（ランキング）。docs/tokyomotion-api.md。

取得・パース・キャッシュは app/tokyomotion.py。ここは入力の検証と
エラーの HTTP への写しだけ。保存は既存の `POST /api/media/fetch`
（urlfetch.py）をそのまま使うので、このルーターに保存口は無い。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import tokyomotion
from ..auth import require_auth

router = APIRouter(prefix="/api", tags=["tokyomotion"])


@router.get("/tokyomotion/ranking")
async def ranking(
    _: Annotated[str, Depends(require_auth)],
    sort: Annotated[str, Query()] = tokyomotion.DEFAULT_SORT,
    period: Annotated[str, Query()] = tokyomotion.DEFAULT_PERIOD,
    page: Annotated[str, Query()] = "1",
) -> dict:
    """一覧の1ページ（20件前後）。

    page は int ではなく文字列で受けて自前で検証する。FastAPI の型検証に
    任せると不正値が 422（英語のメッセージ）になり、sort/period の 400 と揃わない。
    """
    try:
        s, p, n = tokyomotion.validate_params(sort, period, page)
    except tokyomotion.ParamError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    try:
        return await tokyomotion.get_ranking(s, p, n)
    except tokyomotion.FetchError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
