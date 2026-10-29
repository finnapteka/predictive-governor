#!/usr/bin/env python3
"""
predictive-governor / predictor.py

Предиктивный демон управления частотой CPU через MSR.

Режимы:
    predictor.py              # демон: инференс + управление частотой
    predictor.py --train      # обучение модели на metrics.csv
    predictor.py --dry-run    # предсказания без записи в MSR
    predictor.py --status     # напечатать текущее состояние и выйти

Backend'ы (в порядке приоритета):
    1. msr       — wrmsr 0x199 (FID), основной
    2. cpuidle   — disable C1 при пике (вспомогательный)
    3. nice      — renice Firefox/Telegram при пике (fallback)

Fail-Safe: при любом исключении в главном цикле — восстановить исходные
FID всех 8 CPU, cpuidle disable=0, logger -p err, exit 1.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import pickle
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ─── Константы (зашиты под ug48 / i7-975 / kernel 6.8) ────
LOG_DIR = Path("/var/log/predictive_governor")
CSV_PATH = LOG_DIR / "metrics.csv"
MODEL_PATH = LOG_DIR / "model.pkl"
META_PATH = LOG_DIR / "model.meta.json"
TRAIN_LOG = LOG_DIR / "train.log"
DAEMON_LOG = LOG_DIR / "daemon.log"

NCORES = os.cpu_count() or 8
# Лестница FID i7-975 (BCLK 133.33 МГц)
FID_LADDER = (12, 13, 14, 16, 17, 18, 19, 20, 21, 22, 23, 25)
FID_MAX = 25          # 3333 МГц
FID_DOWN = 16         # 2133 МГц (downscale)
FID_DEEP = 16  # никогда не ниже 2133 МГц

MSR_PERF_CTL = 0x199
MSR_MISC_ENABLE = 0x1A0

# Управление (значения выбраны осознанно)
PEAK_THRESHOLD = 7.0  # % средней util за окно
P_UP = 0.75                  # вероятность, при которой сразу upscale
P_DOWN = 0.30                # вероятность, при которой плавный downscale
P_DEEP = 0.05                # вероятность для deep idle
UP_CYCLES = 3                # циклов подряд для upscale
DOWN_SECONDS = 15            # секунд подряд для downscale
DEEP_SECONDS = 300           # секунд подряд для deep idle
LOOP_INTERVAL = 5.0          # период инференса, с
ANTI_STUTTER_LOAD = 0.7 * NCORES  # порог loadavg_1m для откладывания снижения
ANTI_STUTTER_DELAY = 30.0    # сек откладывания

# ─── Логгирование ─────────────────────────────────────────
def setup_logging(logfile: Path, name: str, level=logging.INFO) -> logging.Logger:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def syslog_err(msg: str) -> None:
    """Отправка в syslog через logger(1), не падая при ошибке."""
    try:
        subprocess.run(
            ["logger", "-t", "predictive-governor", "-p", "err", "--", msg],
            check=False, timeout=2,
        )
    except Exception:
        pass


# ─── MSR backend ──────────────────────────────────────────
def _norm_hex(s: str) -> str:
    s = s.strip().lower()
    if not s.startswith("0x"):
        s = "0x" + s
    return s


def rdmsr(cpu: int, reg: int) -> int:
    out = subprocess.check_output(
        ["rdmsr", "-p", str(cpu), f"0x{reg:x}"],
        text=True, timeout=2,
    ).strip()
    return int(_norm_hex(out), 16)


def wrmsr(cpu: int, reg: int, value: int) -> None:
    """Критично: передаём '0x'-префикс, иначе wrmsr парсит как десятичное."""
    subprocess.run(
        ["wrmsr", "-p", str(cpu), f"0x{reg:x}", f"0x{value:x}"],
        check=True, timeout=2,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def get_fid(cpu: int) -> int:
    return rdmsr(cpu, MSR_PERF_CTL) & 0xFF


def set_fid(cpu: int, fid: int) -> None:
    if fid not in FID_LADDER:
        raise ValueError(f"FID {fid} не в лестнице {FID_LADDER}")
    cur = rdmsr(cpu, MSR_PERF_CTL)
    new = (cur & ~0xFF) | (fid & 0xFF)
    wrmsr(cpu, MSR_PERF_CTL, new)


def read_all_fids() -> Dict[int, int]:
    return {c: get_fid(c) for c in range(NCORES)}


def write_all_fids(fid: int) -> None:
    for c in range(NCORES):
        try:
            set_fid(c, fid)
        except Exception as e:
            # продолжаем, но фиксируем
            logging.getLogger("predictor").warning(f"wrmsr cpu{c} fail: {e}")


# ─── cpuidle backend ──────────────────────────────────────
def cpuidle_path(cpu: int) -> Path:
    return Path(f"/sys/devices/system/cpu/cpu{cpu}/cpuidle")


def _find_c1_state(cpu: int) -> Optional[Path]:
    base = cpuidle_path(cpu)
    if not base.exists():
        return None
    for s in sorted(base.glob("state*")):
        try:
            if (s / "name").read_text().strip().upper() == "C1":
                return s / "disable"
        except OSError:
            continue
    return None


def read_cpuidle_disable() -> Dict[int, int]:
    out = {}
    for c in range(NCORES):
        p = _find_c1_state(c)
        if p and p.exists():
            try: out[c] = int(p.read_text().strip())
            except (OSError, ValueError): pass
    return out


def set_cpuidle_disable(value: int) -> bool:
    """Возвращает True, если хотя бы один CPU удалось записать."""
    ok = False
    err = None
    for c in range(NCORES):
        p = _find_c1_state(c)
        if not p: continue
        try:
            p.write_text(str(value))
            ok = True
        except OSError as e:
            err = e
    if not ok and err:
        logging.getLogger("predictor").warning(f"cpuidle недоступен: {err}")
    return ok


# ─── nice backend ─────────────────────────────────────────
def _pids_by_name(names: Tuple[str, ...]) -> List[int]:
    """Возвращает PID всех процессов, чья command-строка содержит любое из имён."""
    pids = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit(): continue
            try:
                cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
            except OSError:
                continue
            if any(n in cmd for n in names):
                pids.append(int(entry.name))
    except OSError:
        pass
    return pids


NICE_TARGETS = ("firefox", "telegram", "schildichat")
NICE_BOOST = 10   # при пике — nice=10 (понижение приоритета)
NICE_NORMAL = 0


def set_nice(pid: int, value: int) -> None:
    try:
        os.setpriority(os.PRIO_PROCESS, pid, value)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# ─── Модель ───────────────────────────────────────────────
FEATURES = [
    "cpu_iowait_pct", "mem_used_pct",
    "net_rx_kbps", "net_tx_kbps", "core_temp_max_c", "loadavg_1m",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos", "is_weekend",
    # далёкие лаги (без утечки); util_lag1/util_lag5 убраны — они почти = таргету
    "util_lag15", "util_lag60",
    # тренд: среднее util за 5 минут
    "util_avg_5m",
]
TARGET = "peak_next30s"   # бинарный: mean(util[t+5..t+30]) ≥ 60%
HORIZON_S = 30
LAG_SECONDS = (15, 60)   # util_lag1/util_lag5 убраны (утечка)


def _featurize(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет циклические признаки времени и лаги."""
    df = df.copy()
    ts = pd.to_datetime(df["ts"], utc=True)
    hour = ts.dt.hour + ts.dt.minute / 60.0
    dow = ts.dt.dayofweek.astype(float)
    df["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    df["dow_sin"] = np.sin(2 * np.pi * dow / 7)
    df["dow_cos"] = np.cos(2 * np.pi * dow / 7)
    df["is_weekend"] = (dow >= 5).astype(int)
    # перевод net в кбит/с для стабильности масштаба
    df["net_rx_kbps"] = df["net_rx_bytes_s"] * 8 / 1000.0
    df["net_tx_kbps"] = df["net_tx_bytes_s"] * 8 / 1000.0
    for lag in LAG_SECONDS:
        df[f"util_lag{lag}"] = df["cpu_util_pct"].shift(lag)
    # util_avg_5m: среднее за 5 мин, СДВИНУТОЕ на 30 с в прошлое
    # (без текущей точки — иначе утечка)
    df["util_avg_5m"] = (
        df["cpu_util_pct"].shift(30)
          .rolling(window=300, min_periods=60).mean()
    )
    # net: сдвигаем на 5 с (трафик инерционен, но текущий = утечка)
    df["net_rx_kbps"] = df["net_rx_kbps"].shift(5)
    df["net_tx_kbps"] = df["net_tx_kbps"].shift(5)
    return df


def load_metrics_for_train() -> pd.DataFrame:
    """metrics.csv + все gz-ротации, читаем только нужные колонки."""
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"нет {CSV_PATH}")
    frames = []
    # gz-ротации
    for gz in sorted(LOG_DIR.glob("metrics-*.csv.gz")):
        try:
            frames.append(pd.read_csv(gz))
        except Exception as e:
            print(f"[train] пропуск {gz.name}: {e}", file=sys.stderr)
    # текущий
    frames.append(pd.read_csv(CSV_PATH))
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["epoch"]).sort_values("epoch").reset_index(drop=True)
    return df


