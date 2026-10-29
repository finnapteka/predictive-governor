# predictive-governor

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![systemd](https://img.shields.io/badge/systemd-ready-green.svg)](https://systemd.io/)

**Predictive CPU frequency governor for Linux.** Uses a machine learning model to predict CPU load peaks 5–30 seconds in advance and scales frequency accordingly via direct MSR writes (`IA32_PERF_CTL` / `0x199`).

Designed for systems where standard `cpufreq` drivers do not bind (e.g., older Intel X58/Bloomfield platforms). Reduces power consumption and heat in idle states while maintaining responsiveness under load.

> Русская версия: [README.ru.md](README.ru.md)

## Verified Hardware

| Component | Value |
|---|---|
| Host | `ug48.localdomain` |
| OS | Linux Mint 21.3 (Ubuntu 22.04 base) |
| Kernel | `6.8.0-138-generic` |
| CPU | Intel Core i7-975 (Bloomfield, X58), 4C/8T |
| BCLK | 133.33 MHz |
| cpufreq | **Not available** (`acpi-cpufreq` fails to bind) |
| Turbo | Disabled in BIOS |
| C-states | Limited (`processor.max_cstate=1`) |

## Architecture

collector.py ───▶ metrics.csv (1 Hz, gzip rotation 14 days)
│
▼ (daily 03:30)
predictor.py --train
(HistGradientBoosting)
│ model.pkl
▼
predictor.py ───▶ every 5s: predict_proba
(daemon) │ p_peak ∈ [0.0, 1.0]
│
┌───────────┼───────────┐
▼ ▼ ▼
backend=msr cpuidle nice
wrmsr 0x199 disable C1 renice Firefox
text


**Decision Logic:**
- `p_peak >= 0.75` (3 cycles) -> **upscale**: FID 25 (3.33 GHz) + disable C1.
- `p_peak <= 0.30` (15 sec) -> **downscale**: FID 16 (2.13 GHz) + allow C1.
- `p_peak <= 0.05` (5 min) -> **deep idle**: FID 12 (1.60 GHz).
- Anti-stutter: if `loadavg_1m > 0.7 * ncpu`, downscale is postponed for 30s.
- Fail-Safe: on exception -> restore all FIDs + cpuidle -> `logger -p err` -> `exit 1`.

## Features

- **ML-driven**: Predicts CPU peaks based on `/proc/stat`, `/proc/meminfo`, `/proc/net/dev`, and core temps.
- **Direct MSR control**: Bypasses missing cpufreq drivers.
- **Fail-Safe**: Guarantees restoring original FID and C1 state on crash.
- **Low overhead**: Collector runs at 1 Hz, daemon at 0.2 Hz.
- **Systemd integration**: Collector, daemon, and daily training timer.

## Installation

```bash
git clone https://github.com/finnapteka/predictive-governor.git
cd predictive-governor
sudo ./install.sh

The script creates a venv, installs dependencies, copies systemd units, and enables the services.
Configuration

The constants are hardcoded for the i7-975 (BCLK 133.33 MHz) in predictor.py:

    FID_LADDER = (12, 13, 14, 16, 17, 18, 19, 20, 21, 22, 23, 25)

    PEAK_THRESHOLD = 7.0 (utilization threshold for training)

    P_UP = 0.75, P_DOWN = 0.30, P_DEEP = 0.05

Diagnostics
bash

# Current FID (frequency ID)
for c in 0 1 2 3 4 5 6 7; do printf "cpu%d: " $c; sudo rdmsr -p $c 0x199; done

# Core temperatures
sensors | grep -A5 coretemp

# Daemon status
systemctl status predictive-governor.service
journalctl -u predictive-governor.service -f

# Training log
tail -f /var/log/predictive_governor/train.log

License

MIT — see LICENSE.
