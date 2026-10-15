"""「URL から保存」の3本の入口（`/api/media/fetch`）。

nas-app の `server/app/routers/media.py`（写真ライブラリのルータ）の 275〜308 行を、
そのまま独立したルータにしたもの。ランキング一覧の「保存」ボタンはここへ URL を渡す。
実体は `app/urlfetch.py` の `manager`（yt-dlp。85po だけ専用の道）。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .. import urlfetch
from ..auth import require_auth

router = APIRouter(prefix="/api/media", tags=["media"])


class FetchIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=urlfetch.MAX_URLS_PER_REQUEST)


@router.post("/fetch")
def fetch_start(_: Annotated[str, Depends(require_auth)], body: FetchIn) -> dict:
    """貼られた URL を取得の列に積む。1本ずつ順に落とす。
    弾いた URL は rejected に理由つきで返す（積めたぶんは止めない）。"""
    jobs, rejected = [], []
    for raw in body.urls:
        if not raw.strip():
            continue
        try:
            jobs.append(urlfetch.manager.submit(raw).as_dict())
        except urlfetch.UrlRejected as e:
            rejected.append({"url": raw.strip()[:200], "error": str(e)})
    if not jobs and not rejected:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL を貼ってください")
    return {"jobs": jobs, "rejected": rejected}


@router.get("/fetch")
def fetch_list(_: Annotated[str, Depends(require_auth)]) -> dict:
    return {"jobs": urlfetch.manager.list()}


@router.delete("/fetch/{job_id}")
def fetch_cancel(_: Annotated[str, Depends(require_auth)], job_id: int) -> dict:
    """動いている・待っているものは中止、終わったものは一覧から消す。"""
    if not urlfetch.manager.cancel(job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "そのジョブはありません")
    return {"ok": True}
