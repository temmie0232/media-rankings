"""85po の一覧（ランキング）。docs/po85-api.md。

取得・パース・キャッシュは app/po85.py。ここは入力の検証と
エラーの HTTP への写しだけ。保存は既存の `POST /api/media/fetch`
（urlfetch.py の `_run_po85`）をそのまま使うので、このルーターに保存口は無い。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import po85
from ..auth import require_auth

router = APIRouter(prefix="/api", tags=["po85"])


@router.get("/po85/ranking")
async def ranking(
    _: Annotated[str, Depends(require_auth)],
    sort: Annotated[str, Query()] = po85.DEFAULT_SORT,
    period: Annotated[str, Query()] = po85.DEFAULT_PERIOD,
    page: Annotated[str, Query()] = "1",
) -> dict:
    """一覧の1ページ（24件）。

    page は int ではなく文字列で受けて自前で検証する。FastAPI の型検証に
    任せると不正値が 422（英語のメッセージ）になり、sort/period の 400 と揃わない。
    新着（recent）は期間を持たないので period は読み捨てる（不正な値は 400 のまま）。
    """
    try:
        s, p, n = po85.validate_params(sort, period, page)
    except po85.ParamError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    try:
        return await po85.get_ranking(s, p, n)
    except po85.FetchError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