def build_training_set(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
    df = _featurize(df)
    # таргет: средняя util в окне (t+5, t+30) ≥ порог
    future = df["cpu_util_pct"].rolling(window=HORIZON_S, min_periods=5).mean().shift(-HORIZON_S)
    df[TARGET] = (future >= PEAK_THRESHOLD).astype(int)
    df = df.dropna(subset=FEATURES + [TARGET])
    X = df[FEATURES].astype(float)
    y = df[TARGET].astype(int)
    return X, y


def train_model() -> int:
    logger = setup_logging(TRAIN_LOG, "train")
    logger.info("=== начало обучения ===")
    t_start = time.time()

    try:
        df = load_metrics_for_train()
    except FileNotFoundError as e:
        logger.error(f"нет данных: {e}")
        syslog_err(f"train: {e}")
        return 2

    logger.info(f"загружено {len(df)} строк, диапазон: {df['ts'].iloc[0]} .. {df['ts'].iloc[-1]}")
    if len(df) < 3600:
        logger.warning(f"мало данных ({len(df)} строк) — модель будет слабой")

    X, y = build_training_set(df)
    if len(X) < 300:
        logger.error(f"недостаточно строк после фичей: {len(X)}")
        return 3
    if len(X) < 2000:  # модель будет слабой при малом X
        logger.warning(f"мало обучающих строк ({len(X)}), модель будет слабой")
    logger.info(f"обучающий набор: {len(X)} строк, положительных: {y.mean()*100:.1f}%")

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    # Проверка баланса классов (фикс 16)
    pos_pct = float(y.mean() * 100)
    if pos_pct < 0.5:
        logger.warning(f"positives={pos_pct:.2f}% < 0.5% — модель бесполезна при таком пороге")
        logger.warning(f"понизь PEAK_THRESHOLD или подожди больше данных")
        return 5
    logger.info(f"positives_pct={pos_pct:.2f}%  (порог {PEAK_THRESHOLD})")

    # Стратифицированный split (фикс 15) — чтобы положительные были в тесте
    stratify = y if y.nunique() > 1 and y.sum() >= 2 else None
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.2, shuffle=True, random_state=42, stratify=stratify)

    model = HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.08, max_depth=6,
        min_samples_leaf=20, l2_regularization=0.1,
        random_state=42, early_stopping=True, validation_fraction=0.1,
    )
    model.fit(X_tr, y_tr)

    # AUC с проверкой на 2 класса в тесте (фикс 14)
    if y_te.nunique() < 2:
        auc = None
        logger.warning(f"AUC не определён: в тесте {y_te.nunique()} класс(ов), "
                       f"positives в тесте = {int(y_te.sum())}")
    else:
        auc = float(roc_auc_score(y_te, model.predict_proba(X_te)[:, 1]))
        logger.info(f"model AUC={auc:.4f}")

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_PATH.with_suffix(".pkl.tmp")
    with open(tmp, "wb") as f:
        pickle.dump(model, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(MODEL_PATH)

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows_total": int(len(df)),
        "rows_train": int(len(X_tr)),
        "rows_test": int(len(X_te)),
        "positives_pct": float(y.mean() * 100),
        "auc": (float(auc) if auc is not None else None),
        "features": FEATURES,
        "peak_threshold": PEAK_THRESHOLD,
        "horizon_s": HORIZON_S,
        "duration_s": round(time.time() - t_start, 1),
    }
    META_PATH.write_text(json.dumps(meta, indent=2))
    logger.info(f"модель сохранена: {MODEL_PATH} (meta: {META_PATH})")
    logger.info(f"=== обучение завершено за {meta['duration_s']} с ===")
    return 0


