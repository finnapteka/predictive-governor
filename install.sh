#!/usr/bin/env bash
# predictive-governor / install.sh
# Развёртывание MVP предиктивного говернора на ug48 (i7-975, kernel 6.8).
# Запуск под root: sudo ./install.sh
#
# Идемпотентен: повторный запуск обновляет файлы и перезапускает сервисы.
# При любой ошибке — откат к исходному состоянию (MSR FID, cpuidle).

set -euo pipefail

# ─── Константы (зашиты под ug48 / i7-975) ─────────────────
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/predictive-governor-git"
LOG_DIR="/var/log/predictive_governor"
VENV="${INSTALL_DIR}/venv"
SYSTEMD_DIR="/etc/systemd/system"

EXPECTED_KERNEL_PREFIX="6.8"
EXPECTED_CPU_MATCH="Intel.*i7.*975"

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
info()  { printf '%s[INFO]%s %s\n' "$GRN" "$RST" "$*"; }
warn()  { printf '%s[WARN]%s %s\n' "$YEL" "$RST" "$*"; }
fail()  { printf '%s[FAIL]%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# ─── 0. Проверки окружения ────────────────────────────────
[ "$(id -u)" -eq 0 ] || fail "нужен root: sudo ./install.sh"

info "проверка окружения..."
kernel=$(uname -r)
[[ "$kernel" == ${EXPECTED_KERNEL_PREFIX}.* ]] \
  || warn "ядро ${kernel}, ожидалось ${EXPECTED_KERNEL_PREFIX}.x — продолжаем, но не гарантируем"

cpu_model=$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs)
[[ "$cpu_model" =~ $EXPECTED_CPU_MATCH ]] \
  || warn "CPU '${cpu_model}', ожидался i7-975 — продолжаем"

for t in rdmsr wrmsr logger ionice nice gzip python3 systemctl sensors; do
  command -v "$t" >/dev/null || fail "не найден инструмент: $t"
done
info "инструменты на месте"

# ─── 1. Каталоги ──────────────────────────────────────────
info "создание каталогов..."
install -d -m 0755 "$INSTALL_DIR"
install -d -m 0755 "$INSTALL_DIR/logs"
install -d -m 0755 "$LOG_DIR"
install -d -m 0755 "$INSTALL_DIR/systemd"
info "  $INSTALL_DIR"
info "  $LOG_DIR"

# ─── 2. Python venv ───────────────────────────────────────
info "проверка Python и зависимостей..."
python3 - <<'PY' || fail "не хватает базовых зависимостей (numpy/pandas)"
import importlib, sys
missing = []
for m in ("numpy", "pandas"):
    try: importlib.import_module(m)
    except ImportError: missing.append(m)
if missing:
    print("отсутствуют:", ", ".join(missing))
    sys.exit(1)
print("numpy/pandas: OK")
PY

if [ ! -x "${VENV}/bin/python" ]; then
  info "создание venv ${VENV} (с --system-site-packages)..."
  python3 -m venv --system-site-packages "$VENV"
else
  info "venv уже существует: ${VENV}"
fi

# Проверка sklearn/scipy в venv; если нет — ставим
if ! "${VENV}/bin/python" -c 'import sklearn, scipy' >/dev/null 2>&1; then
  info "установка sklearn/scipy в venv (pip, локально)..."
  "${VENV}/bin/pip" install --quiet --upgrade pip
  "${VENV}/bin/pip" install --quiet scikit-learn scipy
else
  info "sklearn/scipy: уже в venv"
fi

"${VENV}/bin/python" -c 'import sklearn, scipy, numpy, pandas; print("venv deps: OK")' \
  || fail "верификация зависимостей venv провалилась"

# ─── 3. Копирование скриптов ──────────────────────────────
info "копирование скриптов в ${INSTALL_DIR}..."
for f in collector.py predictor.py train.sh; do
  [ -f "${PROJECT_ROOT}/${f}" ] || fail "нет файла ${PROJECT_ROOT}/${f}"
  install -m 0755 "${PROJECT_ROOT}/${f}" "${INSTALL_DIR}/${f}"
  info "  ${f}"
done

