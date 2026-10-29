#!/usr/bin/env python3
"""
predictive-governor / collector.py

Сборщик метрик с частотой 1 Гц для предиктивного говернора.

Читает напрямую из /proc и sysfs, пишет в CSV:
    /var/log/predictive_governor/metrics.csv

Ротация: файлы старше 14 дней → gzip (*.csv.gz), при желании удаляются
по настройке KEEP_DAYS. Атомарный flush каждую секунду — данные не теряются
при SIGKILL.

Запуск:
    python3 collector.py                 # демон, 1 Гц, бесконечно
    python3 collector.py --once          # один снимок (для отладки)
    python3 collector.py --verbose       # печать в stderr

Зависимости: только стандартная библиотека. numpy не требуется.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

# ─── Константы (зашиты под ug48 / i7-975) ─────────────────
LOG_DIR = Path("/var/log/predictive_governor")
CSV_PATH = LOG_DIR / "metrics.csv"
CSV_HEADER = [
    "ts",               # ISO 8601 UTC
    "epoch",            # unix seconds (float)
    "cpu_util_pct",     # среднее по 8 логическим CPU, 0..100
    "cpu_iowait_pct",   # доля iowait
    "cpu_user_pct",
    "cpu_sys_pct",
    "mem_used_pct",
    "mem_available_kb",
    "net_rx_bytes_s",
    "net_tx_bytes_s",
    "core_temp_max_c",
    "loadavg_1m",
    "ncpu",
]
KEEP_DAYS = 14
ROTATE_CHECK_EVERY = 3600   # раз в час проверяем ротацию
HWMON_CORETEMP = None       # определим при старте

NCORES = os.cpu_count() or 8


# ─── Утилиты чтения ───────────────────────────────────────
def _read_text(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


def read_cpu_stat() -> Tuple[int, int, int, int, int, int, int, int]:
    """
    Возвращает (user, nice, system, idle, iowait, irq, softirq, steal) —
    агрегированные значения по первой строке cpu из /proc/stat.
    """
    line = _read_text("/proc/stat").splitlines()[0]
    parts = line.split()
    # parts[0] == 'cpu'
    vals = [int(x) for x in parts[1:9]]
    # добиваем до 8 полей, если старые ядра
    while len(vals) < 8:
        vals.append(0)
    return tuple(vals[:8])  # type: ignore


def read_meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    with open("/proc/meminfo", "r") as f:
        for line in f:
            k, _, v = line.partition(":")
            try:
                out[k.strip()] = int(v.strip().split()[0])
            except (ValueError, IndexError):
                continue
    return out


def read_netdev() -> Dict[str, Tuple[int, int]]:
    """
    Возвращает {iface: (rx_bytes, tx_bytes)} по /proc/net/dev.
    Считаем по всем интерфейсам, кроме lo — чтобы не терять локальный трафик
    (но lo обычно шумный; можно исключить при желании).
    """
    out: Dict[str, Tuple[int, int]] = {}
    with open("/proc/net/dev", "r") as f:
        lines = f.readlines()[2:]
        for line in lines:
            name, _, rest = line.partition(":")
            name = name.strip()
            vals = rest.split()
            if len(vals) < 9:
                continue
            out[name] = (int(vals[0]), int(vals[8]))
    return out


def find_coretemp() -> Optional[Path]:
    """Возвращает путь к hwmon с name=coretemp (обычно hwmon1 на ug48)."""
    base = Path("/sys/class/hwmon")
    if not base.exists():
        return None
    for d in base.iterdir():
        name_file = d / "name"
        try:
            if name_file.read_text().strip() == "coretemp":
                return d
        except OSError:
            continue
    return None


def read_core_temps(coretemp_dir: Optional[Path]) -> float:
    """Максимальная температура среди temp*_input (мК → °C)."""
    if not coretemp_dir:
        return 0.0
    mx = 0
    for t in coretemp_dir.glob("temp*_input"):
        try:
            v = int(t.read_text().strip())
            if v > mx:
                mx = v
        except (OSError, ValueError):
            continue
    return mx / 1000.0 if mx else 0.0


def read_loadavg_1m() -> float:
    try:
        return float(_read_text("/proc/loadavg").split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


# ─── Дельта-агрегаторы (для net) ──────────────────────────
class NetDelta:
    """Считает bytes/sec для RX/TX между двумя снимками."""

    def __init__(self) -> None:
        self.prev: Dict[str, Tuple[int, int]] = {}
        self.prev_ts: Optional[float] = None

    def update(self, now: float) -> Tuple[float, float]:
        cur = read_netdev()
        if self.prev_ts is None or not self.prev:
            self.prev = cur
            self.prev_ts = now
            return 0.0, 0.0
        dt = max(now - self.prev_ts, 1e-6)
        # суммируем все iface кроме lo
        rx = 0; tx = 0
        for iface, (c_rx, c_tx) in cur.items():
            if iface == "lo" or iface not in self.prev:
                continue
            p_rx, p_tx = self.prev[iface]
            rx += max(c_rx - p_rx, 0)
            tx += max(c_tx - p_tx, 0)
        self.prev = cur
        self.prev_ts = now
        return rx / dt, tx / dt


# ─── Ротация ──────────────────────────────────────────────
def rotate_old_csv() -> None:
    """Ротация: *.csv старше KEEP_DAYS → gzip; gzip старше KEEP_DAYS+KEEP_DAYS → удалить."""
    if not LOG_DIR.exists():
        return
    now = time.time()
    keep_sec = KEEP_DAYS * 86400
    for f in LOG_DIR.glob("metrics-*.csv"):
        try:
            if now - f.stat().st_mtime > keep_sec:
                gz = f.with_suffix(".csv.gz")
                with open(f, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst)
                f.unlink()
                log(f"ротация: {f.name} → {gz.name}")
        except OSError as e:
            log(f"ошибка ротации {f}: {e}")
    # старые gz (старше 2*KEEP_DAYS) — удаляем
    for g in LOG_DIR.glob("metrics-*.csv.gz"):
        try:
            if now - g.stat().st_mtime > 2 * keep_sec:
                g.unlink()
                log(f"удаление старой ротации: {g.name}")
        except OSError as e:
            log(f"ошибка удаления {g}: {e}")


# ─── CSV writer ───────────────────────────────────────────
class CsvWriter:
    """
    Открывает CSV на дозапись, пишет заголовок при первом запуске.
    Каждая строка — flush + fsync раз в секунду (для надёжности при сбоях).
    """
    def __init__(self, path: Path, header):
        self.path = path
        self.header = header
        new_file = not path.exists() or path.stat().st_size == 0
        self.f = open(path, "a", newline="")
        self.w = csv.writer(self.f)
        if new_file:
            self.w.writerow(header)
            self.f.flush()
            os.fsync(self.f.fileno())

    def write(self, row) -> None:
        self.w.writerow(row)
        self.f.flush()
        try:
            os.fsync(self.f.fileno())
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.f.close()
        except OSError:
            pass


# ─── Логгирование ─────────────────────────────────────────
def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[collector] {ts} {msg}", file=sys.stderr, flush=True)


# ─── Снимок ───────────────────────────────────────────────
def take_snapshot(
    prev_cpu: Optional[Tuple[int, ...]],
    net: NetDelta,
    coretemp_dir: Optional[Path],
) -> Tuple[list, Tuple[int, ...]]:
    """
    Возвращает (row, new_prev_cpu). Если prev_cpu is None — util=0.
    """
    now = time.time()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cur_cpu = read_cpu_stat()
    if prev_cpu is None:
        util = iow = user = sysc = 0.0
    else:
        d = [c - p for c, p in zip(cur_cpu, prev_cpu)]
        total = sum(d)
        if total <= 0:
            util = iow = user = sysc = 0.0
        else:
            # d = (user, nice, system, idle, iowait, irq, softirq, steal)
            user_pct = (d[0] + d[1]) * 100.0 / total
            sys_pct = (d[2] + d[5] + d[6]) * 100.0 / total
            iowait_pct = d[4] * 100.0 / total
            idle_pct = d[3] * 100.0 / total
            util = max(0.0, 100.0 - idle_pct - iowait_pct)
            iow, user, sysc = iowait_pct, user_pct, sys_pct

    mem = read_meminfo()
    total_kb = mem.get("MemTotal", 0) or 1
    avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    used_pct = 100.0 * (total_kb - avail_kb) / total_kb

    rx_bps, tx_bps = net.update(now)
    temp_c = read_core_temps(coretemp_dir)
    load1 = read_loadavg_1m()

    row = [
        ts,
        f"{now:.3f}",
        f"{util:.2f}",
        f"{iow:.2f}",
        f"{user:.2f}",
        f"{sysc:.2f}",
        f"{used_pct:.2f}",
        str(avail_kb),
        f"{rx_bps:.1f}",
        f"{tx_bps:.1f}",
        f"{temp_c:.1f}",
        f"{load1:.2f}",
        str(NCORES),
    ]
    return row, cur_cpu


# ─── Main ─────────────────────────────────────────────────
_stop = False

def _sig_handler(signum, frame):
    global _stop
    _stop = True
    log(f"получен сигнал {signum}, завершаемся")


def main() -> int:
    ap = argparse.ArgumentParser(description="predictive-governor metrics collector")
    ap.add_argument("--once", action="store_true", help="один снимок и выход")
    ap.add_argument("--verbose", action="store_true", help="печать каждой записи")
    ap.add_argument("--interval", type=float, default=1.0, help="интервал сбора, с")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    coretemp_dir = find_coretemp()
    if coretemp_dir is None:
        log("coretemp не найден — temperature=0.0")
    else:
        log(f"coretemp: {coretemp_dir}")

    net = NetDelta()
    prev_cpu: Optional[Tuple[int, ...]] = None
    # первый снимок для дельт — чтобы не писать util=0 при первом тике
    prev_cpu = read_cpu_stat()
    net.update(time.time())

    if args.once:
        row, _ = take_snapshot(prev_cpu, net, coretemp_dir)
        print(",".join(row))
        return 0

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    writer = CsvWriter(CSV_PATH, CSV_HEADER)
    log(f"пишем в {CSV_PATH} каждые {args.interval} с")
    next_rotate = time.time() + ROTATE_CHECK_EVERY
    last_prev = prev_cpu

    try:
        while not _stop:
            t0 = time.monotonic()
            row, last_prev = take_snapshot(last_prev, net, coretemp_dir)
            writer.write(row)
            if args.verbose:
                log(",".join(row))

            if time.time() >= next_rotate:
                rotate_old_csv()
                next_rotate = time.time() + ROTATE_CHECK_EVERY

            dt = args.interval - (time.monotonic() - t0)
            if dt > 0:
                time.sleep(dt)
    finally:
        writer.close()
        log("collector остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