# ─── Демон ────────────────────────────────────────────────
@dataclass
class DaemonState:
    orig_fids: Dict[int, int] = field(default_factory=dict)
    orig_cpuidle: Dict[int, int] = field(default_factory=dict)
    cur_fid: int = FID_MAX
    up_streak: int = 0
    down_since: Optional[float] = None
    deep_since: Optional[float] = None
    last_up_ts: float = 0.0
    last_change_ts: float = 0.0
    downscale_postponed_until: float = 0.0
    running: bool = True
    cpuidle_available: bool = True
    _cycle_count: int = 0
    p_history: list = field(default_factory=list)
    floor_hits: int = 0


class PredictorDaemon:
    def __init__(self, dry_run: bool = False):
        self.log = setup_logging(DAEMON_LOG, "predictor")
        self.dry = dry_run
        self.state = DaemonState()
        self.model = None
        self.meta = {}
        self.buf: List[dict] = []
        self.buf_max = 900   # ≥ 5 мин для util_avg_5m + запас
        self._load_model()

    def _load_model(self) -> None:
        if not MODEL_PATH.exists():
            self.log.warning(f"нет модели {MODEL_PATH} — инференс будет 0.0 "
                             f"(запусти --train для обучения)")
            return
        try:
            with open(MODEL_PATH, "rb") as f:
                self.model = pickle.load(f)
            self.meta = json.loads(META_PATH.read_text()) if META_PATH.exists() else {}
            self.log.info(f"модель загружена, AUC={self.meta.get('auc', '?')}, "
                          f"обучена: {self.meta.get('trained_at', '?')}")
        except Exception as e:
            self.log.error(f"не удалось загрузить модель: {e}")
            syslog_err(f"load model: {e}")
            self.model = None

    def _read_current(self) -> Optional[dict]:
        """Берёт последнюю строку из metrics.csv — O(1), без чтения всего файла."""
        if not CSV_PATH.exists():
            return None
        try:
            with open(CSV_PATH, "rb") as f:
                f.seek(0, 2)              # конец файла
                size = f.tell()
                if size < 128:
                    return None
                back = min(size, 4096)    # последние 4 KB
                f.seek(size - back)
                data = f.read().decode("utf-8", errors="ignore")
            lines = [l for l in data.splitlines() if l.strip() and not l.startswith("ts,")]
            if not lines:
                return None
            last_line = lines[-1]
            cols = ["ts","epoch","cpu_util_pct","cpu_iowait_pct","cpu_user_pct",
                    "cpu_sys_pct","mem_used_pct","mem_available_kb","net_rx_bytes_s",
                    "net_tx_bytes_s","core_temp_max_c","loadavg_1m","ncpu"]
            vals = last_line.split(",")
            if len(vals) != len(cols):
                self.log.debug(f"read tail: cols mismatch {len(vals)} vs {len(cols)}")
                return None
            d = dict(zip(cols, vals))
            # ВАЖНО: convert to numeric — иначе _featurize падает с TypeError
            try:
                for k in ("epoch","cpu_util_pct","cpu_iowait_pct","cpu_user_pct",
                          "cpu_sys_pct","mem_used_pct","net_rx_bytes_s","net_tx_bytes_s",
                          "core_temp_max_c","loadavg_1m"):
                    if k in d and d[k] != "":
                        d[k] = float(d[k])
                for k in ("mem_available_kb","ncpu"):
                    if k in d and d[k] != "":
                        d[k] = int(d[k])
            except (ValueError, TypeError) as e:
                self.log.debug(f"read tail: numeric convert fail: {e}")
                return None
            return d
        except Exception as e:
            self.log.debug(f"read tail fail: {e}")
            return None

    def _build_infer_row(self, row: dict) -> Optional[pd.DataFrame]:
        # дополняем историю для лагов
        self.buf.append(row)
        if len(self.buf) > self.buf_max:
            self.buf = self.buf[-self.buf_max:]
        if len(self.buf) < max(LAG_SECONDS) + 1:
            return None
        df = pd.DataFrame(self.buf)
        df["ts"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        df = _featurize(df)
        last = df.iloc[[-1]]
        return last[FEATURES].astype(float)

    def predict(self, X: pd.DataFrame) -> Optional[float]:
        """Возвращает вероятность пика или None, если модели нет."""
        if self.model is None:
            return None
        try:
            return float(self.model.predict_proba(X)[0, 1])
        except Exception as e:
            self.log.error(f"predict fail: {e}")
            return None

    def _save_state(self) -> None:
        self.state.orig_fids = read_all_fids()
        self.state.orig_cpuidle = read_cpuidle_disable()
        self.log.info(f"исходные FID: {self.state.orig_fids}")
        self.log.info(f"исходные cpuidle disable: {self.state.orig_cpuidle}")

    def _restore_state(self) -> None:
        self.log.info("восстановление исходного состояния...")
        for c, f in self.state.orig_fids.items():
            try: set_fid(c, f)
            except Exception as e: self.log.warning(f"restore FID cpu{c}: {e}")
        if self.state.cpuidle_available:
            for c, v in self.state.orig_cpuidle.items():
                p = _find_c1_state(c)
                if p:
                    try: p.write_text(str(v))
                    except OSError as e: self.log.warning(f"restore cpuidle cpu{c}: {e}")
        self.log.info("восстановление завершено")

    def _apply_fid(self, fid: int) -> None:
        if self.dry:
            self.log.info(f"[dry] set_fid({fid})")
            self.state.cur_fid = fid
            return
        try:
            write_all_fids(fid)
            self.state.cur_fid = fid
            self.state.last_change_ts = time.time()
            self.log.info(f"FID → {fid} ({fid*133.33:.0f} МГц)")
        except Exception as e:
            self.log.error(f"set_fid({fid}) fail: {e}")
            syslog_err(f"set_fid fail: {e}")

    def _apply_cpuidle(self, c1_disable: int) -> None:
        if self.dry: return
        if not self.state.cpuidle_available:
            return
        try:
            ok = set_cpuidle_disable(c1_disable)
            if ok:
                self.log.info(f"cpuidle C1 disable={c1_disable}")
            else:
                self.log.warning("cpuidle недоступен — отключаем в этом сеансе")
                self.state.cpuidle_available = False
        except Exception as e:
            self.log.warning(f"cpuidle fail: {e}")
            self.state.cpuidle_available = False

    def _apply_nice(self, boost: bool) -> None:
        if self.dry: return
        pids = _pids_by_name(NICE_TARGETS)
        val = NICE_BOOST if boost else NICE_NORMAL
        for pid in pids:
            set_nice(pid, val)
        if pids:
            self.log.info(f"nice({val}) для {len(pids)} процессов")

    def _decide(self, p: float, load1: float, util_now: float = 0.0) -> None:
        """Логика переключения FID с гистерезисом и anti-stutter."""
        now = time.time()
        # FLOOR: реальная нагрузка — всегда FID_MAX, модель игнорируется
        # Порог 2.0 (не 1.0): load1=1.0 при 8 CPU = 12.5% — это фон (NFS/Samba/Firefox),
        # на 2133 МГц фон работает без лагов. FLOOR нужен только при реальной работе.
        if load1 >= 2.0:
            self.state.floor_hits += 1
            if self.state.floor_hits % 12 == 1:
                self.log.info(f"FLOOR active (hits={self.state.floor_hits}, load1={load1:.2f})")
            if self.state.cur_fid != FID_MAX:
                self._apply_fid(FID_MAX)
                self.log.info(f"FLOOR load1={load1:.2f} >= 1.0 -> FID_MAX")
            self.state.up_streak = 0
            self.state.down_since = None
            self.state.deep_since = None
            return

        # p_history: сглаживание за 6 циклов (30 с)
        self.state.p_history.append(p)
        if len(self.state.p_history) > 6:
            self.state.p_history = self.state.p_history[-6:]
        p_avg = sum(self.state.p_history) / max(len(self.state.p_history), 1)

        # UPScale
        if p >= P_UP:
            self.state.up_streak += 1
            if self.state.up_streak >= UP_CYCLES and self.state.cur_fid != FID_MAX:
                self._apply_fid(FID_MAX)
                self._apply_cpuidle(1)
                self.log.info(f"UPSCALE p={p:.2f} (FID {FID_MAX})")
            self.state.down_since = None
            self.state.deep_since = None
            return
        else:
            self.state.up_streak = 0

        # Anti-stutter: откладываем снижение при высокой loadavg
        if load1 > ANTI_STUTTER_LOAD:
            if now < self.state.downscale_postponed_until:
                return
            self.state.downscale_postponed_until = now + ANTI_STUTTER_DELAY
            self.log.info(f"снижение отложено: loadavg={load1:.2f} > {ANTI_STUTTER_LOAD:.2f}")

        # Downscale по сглаженному p_avg (а не по мгновенному p)
        if p_avg <= P_DOWN and load1 < 2.0:
            if self.state.down_since is None:
                self.state.down_since = now
            elif now - self.state.down_since >= DOWN_SECONDS and self.state.cur_fid > FID_DOWN:
                self._apply_fid(FID_DOWN)
                self._apply_cpuidle(0)
                self.log.info(f"DOWNSCALE p_avg={p_avg:.3f} (FID {FID_DOWN})")
                self.state.down_since = None
        else:
            self.state.down_since = None

        # Deep idle
        if p <= P_DEEP:
            if self.state.deep_since is None:
                self.state.deep_since = now
            elif now - self.state.deep_since >= DEEP_SECONDS and self.state.cur_fid > FID_DEEP:
                self._apply_fid(FID_DEEP)
                self.log.info(f"DEEP IDLE p={p:.2f} (FID {FID_DEEP})")
                self.state.deep_since = None
        else:
            self.state.deep_since = None

    def run(self) -> int:
        self.log.info(f"=== демон запущен (dry={self.dry}) ===")
        self._save_state()
        # при старте — на максимум, чтобы не тормозить
        self._apply_fid(FID_MAX)
        self._apply_cpuidle(0)
        self.log.info(f"пороги: P_UP={P_UP} P_DOWN={P_DOWN} P_DEEP={P_DEEP} peak={PEAK_THRESHOLD}")

        def _stop(signum, frame):
            self.log.info(f"сигнал {signum}")
            self.state.running = False
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGHUP, _stop)

        try:
            while self.state.running:
                t0 = time.monotonic()
                try:
                    row = self._read_current()
                    if row is None:
                        time.sleep(1.0); continue
                    X = self._build_infer_row(row)
                    if X is None:
                        time.sleep(1.0); continue
                    p = self.predict(X)
                    load1 = float(row.get("loadavg_1m", 0.0))
                    self.state._cycle_count += 1
                    if p is None:
                        if self.state.cur_fid != FID_MAX:
                            self.log.info("модели нет — принудительно FID_MAX")
                            self._apply_fid(FID_MAX)
                        self.log.debug(f"p=None load1={load1:.2f}")
                    else:
                        if self.state._cycle_count % 12 == 1:
                            self.log.info(
                                f"p={p:.3f} load1={load1:.2f} fid={self.state.cur_fid}")
                        self._decide(p, load1, float(row.get("cpu_util_pct", 0.0)))
                except Exception as e:
                    # Fail-Safe
                    self.log.exception(f"критическая ошибка в цикле: {e}")
                    syslog_err(f"predictor cycle: {e}")
                    raise
                dt = LOOP_INTERVAL - (time.monotonic() - t0)
                if dt > 0:
                    time.sleep(dt)
        except Exception:
            self._restore_state()
            return 1
        finally:
            self._restore_state()
            self.log.info("демон остановлен")
        return 0

    def status(self) -> int:
        print("=== predictive-governor status ===")
        print(f"модель: {MODEL_PATH} (exists={MODEL_PATH.exists()})")
        if META_PATH.exists():
            print(META_PATH.read_text())
        print("--- текущие FID ---")
        for c, f in read_all_fids().items():
            print(f"cpu{c}: FID={f} ({f*133.33:.0f} МГц)")
        print("--- cpuidle C1 disable ---")
        print(read_cpuidle_disable())
        return 0


# ─── CLI ──────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="predictive-governor predictor")
    ap.add_argument("--train", action="store_true", help="обучить модель")
    ap.add_argument("--dry-run", action="store_true", help="без записи в MSR")
    ap.add_argument("--status", action="store_true", help="текущее состояние")
    args = ap.parse_args()

    if args.train:
        return train_model()
    if args.status:
        return PredictorDaemon(dry_run=True).status()
    return PredictorDaemon(dry_run=args.dry_run).run()


if __name__ == "__main__":
    sys.exit(main())
