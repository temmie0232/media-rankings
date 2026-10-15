"""NASの負荷確認 (§14.5)。routers/system.py の nas_status と同じ /proc, /sys の
読み方を使う。コンテナ内から見えるのがホスト全体の値かコンテナぶんかは
カーネルcgroup構成に依存するが、いずれにせよ「悪化したら延期する」という
安全側の判定に使うだけなので、取れる値をそのまま使えば十分。
"""
from __future__ import annotations

import logging
import os

from ..config import settings

log = logging.getLogger("nasapp.gofile.hostload")


def _read_first(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def free_memory_mb() -> int | None:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                info[k] = int(v.strip().split()[0])  # kB
        avail = info.get("MemAvailable")
        return avail // 1024 if avail is not None else None
    except Exception:
        return None


def load_average_1m() -> float | None:
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def soc_temp_c() -> float | None:
    temp = _read_first("/sys/class/thermal/thermal_zone0/temp")
    if temp and temp.lstrip("-").isdigit():
        return int(temp) / 1000
    return None


def should_defer_browser_checks() -> tuple[bool, str | None]:
    """§14.5: Playwright検証を始めてよいか。

    延期した項目は unknown ではなく pending のまま次回へ回す
    （§14.5「延期した項目は pending とし、障害件数へ含めない」）。
    """
    g = settings.gofile

    free_mb = free_memory_mb()
    if free_mb is not None and free_mb < g.min_free_memory_mb:
        return True, f"空きメモリ不足 ({free_mb}MB < {g.min_free_memory_mb}MB)"

    load1 = load_average_1m()
    if load1 is not None and load1 > g.max_load_average:
        return True, f"CPU負荷が高い (load1={load1:.2f} > {g.max_load_average})"

    temp = soc_temp_c()
    # Pi 4 は80℃前後がサーマルスロットリングの目安。安全側に70℃で止める
    if temp is not None and temp >= 70.0:
        return True, f"CPU温度が高い ({temp:.1f}°C)"

    return False, None