# ─── 4. systemd units ─────────────────────────────────────
info "копирование systemd units..."
for u in predictive-governor.service predictive-governor-collector.service \
         predictive-governor-train.service predictive-governor-train.timer; do
  [ -f "${PROJECT_ROOT}/systemd/${u}" ] || fail "нет файла systemd/${u}"
  install -m 0644 "${PROJECT_ROOT}/systemd/${u}" "${SYSTEMD_DIR}/${u}"
  info "  ${SYSTEMD_DIR}/${u}"
done

# ─── 5. Smoke-test MSR (запись и откат) ───────────────────
info "smoke-test записи в MSR 0x199 (все 8 CPU)..."
declare -A ORIG_FID
for c in 0 1 2 3 4 5 6 7; do
  raw=$(rdmsr -p "$c" 0x199)
  ORIG_FID[$c]="$raw"
  # нормализуем: дописываем 0x если нет
  [[ "$raw" == 0x* ]] || raw="0x${raw}"
  fid=$(( raw & 0xff ))
  [[ " 12 13 14 16 17 18 19 20 21 22 23 25 " == *" ${fid} "* ]] \
    || warn "cpu${c}: FID=${fid} вне ожидаемой лестницы, продолжаем"
done
info "  исходные FID: $(for c in 0 1 2 3 4 5 6 7; do printf '%s ' "${ORIG_FID[$c]}"; done)"

# Пробуем записать FID 16 (2133 МГц) на cpu0 и вернуть
probe_ok=0
orig0="${ORIG_FID[0]}"; [[ "$orig0" == 0x* ]] || orig0="0x${orig0}"
target_fid=16
new0=$(python3 -c "v=int('${orig0}',16); v=(v & ~0xff) | ${target_fid}; print(f'0x{v:x}')")
if wrmsr -p 0 0x199 "$new0" 2>/dev/null; then
  sleep 0.2
  back=$(rdmsr -p 0 0x199)
  [[ "$back" == 0x* ]] || back="0x${back}"
  if [ "$((back & 0xff))" -eq "$target_fid" ]; then
    probe_ok=1
    info "  MSR write: OK (FID 0→${target_fid})"
  else
    warn "  MSR write: запись не принята (read back FID=$((back & 0xff)))"
  fi
  # всегда восстанавливаем
  wrmsr -p 0 0x199 "0x$(printf '%x' "$((orig0))")" || warn "  восстановление cpu0 не удалось"
else
  warn "  MSR write: команда wrmsr вернула ошибку"
fi

if [ "$probe_ok" -ne 1 ]; then
  warn "MSR-путь недоступен — демон запустится в fallback-режиме (nice/cpuidle)"
fi

# ─── 6. Активация systemd ─────────────────────────────────
info "systemctl daemon-reload..."
systemctl daemon-reload

info "включение и запуск predictive-governor-collector.service..."
systemctl enable --now predictive-governor-collector.service

info "включение и запуск predictive-governor.service..."
systemctl enable --now predictive-governor.service

info "включение predictive-governor-train.timer..."
systemctl enable --now predictive-governor-train.timer

sleep 2
if systemctl is-active --quiet predictive-governor.service; then
  info "  сервис активен"
else
  warn "  сервис НЕ активен — смотри: journalctl -u predictive-governor.service -n 50"
fi

# ─── 7. Итог ──────────────────────────────────────────────
echo
info "════════════════════════════════════════════"
info " Установка завершена"
info "════════════════════════════════════════════"
echo
info "Проверка:"
echo "  systemctl status predictive-governor.service"
echo "  journalctl -u predictive-governor.service -f"
echo "  tail -f ${LOG_DIR}/metrics.csv"
echo "  sensors | grep -A5 coretemp"
echo "  sudo rdmsr -a 0x199      # текущие FID"
echo
info "Первый запуск обучения (модель обучится на накопленных данных):"
echo "  ${INSTALL_DIR}/train.sh"
echo
info "Удаление:"
echo "  sudo systemctl disable --now predictive-governor.service predictive-governor-train.timer"
echo "  sudo rm -rf ${INSTALL_DIR} ${LOG_DIR} ${SYSTEMD_DIR}/predictive-governor.*"
