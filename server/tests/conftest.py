"""単体で走らせるための最小の土台。**ホストアプリに組み込んだら捨てる**
（ホストの conftest が `client` / `token` を持っているはず）。

nas-app のテストは `app.main` を丸ごと起こして実ログインで token を取る。
ここには認証もアプリ本体も無いので、`app.auth.require_auth`（Bearer が合えば通す）の
代役を差し込み、tokyomotion / po85 のルータだけを載せた FastAPI を作る。
gofile_gallery と urlfetch は db / config / privatefiles が要るので、この土台では走らない
（test_gofile_gallery.py / test_urlfetch_po85.py は組み込み先で通すこと）。
"""
from __future__ import annotations

import sys
import types

import pytest
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

TEST_TOKEN = "test-token"
_bearer = HTTPBearer(auto_error=False)


async def require_auth(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    if not cred or cred.credentials != TEST_TOKEN:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ログインが必要です")
    return "user"


if "app.auth" not in sys.modules:
    _auth = types.ModuleType("app.auth")
    _auth.require_auth = require_auth  # type: ignore[attr-defined]
    sys.modules["app.auth"] = _auth

from app.routers import po85 as po85_router  # noqa: E402
from app.routers import tokyomotion as tokyomotion_router  # noqa: E402


@pytest.fixture(scope="session")
def client():
    app = FastAPI()
    app.include_router(tokyomotion_router.router)
    app.include_router(po85_router.router)
    return TestClient(app)


@pytest.fixture()
def token() -> str:
    return TEST_TOKEN


@pytest.fixture()
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
