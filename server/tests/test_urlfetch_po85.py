"""「URLから保存」に 85po の動画ページを貼ったときの道（urlfetch._run_po85）。

yt-dlp は 85po を取れない（generic が KVS と判定して `Unable to extract flashvars` で失敗する）ので、
`po85.resolve_video` で画質ごとの URL を割り出し、httpx でストリーム保存する。

★本物の 85po には繋がない★ 動画ページの解決（`po85.resolve_video`）を差し替え、ファイルの取得は
httpx の MockTransport で返す（get_file → 302 → 別ホストの CDN → 本体、と同じ形）。見るのは

  - 流れ（downloading → processing → done）と、`private/tmp/{題名}/{題名}.mp4` に入ること
  - 最高画質（AV1 でも高いほう）の URL を取りに行くこと
  - 飛び先ごとの SSRF 検査（LAN・http へは行かない）
  - 中止・失敗・容量不足のとき、作業ディレクトリもフォルダも残らないこと
  - 途中で切れたら Range で再開すること
"""
from __future__ import annotations

import dataclasses
import shutil
import time
from pathlib import Path

import httpx
import pytest

from app import po85, urlfetch
from app.config import settings

PAGE = "https://www.85po.net/ja/video/9001/ao-no-todai/"
GET_FILE = "https://www.85po.net/ja/get_file/3/cccc/9000/9001/9001_1080p.mp4/?v-acctoken=TOK1080"
CDN = "https://cdn.example/remote_control.php?file=XYZ.mp4&acctoken=ABC"
TMP = settings.files_root / "private" / "tmp"


@pytest.fixture()
def mgr(monkeypatch, client):
    m = urlfetch.Manager()
    monkeypatch.setattr(urlfetch, "manager", m)
    return m


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    table = {
        "www.85po.net": "104.26.1.1",
        "85po.net": "104.26.1.1",
        "cdn.example": "93.184.216.34",
        "lan.example": "192.168.1.10",
        "tail.example": "100.101.102.103",
        "www.85po.net.evil.example": "93.184.216.34",
    }

    def fake(host, port, *a, **k):
        if host not in table:
            import socket

            raise socket.gaierror("nope")
        return [(2, 1, 6, "", (table[host], 0))]

    monkeypatch.setattr(urlfetch.socket, "getaddrinfo", fake)


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(urlfetch, "_BACKOFF", 0.0)


def _wait(m: urlfetch.Manager, job_id: int, states: tuple[str, ...], timeout=10.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        for j in m.list():
            if j["id"] == job_id and j["state"] in states:
                return j
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} が {states} にならない: {m.list()}")


