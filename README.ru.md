# predictive-governor

Предиктивный демон управления частотой CPU для Linux. Использует модель машинного обучения для предсказания пиков нагрузки за 5–30 секунд и управляет частотой напрямую через MSR (IA32_PERF_CTL / 0x199).

Рассчитан на системы без рабочего cpufreq-драйвера (например, старые платформы Intel X58/Bloomfield). Снижает энергопотребление и нагрев в простое, сохраняя отзывчивость под нагрузкой.

    English version: README.md

Архитектура
text

collector.py ───▶ metrics.csv (1 Гц, gzip-ротация 14 дней)
                      │
                      ▼ (ежедневно в 03:30)
                 predictor.py --train
                 (HistGradientBoosting)
                      │ model.pkl
                      ▼
predictor.py ───▶ каждые 5 с: predict_proba
(демон)               │ p_peak ∈ [0.0, 1.0]
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
      backend=msr  cpuidle      nice
      wrmsr 0x199  disable C1   renice Firefox

Логика решений:

    p_peak >= 0.75 (3 цикла) -> upscale: FID 25 (3.33 ГГц) + запрет C1.

    p_peak <= 0.30 (15 с) -> downscale: FID 16 (2.13 ГГц) + разрешение C1.

    p_peak <= 0.05 (5 мин) -> deep idle: FID 12 (1.60 ГГц).

    Anti-stutter: при loadavg_1m > 0.7 * ncpu снижение откладывается на 30 с.

    Fail-Safe: при исключении -> восстановить все FID + cpuidle -> logger -p err -> exit 1.

Установка
bash

git clone https://github.com/finnapteka/predictive-governor.git
cd predictive-governor
sudo ./install.sh

Скрипт создаёт venv, ставит зависимости, копирует systemd-юниты и включает сервисы.
Метрики, которые собираются

    /proc/stat -> cpu_util_pct, cpu_iowait_pct

    /proc/meminfo -> mem_used_pct

    /proc/net/dev -> net_rx_bytes_s, net_tx_bytes_s

    /sys/class/hwmon -> core_temp_max_c

Модель предсказывает, будет ли cpu_util_pct >= 60% в окне [t+5, t+30].
Лицензия

MIT — см. LICENSE.
