#!/usr/bin/env bash
# predictive-governor / train.sh
# Обёртка обучения модели в фоне с минимальным приоритетом.
# Используется в predictive-governor-train.timer, либо вручную:
#     sudo /opt/predictive-governor-git/train.sh
#
# Все параметры и пути захардкожены под ug48 / i7-975.

set -euo pipefail

INSTALL_DIR="/opt/predictive-governor-git"
VENV="${INSTALL_DIR}/venv"
LOG_DIR="/var/log/predictive_governor"
TRAIN_LOG="${LOG_DIR}/train.log"
PY="${VENV}/bin/python"
PREDICTOR="${INSTALL_DIR}/predictor.py"

# Минимальный приоритет CPU + idle I/O (не мешаем основной работе)
NICE_LEVEL=19
IONICE_CLASS=3   # idle
MIN_ROWS=1800   # ~30 минут по 1 Гц

mkdir -p "$LOG_DIR"

# Проверка, что venv и predictor на месте
if [ ! -x "$PY" ]; then
  echo "[train] ОШИБКА: нет интерпретатора $PY" | tee -a "$TRAIN_LOG" >&2
  logger -t predictive-governor-train -p err "no venv python at $PY"
  exit 2
fi
if [ ! -f "$PREDICTOR" ]; then
  echo "[train] ОШИБКА: нет $PREDICTOR" | tee -a "$TRAIN_LOG" >&2
  logger -t predictive-governor-train -p err "no predictor at $PREDICTOR"
  exit 2
fi

# Метка в логе
{
  echo
  echo "════════════════════════════════════════════════════════════"
  echo "[train] начало: $(date -Is)"
  echo "[train] пользователь: $(id -un) (uid=$(id -u))"
  echo "[train] nice=${NICE_LEVEL} ionice=${IONICE_CLASS}"
} >> "$TRAIN_LOG"

# Запуск под nice + ionice, с таймаутом 15 мин (защита от зависания)
TIMEOUT_SEC=900
t0=$(date +%s)
rc=0
timeout "${TIMEOUT_SEC}" nice -n "$NICE_LEVEL" ionice -c "$IONICE_CLASS" \
    "$PY" "$PREDICTOR" --train >> "$TRAIN_LOG" 2>&1 || rc=$?
t1=$(date +%s)
dur=$((t1 - t0))

if [ "$rc" -eq 0 ]; then
  echo "[train] OK (${dur}s)" >> "$TRAIN_LOG"
  logger -t predictive-governor-train -p info "training OK in ${dur}s"
elif [ "$rc" -eq 124 ]; then
  echo "[train] TIMEOUT (${dur}s > ${TIMEOUT_SEC}s)" >> "$TRAIN_LOG"
  logger -t predictive-governor-train -p err "training timed out after ${dur}s"
else
  echo "[train] FAILED rc=${rc} (${dur}s)" >> "$TRAIN_LOG"
  logger -t predictive-governor-train -p err "training failed rc=${rc} after ${dur}s"
fi

echo "[train] конец: $(date -Is)" >> "$TRAIN_LOG"
exit "$rc"