def _rm(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _workdirs() -> list[Path]:
    return sorted(settings.tmp_root.glob("dl_*"))


def _formats() -> list[dict]:
    return [
        {"url": GET_FILE, "height": 1080, "codec": "av1", "ext": "mp4"},
        {"url": "https://www.85po.net/ja/get_file/3/aaaa/9000/9001/9001_720p.mp4/?v-acctoken=T7", "height": 720, "codec": "av1", "ext": "mp4"},
        {"url": "https://www.85po.net/ja/get_file/3/bbbb/9000/9001/9001.mp4/?v-acctoken=T4", "height": 480, "codec": None, "ext": "mp4"},
    ]


def _streaming(r: httpx.Response) -> httpx.Response:
    """`content=` で作った応答は作った時点で読み込み済みになり、`iter_raw` が使えない。
    本物の通信と同じく「まだ読んでいない本文」にして返す（ヘッダはそのまま）。"""
    if isinstance(r.stream, httpx.ByteStream):
        return httpx.Response(r.status_code, headers=r.headers, stream=httpx.ByteStream(r.content))
    return r


class Site:
    """resolve_video と、ファイルの取得（MockTransport）の差し替え一式。"""

    def __init__(self, monkeypatch, *, title="青い灯台の散歩", body=b"0123456789" * 1000, formats=None):
        self.title = title
        self.body = body
        self.formats = formats if formats is not None else _formats()
        self.requests: list[httpx.Request] = []
        self.resolved: list[str] = []
        self.handler = self.default_handler  # 差し替えて挙動を変える
        monkeypatch.setattr(po85, "resolve_video", self.resolve)
        monkeypatch.setattr(po85, "new_client", self.new_client)

    def resolve(self, page_url, *, client=None):
        self.resolved.append(page_url)
        return {"title": self.title, "formats": list(self.formats), "duration": 150}

    def new_client(self):
        def handler(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            return _streaming(self.handler(req))

        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    # get_file は別ホストの CDN へ 302、CDN が本体を返す（Range に対応）
    def default_handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.85po.net":
            return httpx.Response(302, headers={"location": CDN})
        return self.serve(req)

    def serve(self, req: httpx.Request, body: bytes | None = None) -> httpx.Response:
        body = self.body if body is None else body
        rng = req.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            part = body[start:]
            return httpx.Response(
                206,
                content=part,
                headers={
                    "content-type": "video/mp4",
                    "content-range": f"bytes {start}-{len(body) - 1}/{len(body)}",
                    "content-length": str(len(part)),
                },
            )
        return httpx.Response(200, content=body, headers={"content-type": "video/mp4", "content-length": str(len(body))})


@pytest.fixture()
def site(monkeypatch):
    return Site(monkeypatch)


def _submit(mgr, url=PAGE):
    return mgr.submit(url)


# ---------------------------------------------------------------- 流れ


def test_saves_the_best_quality_into_a_folder_named_after_the_title(mgr, site):
    job = _submit(mgr)
    done = _wait(mgr, job.id, ("done", "error"))
    folder = TMP / "青い灯台の散歩"
    try:
        assert done["state"] == "done", done
        assert done["title"] == "青い灯台の散歩"
        assert done["dest"] == "private/tmp/青い灯台の散歩"
        assert done["percent"] == 100.0 and not done["error"]
        assert done["total_bytes"] == done["done_bytes"] == len(site.body)
        # フォルダの中に {題名}.mp4 が1本だけ。中身は取ったとおり
        assert [p.name for p in folder.iterdir()] == ["青い灯台の散歩.mp4"]
        assert (folder / "青い灯台の散歩.mp4").read_bytes() == site.body
        # 作業ディレクトリは残らない
        assert _workdirs() == []
        # 最高画質（1080p の AV1）の URL を、get_file → CDN の順に取りに行った
        urls = [str(r.url) for r in site.requests]
        assert urls == [GET_FILE, CDN]
        assert site.resolved == [PAGE]
        # Cookie は要らない・Referer はサイトの origin を付ける・圧縮させない
        assert site.requests[0].headers["referer"] == "https://www.85po.net/"
        assert site.requests[0].headers["accept-encoding"] == "identity"
    finally:
        _rm(folder)


def test_goes_through_downloading_then_processing_then_done(mgr, site, monkeypatch):
    seen: list[str] = []
    real = urlfetch._save_into_folder

    def spy(path, title):
        seen.append(job_state(mgr, job.id))
        return real(path, title)

    def job_state(m, jid):
        return next(j["state"] for j in m.list() if j["id"] == jid)

    monkeypatch.setattr(urlfetch, "_save_into_folder", spy)
    job = _submit(mgr)
    done = _wait(mgr, job.id, ("done", "error"))
    try:
        assert done["state"] == "done", done
        assert seen == ["processing"]
    finally:
        _rm(TMP / "青い灯台の散歩")


def test_same_title_is_not_overwritten(mgr, site):
    folders = [TMP / "青い灯台の散歩", TMP / "青い灯台の散歩 (2)"]
    try:
        a = _wait(mgr, _submit(mgr).id, ("done", "error"))
        b = _wait(mgr, _submit(mgr).id, ("done", "error"))
        assert a["dest"] == "private/tmp/青い灯台の散歩" and b["dest"] == "private/tmp/青い灯台の散歩 (2)"
        assert all((f / f"{f.name.split(' (')[0]}.mp4").is_file() for f in folders)
    finally:
        for f in folders:
            _rm(f)


def test_takes_the_page_path_not_ytdlp(mgr, site, monkeypatch):
    # yt-dlp を呼んだらそれは別の道に流れている
    import sys

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("85po は yt-dlp に渡さない")

    monkeypatch.setitem(sys.modules, "yt_dlp", Boom())
    done = _wait(mgr, _submit(mgr, "https://85po.net/en/video/9001/ao-no-todai/").id, ("done", "error"))
    try:
        assert done["state"] == "done", done
    finally:
        _rm(TMP / "青い灯台の散歩")


def test_progress_shows_on_the_job(mgr, monkeypatch):
    site = Site(monkeypatch, body=b"x" * 600_000)
    gate = {"open": False}

    def slow(req):
        if req.url.host == "www.85po.net":
            return httpx.Response(302, headers={"location": CDN})

        def gen():
            sent = 0
            while sent < len(site.body):
                end = min(sent + 100_000, len(site.body))
                while not gate["open"] and sent >= 200_000:
                    time.sleep(0.01)
                yield site.body[sent:end]
                sent = end
                time.sleep(0.02)

        return httpx.Response(200, content=gen(), headers={"content-type": "video/mp4", "content-length": str(len(site.body))})

    site.handler = slow
    job = _submit(mgr)
    try:
        running = _wait(mgr, job.id, ("downloading",))
        end = time.time() + 5
        while (running["done_bytes"] < 200_000 or not running["percent"]) and time.time() < end:
            time.sleep(0.02)
            running = next(j for j in mgr.list() if j["id"] == job.id)
        assert running["total_bytes"] == 600_000 and running["title"] == "青い灯台の散歩"
        assert 0 < running["percent"] < 100
        gate["open"] = True
        done = _wait(mgr, job.id, ("done", "error"))
        assert done["state"] == "done", done
    finally:
        gate["open"] = True
        _rm(TMP / "青い灯台の散歩")


# ---------------------------------------------------------------- 失敗・中止・後片付け


def _assert_clean(title="青い灯台の散歩"):
    assert _workdirs() == []
    assert not (TMP / title).exists()


def test_cancel_mid_download_leaves_nothing_behind(mgr, monkeypatch):
    site = Site(monkeypatch, body=b"x" * 5_000_000)

    def endless(req):
        if req.url.host == "www.85po.net":
            return httpx.Response(302, headers={"location": CDN})

        def gen():
            for _ in range(50):
                yield b"x" * 100_000
                time.sleep(0.02)

        return httpx.Response(200, content=gen(), headers={"content-type": "video/mp4", "content-length": "5000000"})

    site.handler = endless
    job = _submit(mgr)
    running = _wait(mgr, job.id, ("downloading",))
    end = time.time() + 5
    while running["done_bytes"] == 0 and time.time() < end:
        time.sleep(0.02)
        running = next(j for j in mgr.list() if j["id"] == job.id)
    assert running["done_bytes"] > 0 and any(w.is_dir() for w in _workdirs())
    assert mgr.cancel(job.id)
    done = _wait(mgr, job.id, ("cancelled", "done", "error"))
    assert done["state"] == "cancelled", done
    time.sleep(0.2)
    _assert_clean()


def test_cancel_while_the_page_is_being_read_never_downloads(mgr, monkeypatch):
    site = Site(monkeypatch)

    def slow_resolve(page_url, *, client=None):
        time.sleep(0.4)
        return {"title": "青い灯台の散歩", "formats": _formats(), "duration": 1}

    monkeypatch.setattr(po85, "resolve_video", slow_resolve)
    job = _submit(mgr)
    time.sleep(0.15)
    assert mgr.cancel(job.id)
    done = _wait(mgr, job.id, ("cancelled", "done", "error"))
    assert done["state"] == "cancelled", done
    time.sleep(0.5)
    assert site.requests == []  # ファイルは取りに行っていない
    assert next(j for j in mgr.list() if j["id"] == job.id)["state"] == "cancelled"
    _assert_clean()


@pytest.mark.parametrize(
    "code,word",
    [(403, "拒否"), (404, "見つかりません"), (410, "410")],
)
def test_http_failure_is_a_short_japanese_message(mgr, site, code, word):
    site.handler = lambda req: (
        httpx.Response(302, headers={"location": CDN}) if req.url.host == "www.85po.net"
        else httpx.Response(code, content=b"no")
    )
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and word in done["error"], done
    _assert_clean()


def test_a_page_error_from_resolve_is_shown_as_is(mgr, monkeypatch):
    def fail(page_url, *, client=None):
        raise po85.FetchError("ログインが必要な動画は取れません")

    monkeypatch.setattr(po85, "resolve_video", fail)
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and done["error"] == "ログインが必要な動画は取れません"
    _assert_clean()


def test_html_instead_of_a_video_is_refused(mgr, site):
    site.handler = lambda req: (
        httpx.Response(302, headers={"location": CDN}) if req.url.host == "www.85po.net"
        else httpx.Response(200, content=b"<html>blocked</html>", headers={"content-type": "text/html"})
    )
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "動画ではないページ" in done["error"], done
    _assert_clean()


def test_redirect_to_a_private_address_is_never_followed(mgr, site):
    site.handler = lambda req: (
        httpx.Response(302, headers={"location": "https://lan.example/secret.mp4"})
        if req.url.host == "www.85po.net"
        else httpx.Response(200, content=b"internal")
    )
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "自宅ネットワーク" in done["error"], done
    assert [r.url.host for r in site.requests] == ["www.85po.net"]  # LAN には行っていない
    _assert_clean()


def test_redirect_to_http_or_a_tailnet_address_is_refused(mgr, site):
    for target in ("http://cdn.example/x.mp4", "https://tail.example/x.mp4"):
        site.requests.clear()
        site.handler = lambda req, target=target: (
            httpx.Response(302, headers={"location": target}) if req.url.host == "www.85po.net"
            else httpx.Response(200, content=b"x")
        )
        done = _wait(mgr, _submit(mgr).id, ("done", "error"))
        assert done["state"] == "error", (target, done)
        assert [r.url.host for r in site.requests] == ["www.85po.net"]
    _assert_clean()


def test_a_media_url_that_is_not_public_is_refused_before_any_request(mgr, monkeypatch):
    site = Site(monkeypatch, formats=[{"url": "https://lan.example/a.mp4", "height": 720, "codec": None, "ext": "mp4"}])
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "自宅ネットワーク" in done["error"], done
    assert site.requests == []
    _assert_clean()


def test_too_many_redirects(mgr, site):
    site.handler = lambda req: httpx.Response(302, headers={"location": GET_FILE})
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "転送が多すぎ" in done["error"], done
    _assert_clean()


def test_not_enough_space_is_refused_before_downloading(mgr, site, monkeypatch):
    class Usage:
        free = 100  # MIN_FREE_BYTES を切る

    monkeypatch.setattr(urlfetch.shutil, "disk_usage", lambda p: Usage)
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "空き" in done["error"], done
    assert site.requests == [] and site.resolved == []
    _assert_clean()


def test_not_enough_space_for_this_file_is_refused_when_the_size_is_known(mgr, site, monkeypatch):
    # 作業前の確認は通るが、「このファイルを入れたら MIN_FREE_BYTES を割る」ときは落とさない
    calls = {"n": 0}

    class Usage:
        def __init__(self, free):
            self.free = free

    def usage(p):
        calls["n"] += 1
        return Usage(urlfetch.MIN_FREE_BYTES + 5_000) if calls["n"] == 1 else Usage(urlfetch.MIN_FREE_BYTES + len(site.body) - 1)

    monkeypatch.setattr(urlfetch.shutil, "disk_usage", usage)
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "空き" in done["error"], done
    _assert_clean()


def test_a_file_over_the_limit_is_refused(mgr, site, monkeypatch):
    small = dataclasses.replace(settings, max_upload_mb=1)
    monkeypatch.setattr(urlfetch, "settings", small)
    site.body = b"x" * (2 * 1024 * 1024)
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "大きすぎ" in done["error"], done
    _assert_clean()


def test_the_limit_also_stops_a_stream_with_no_content_length(mgr, site, monkeypatch):
    small = dataclasses.replace(settings, max_upload_mb=1)
    monkeypatch.setattr(urlfetch, "settings", small)

    def chunked(req):
        if req.url.host == "www.85po.net":
            return httpx.Response(302, headers={"location": CDN})

        def gen():
            for _ in range(20):
                yield b"x" * 200_000

        return httpx.Response(200, content=gen(), headers={"content-type": "video/mp4"})

    site.handler = chunked
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "大きすぎ" in done["error"], done
    _assert_clean()


def test_an_empty_response_is_an_error(mgr, site):
    site.handler = lambda req: (
        httpx.Response(302, headers={"location": CDN}) if req.url.host == "www.85po.net"
        else httpx.Response(200, content=b"", headers={"content-type": "video/mp4", "content-length": "0"})
    )
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "空の応答" in done["error"], done
    _assert_clean()


# ---------------------------------------------------------------- 途中で切れたとき


def test_resumes_from_where_it_was_cut(mgr, site):
    body = bytes(range(256)) * 2000  # 512,000 バイト
    site.body = body
    state = {"cut": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.85po.net":
            return httpx.Response(302, headers={"location": CDN})
        if not state["cut"] and not req.headers.get("range"):
            state["cut"] = True

            def gen():
                yield body[:300_000]
                raise httpx.ReadError("connection reset")

            return httpx.Response(200, content=gen(), headers={"content-type": "video/mp4", "content-length": str(len(body))})
        return site.serve(req, body)

    site.handler = handler
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    try:
        assert done["state"] == "done", done
        assert (TMP / "青い灯台の散歩" / "青い灯台の散歩.mp4").read_bytes() == body
        ranges = [r.headers.get("range") for r in site.requests if r.url.host == "cdn.example"]
        assert ranges == [None, "bytes=300000-"]
        assert _workdirs() == []
    finally:
        _rm(TMP / "青い灯台の散歩")


def test_restarts_from_zero_when_the_server_ignores_range(mgr, site):
    body = b"abcdefghij" * 30_000
    site.body = body
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.85po.net":
            return httpx.Response(302, headers={"location": CDN})
        state["n"] += 1
        if state["n"] == 1:

            def gen():
                yield body[:100_000]
                raise httpx.ReadError("reset")

            return httpx.Response(200, content=gen(), headers={"content-type": "video/mp4", "content-length": str(len(body))})
        # Range を無視して全部返す
        return httpx.Response(200, content=body, headers={"content-type": "video/mp4", "content-length": str(len(body))})

    site.handler = handler
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    try:
        assert done["state"] == "done", done
        assert (TMP / "青い灯台の散歩" / "青い灯台の散歩.mp4").read_bytes() == body
    finally:
        _rm(TMP / "青い灯台の散歩")


def test_gives_up_when_it_keeps_failing_without_progress(mgr, site):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.85po.net":
            return httpx.Response(302, headers={"location": CDN})
        return httpx.Response(503, content=b"busy")

    site.handler = handler
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error" and "途中で切れました" in done["error"], done
    assert len([r for r in site.requests if r.url.host == "cdn.example"]) == urlfetch._STALL_LIMIT
    _assert_clean()


def test_a_truncated_body_that_never_completes_is_an_error(mgr, site):
    # Content-Length より短いまま終わり続ける（サーバが Range を受けない）
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.85po.net":
            return httpx.Response(302, headers={"location": CDN})
        return httpx.Response(200, content=b"x" * 1000, headers={"content-type": "video/mp4", "content-length": "5000"})

    site.handler = handler
    done = _wait(mgr, _submit(mgr).id, ("done", "error"))
    assert done["state"] == "error", done
    _assert_clean()


def test_cancel_during_the_wait_before_a_retry_is_immediate(mgr, site, monkeypatch):
    monkeypatch.setattr(urlfetch, "_BACKOFF", 30.0)  # 待ちが長くても中止は即効く

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.85po.net":
            return httpx.Response(302, headers={"location": CDN})
        return httpx.Response(503, content=b"busy")

    site.handler = handler
    job = _submit(mgr)
    time.sleep(0.3)
    t0 = time.time()
    assert mgr.cancel(job.id)
    done = _wait(mgr, job.id, ("cancelled", "error", "done"), timeout=5)
    assert done["state"] == "cancelled" and time.time() - t0 < 3
    _assert_clean()


# ---------------------------------------------------------------- 受け付け側は既存のまま


def test_same_page_is_not_queued_twice_while_active(mgr, site, monkeypatch):
    monkeypatch.setattr(mgr, "_run", lambda job: time.sleep(0.3))
    a = mgr.submit(PAGE)
    b = mgr.submit(PAGE)
    assert a.id == b.id
    mgr.cancel(a.id)


def test_other_hosts_still_use_the_ytdlp_path(mgr, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(mgr, "_run_po85", lambda job: called.append("po85"))
    monkeypatch.setattr(mgr, "_run_gofile", lambda job, code: called.append("gofile"))
    # yt-dlp の道には入らせない（ネットに出てしまうので、動画の解析だけ失敗させる）
    import yt_dlp

    class Failing:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError("Unsupported URL: " + url)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Failing)
    job = mgr.submit("https://www.85po.net.evil.example/ja/video/1/x/")
    done = _wait(mgr, job.id, ("done", "error"))
    assert done["state"] == "error" and "見つかりません" in done["error"], done
    assert called == []
