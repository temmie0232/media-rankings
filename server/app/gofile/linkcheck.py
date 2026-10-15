"""Playwrightによるページ検証 (§6.2)。

★api コンテナはこのモジュールを import しない★ Playwright(Chromium同梱)に
依存するため、gofile-worker コンテナだけが使う（パッケージの __init__.py
参照）。ページHTMLの単純なHTTP取得では JS 実行後の
`This content does not exist` を判定できないため、ヘッドレスブラウザを使う
（Gofileのクライアントコード・トークン生成・CAPTCHA等は解析・転用しない）。
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from ..config import settings
from . import security

log = logging.getLogger("nasapp.gofile.linkcheck")

# §14.4: 判定に不要な通信を遮断する（動画・音声・広告・アクセス解析・
# 外部トラッカー・Webフォント）。画像は遮断しない — 遮断条件で正常画面まで
# エラーになる場合はその条件を無効化する、という §14.4 末尾の注記に従い、
# 判定に影響しうるものは残す側に倒す
_BLOCK_RESOURCE_TYPES = {"media", "font"}
_BLOCK_URL_SUBSTRINGS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googlesyndication.com", "facebook.net", "facebook.com/tr",
    "hotjar.com", "sentry.io", "amplitude.com", "segment.com",
    "adservice.", "/ads/", "ad-delivery.net",
)

_INVALID_TEXT_MARKERS = ("This content does not exist",)


def _running_as_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


async def _route_filter(route) -> None:
    req = route.request
    if req.resource_type == "document":
        # ページ本体のナビゲーション（リダイレクト先を含む）。§11「リダイレクト先も
        # 同じ許可条件で再検証する」を、ブラウザ内の遷移についても適用する
        host = urlsplit(req.url).hostname or ""
        if not security.is_allowed_thumbnail_host(host):  # gofile.io / *.gofile.io と同じ判定
            await route.abort()
            return
    if req.resource_type in _BLOCK_RESOURCE_TYPES:
        await route.abort()
        return
    if any(s in req.url for s in _BLOCK_URL_SUBSTRINGS):
        await route.abort()
        return
    await route.continue_()


class BrowserSession:
    """30分ジョブの間だけ生きる1つのChromiumプロセス (§14.3)。

    ★常駐させない・1件ごとに起動し直さない★ ジョブ開始時に1回起動し、
    同じブラウザ内でページを1件ずつ順番に検証し、ジョブ終了時に必ず閉じる。
    利用側は `async with BrowserSession() as session:` で使う。
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None

    async def __aenter__(self) -> "BrowserSession":
        self._pw = await async_playwright().start()
        args = ["--disable-dev-shm-usage", "--disable-gpu"]
        if _running_as_root():
            # 非rootで動かすのが既定 (§14.7)。rootで起動せざるを得ない環境向けの
            # 保険としてのみ --no-sandbox を付ける（サンドボックスを弱めるので既定では付けない）
            args.append("--no-sandbox")
        self._browser = await self._pw.chromium.launch(headless=True, args=args)
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def check(self, code: str) -> str:
        """1件ぶんの判定。戻り値は link_status: 'valid' | 'invalid' | 'unknown'。"""
        if self._browser is None or not security.is_valid_code(code):
            return "unknown"

        url = security.gofile_page_url(code)
        context = await self._browser.new_context(user_agent=settings.gofile.user_agent)
        try:
            page = await context.new_page()
            await page.route("**/*", _route_filter)
            try:
                resp = await page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=settings.gofile.page_check_timeout_s * 1000,
                )
            except PlaywrightTimeout:
                return "unknown"
            except PlaywrightError as e:
                log.info("ページ検証で通信エラー %s: %s", code, e)
                return "unknown"

            if resp is None:
                return "unknown"
            if resp.status == 429 or resp.status >= 500:
                return "unknown"

            try:
                body_text = await page.inner_text("body", timeout=5000)
            except PlaywrightError:
                body_text = ""

            if any(marker in body_text for marker in _INVALID_TEXT_MARKERS):
                return "invalid"
            if resp.status == 404:
                return "invalid"
            if 200 <= resp.status < 300:
                return "valid"
            return "unknown"
        finally:
            await context.close()
