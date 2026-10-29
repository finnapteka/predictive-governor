# predictive-governor

Предиктивный демон управления частотой CPU для Linux (Ubuntu/Debian / Linux Mint),
рассчитанный на системы **без рабочего cpufreq-драйвера**, где частота управляется
напрямую через MSR-регистры (`IA32_PERF_CTL` / `0x199`).

Цель — снизить энергопотребление и тепловыделение в простое, не теряя отзывчивости
под нагрузкой: демон прогнозирует пик за 5–30 секунд вперёд и заранее поднимает
частоту; при устойчивом затишье — плавно опускает.

---

## Целевая платформа (проверено на этой машине)

| Компонент | Значение |
|---|---|
| Хост | `ug48.localdomain` |
| ОС | Linux Mint 21.3 (Ubuntu 22.04 base) |
| Ядро | `6.8.0-138-generic` |
| CPU | Intel Core i7-975 (Bloomfield, X58), 4 ядра / 8 потоков |
| BCLK | 133.33 МГц |
| cpufreq | **отсутствует** (`acpi-cpufreq` не биндится) |
| Турбо | отключён BIOS (`IA32_MISC_ENABLE` bit 38 = 1) |
| C-states | прижаты `processor.max_cstate=1 intel_idle.max_cstate=1` |

**Ключевое ограничение:** стандартный путь `/sys/devices/system/cpu/*/cpufreq/`
на этой машине недоступен. Управление частотой идёт через MSR. Скрипт `cli/verify.sh`
фиксирует это состояние и валидирует MSR-путь.

---

## Архитектура
┌─────────────────────────────┐
collector.py ───▶│ metrics.csv (+ gzip, 14 дн.)│
1 Гц └──────────────┬──────────────┘
│ ежедневно 03:30
▼
┌─────────────────────────────┐
│ predictor.py --train │
│ HistGradientBoosting │
│ nice -n 19 ionice -c3 │
└──────────────┬──────────────┘
│ model.pkl
▼
┌─────────────────────────────┐
predictor.py ───▶│ каждые 5 с: predict_proba │
(демон) │ p_peak ∈ [0.0, 1.0] │
└──────────────┬──────────────┘
│
┌─────────────────┼─────────────────┐
▼ ▼ ▼
backend=msr backend=cpuidle backend=nice
(основной) (вспомогательный) (fallback)
wrmsr 0x199 disable C1 renice Firefox/
FID 12..25 Telegram при пике


**Логика решений:**
- `p_peak ≥ 0.75` (3 цикла подряд) → **upscale**: FID 25 (3333 МГц) + запрет C1.
- `p_peak ≤ 0.15` (15 с подряд) → **downscale**: FID 16 (2133 МГц) + разрешение C1.
- `p_peak ≤ 0.05` (> 5 мин) → **deep idle**: FID 12 (1600 МГц).
- Между — гистерезис (deadband = 1 FID).
- Anti-stutter: при `loadavg_1m > 0.7 × ncpu` снижение откладывается на 30 с.
- Fail-Safe: любое исключение → восстановить все 8 FID + cpuidle + `logger -p err` + `exit 1`.

---

## Лестница FID (i7-975)

Множители, валидные для Bloomfield при BCLK 133.33 МГц:

| FID | Частота |
|-----|---------|
| 12  | 1600 МГц |
| 13  | 1733 МГц |
| 14  | 1866 МГц |
| 16  | 2133 МГц |
| 17  | 2266 МГц |
| 18  | 2400 МГц |
| 19  | 2533 МГц |
| 20  | 2666 МГц |
| 21  | 2800 МГц |
| 22  | 2933 МГц |
| 23  | 3066 МГц |
| 25  | 3333 МГц |

**Формат MSR 0x199:** биты 0–7 = FID, биты 8–15 = VID.
**Критично:** `wrmsr` требует `0x`-префикс (`0x19`, не `19`) — иначе аргумент
интерпретируется как десятичное число.

---

## Состав проекта

| Файл | Назначение |
|---|---|
| `collector.py` | Сборщик метрик 1 Гц (CSV + gzip-ротация 14 дней) |
| `predictor.py` | Обучение модели (`--train`) и демон-инференс |
| `train.sh` | Обёртка обучения под `nice -n 19 ionice -c3` |
| `systemd/predictive-governor.service` | Unit демона |
| `systemd/predictive-governor-train.timer` | Таймер ежедневного обучения |
| `cli/verify.sh` | Диагностика состояния (cpufreq / MSR / temps / cpuidle) |
| `install.sh` | Развёртывание MVP |

---

## Метрики, которые собираются

- `/proc/stat` → `cpu_util_pct` (среднее по 8 логическим CPU), `cpu_iowait_pct`
- `/proc/meminfo` → `mem_used_pct`, `mem_available_kb`
- `/proc/net/dev` → `net_rx_bytes_s`, `net_tx_bytes_s` (дельты)
- `/sys/class/hwmon/hwmon1/temp2..5_input` → `core_temp_max_c`
- Время: `hour`, `minute`, `dow`, `is_weekend`, `sin_hour`, `cos_hour`

Модель учится по этим признакам + лагам (1, 5, 15, 60 с) предсказывать
бинарный таргет «будет ли `cpu_util_pct ≥ 60%` в окне [t+5, t+30]».

---

## Установка

```bash
git clone <repo> predictive-governor
cd predictive-governor
sudo ./install.sh

install.sh:

    создаёт /opt/predictive_governor/ и /var/log/predictive_governor/;

    создаёт venv /opt/predictive_governor/venv с --system-site-packages
    (numpy/pandas из системы, sklearn/scipy доустанавливаются в venv);

    копирует скрипты и unit-файлы;

    делает smoke-test записи в MSR с немедленным откатом;

    systemctl daemon-reload && enable --now predictive-governor.service predictive-governor-train.timer

Проверка

# текущий FID всех CPU
for c in 0 1 2 3 4 5 6 7; do printf "cpu%d: " $c; sudo rdmsr -p $c 0x199; done

# температуры
sensors | grep -A5 coretemp

# статус демона
systemctl status predictive-governor.service
journalctl -u predictive-governor.service -f

# лог обучения
tail -f /var/log/predictive_governor/train.log

Удаление

sudo systemctl disable --now predictive-governor.service predictive-governor-train.timer
sudo rm -rf /opt/predictive_governor /var/log/predictive_governor
sudo rm /etc/systemd/system/predictive-governor.*

Ограничения MVP

    Нет управления C-states глубже C1 (max_cstate=1 в cmdline).

    Турбо не разблокируется (риск нестабильности на X58).

    Модель — HistGradientBoostingClassifier с дефолтными гиперпараметрами.

    Пока нет sched_setaffinity (в планах v2).

    Обучение на 14 днях метрик; при меньшем объёме — фолбэк на простое сглаживание.
