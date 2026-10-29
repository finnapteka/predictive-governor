#!/usr/bin/env bash
# predictive-governor / cli/verify.sh  (v2 — исправлена раскладка FID)
# Запуск под root. Лог: ./logs/verify_YYYYMMDD_HHMMSS.log

set -u
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${PROJECT_ROOT}/logs"; mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/verify_$(date +%Y%m%d_%H%M%S).log"

sec()  { printf '\n===== %s =====\n' "$1" >>"$LOG"; }
runs() { sec "$1"; { bash -c "$2" ; } >>"$LOG" 2>&1 || echo "[exit=$?] $2" >>"$LOG"; }
note() { printf '# %s\n' "$1" >>"$LOG"; }

: >"$LOG"
cat >>"$LOG" <<EOF
predictive-governor verify (v2)
timestamp : $(date -Is)
host      : $(hostname)
kernel    : $(uname -r)
project   : ${PROJECT_ROOT}
EOF

[ "$(id -u)" -eq 0 ] || { echo "ОШИБКА: нужен root" | tee -a "$LOG"; exit 1; }

# ---------- 0. инструменты ----------
runs "0.1 наличие инструментов" \
  'for t in rdmsr wrmsr cpupower sensors turbostat logger ionice nice gzip python3; do
     printf "%-10s " "$t"; command -v "$t" || echo MISSING; done'

# ---------- 1. cpufreq (ожидаемо отсутствует) ----------
note "cpufreq на этом хосте отсутствует; фиксируем для протокола."
runs "1.1 наличие cpufreq sysfs" \
  'ls -ld /sys/devices/system/cpu/cpu0/cpufreq 2>&1; echo "---"; cpupower frequency-info 2>&1 | head -n 15'

# ---------- 2. MSR: чтение и декодирование (верная раскладка) ----------
note "IA32_PERF_CTL (0x199): биты 0-7 = FID (множитель), биты 8-15 = VID."
note "i7-975: лестница FID 12,13,14,16,17,18,19,20,21,22,23,25 при BCLK 133.33 МГц."
note "IA32_MISC_ENABLE (0x1a0): бит 38 = Turbo Mode Disable (не трогаем)."

runs "2.1 rdmsr 0x199 по всем CPU" 'rdmsr -a 0x199 2>&1'
runs "2.2 декодирование FID (младший байт)" \
  'python3 - <<PY 2>&1
import subprocess
out = subprocess.check_output(["rdmsr","-a","0x199"], text=True)
BCLK = 133.33
ladder = {12,13,14,16,17,18,19,20,21,22,23,25}
for line in out.splitlines():
    try: v = int(line.strip(), 16)
    except ValueError: continue
    fid = v & 0xff
    vid = (v >> 8) & 0xff
    ok = "OK" if fid in ladder else "НЕВАЛИД"
    print(f"raw=0x{v:04x}  FID={fid:2d}  VID={vid:2d}  ~{fid*BCLK:7.0f} МГц  [{ok}]")
PY'
runs "2.3 rdmsr 0x1a0 (бит турбо)" 'rdmsr -a 0x1a0 2>&1'
runs "2.4 декодирование турбо" \
  'python3 - <<PY 2>&1
import subprocess
v = int(subprocess.check_output(["rdmsr","-a","0x1a0"], text=True).split()[0], 16)
print(f"0x{v:016x}  turbo_disable(бит38)={(v>>38)&1}")
PY'

# ---------- 3. WRMSR-проба (исправлено) ----------
note "Проба записи: сохранить 0x199 всех CPU, выставить CPU0 FID=16 (2133 МГц), проверить, восстановить все."
runs "3.1 WRMSR-проба cpu0 (сохранить/проверить/восстановить)" \
  'set -e
   ORIG=$(rdmsr -p 0 0x199)                       # например 0x19
   NEW=$(python3 -c "v=int(\"${ORIG}\",16); v=(v & ~0xff) | 16; print(f\"0x{v:x}\")")
   echo "orig=${ORIG} target=${NEW}"
   wrmsr -p 0 0x199 "${NEW}" && echo "запись: ok" || echo "запись: ОШИБКА"
   MID=$(rdmsr -p 0 0x199)
   echo "чтение_после=${MID}"
   if [ "$MID" = "$NEW" ] || [ "$MID" = "$(printf '0x%x' $((16#${NEW#0x})))" ]; then
     echo "ПРИНЯТО: железо приняло запись"
   else
     echo "ОТКЛОНЕНО: железо проигнорировало запись (BIOS lock?)"
   fi
   wrmsr -p 0 0x199 "${ORIG}" && echo "восстановление: ok" || echo "восстановление: ОШИБКА"
   FINAL=$(rdmsr -p 0 0x199)
   echo "финал=${FINAL}"
   if [ "$FINAL" = "$ORIG" ]; then echo "ВОССТАНОВЛЕНИЕ OK"; else echo "НЕСОВПАДЕНИЕ ВОССТАНОВЛЕНИЯ"; fi'

# ---------- 4. coretemp ----------
runs "4.1 путь coretemp" \
  'for h in /sys/class/hwmon/hwmon*; do
     [ "$(cat $h/name 2>/dev/null)" = coretemp ] && echo "$h" && \
       for t in $h/temp*_input; do printf "%s=%s мК\n" "$t" "$(cat $t)"; done
   done'

# ---------- 5. cpuidle ----------
runs "5.1 состояния cpuidle cpu0" \
  'for s in /sys/devices/system/cpu/cpu0/cpuidle/state*/; do
     printf "%s name=%-5s disable=%s latency=%s\n" "$s" \
       "$(cat $s/name 2>/dev/null)" "$(cat $s/disable 2>/dev/null)" "$(cat $s/latency 2>/dev/null)"
   done'
runs "5.2 cpupower idle-info" 'cpupower idle-info --silent 2>&1 | head -n 15'

# ---------- 6. базовый снимок ----------
runs "6.1 load/uptime" 'cat /proc/loadavg; uptime'
runs "6.2 cmdline" 'cat /proc/cmdline'

# ---------- 7. сводка ----------
sec "7. СВОДКА"
{
  echo "cpufreq_sysfs: $([ -d /sys/devices/system/cpu/cpu0/cpufreq ] && echo present || echo absent)"
  echo "rdmsr_works:  $(rdmsr -a 0x199 >/dev/null 2>&1 && echo yes || echo no)"
  echo "wrmsr_accepted: $(grep -q 'ПРИНЯТО' "$LOG" && echo yes || echo no)"
  echo "wrmsr_restored: $(grep -q 'ВОССТАНОВЛЕНИЕ OK' "$LOG" && echo yes || echo no)"
  echo "coretemp:     $(for h in /sys/class/hwmon/hwmon*; do [ "$(cat $h/name 2>/dev/null)" = coretemp ] && echo "$h"; done)"
  echo "cpuidle:      $(for s in /sys/devices/system/cpu/cpu0/cpuidle/state*/name; do cat $s 2>/dev/null; done | tr '\n' ',')"
} >>"$LOG" 2>&1

echo "ЛОГ: ${LOG}"
