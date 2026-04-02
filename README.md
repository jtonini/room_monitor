# Room Monitor

Environmental monitoring tools for Arachne's server room at the University of Richmond.

## gauge_monitor

Reads an analog UPPOD hygrometer/thermometer via a webcam and sends email alerts when temperature or humidity exceed safe thresholds.

**How it works:** Fetches a JPEG snapshot from an mjpg-streamer webcam, detects needle angles on the humidity and temperature dials using OpenCV (radial darkness sweep), maps angles to readings via a piecewise calibration table, logs to SQLite, and sends email alerts with cooldown.

### Requirements

- Python 3.9+ with `opencv-python-headless`, `numpy`, `tomli` (Python < 3.11)
- Webcam serving JPEG snapshots (mjpg-streamer or similar)
- Postfix or other local MTA for email alerts

### Quick Start

```bash
# Install dependencies
pip install opencv-python-headless numpy tomli

# Copy files
sudo mkdir -p /usr/local/lib/gauge_monitor /var/lib/gauge_monitor/snapshots
sudo cp gauge_monitor/gauge_monitor.py gauge_monitor/gauge_monitor.sql /usr/local/lib/gauge_monitor/
sudo cp gauge_monitor/gauge_monitor.toml.example /usr/local/etc/gauge_monitor.toml

# Edit config (webcam URL, thresholds, email addresses, gauge geometry)
sudo vi /usr/local/etc/gauge_monitor.toml

# Calibrate — verify needle detection and tick mark alignment
python3 /usr/local/lib/gauge_monitor/gauge_monitor.py -c /usr/local/etc/gauge_monitor.toml --calibrate

# Test a live check
python3 /usr/local/lib/gauge_monitor/gauge_monitor.py -c /usr/local/etc/gauge_monitor.toml --check

# Add to cron (every minute)
crontab -e
# */1 * * * * /opt/anaconda/bin/python3 /usr/local/lib/gauge_monitor/gauge_monitor.py -c /usr/local/etc/gauge_monitor.toml 2>&1 | logger -t gauge_monitor
```

### Usage

```
gauge_monitor.py [-c CONFIG] [--check | --calibrate | --history] [--image FILE] [--rows N]

  --check       Run a monitoring check (default)
  --calibrate   Show detected needles overlaid on image for visual verification
  --history     Show recent readings from database
  --image FILE  Use a local image file instead of fetching live (for calibration)
  --rows N      Number of history rows to show (default: 20)
```

### Calibration

The gauge geometry is defined in the `[gauge]` section of the TOML config. The humidity dial uses a piecewise calibration table (list of `[value, angle]` pairs) for accuracy across the non-linear scale. Temperature uses a linear mapping.

Run `--calibrate` to generate a debug overlay image (`gauge_calibration.png`) showing the detected needle (red line), center point (green dot), and reference tick marks (cyan/yellow). Adjust config values until the overlay aligns with the printed numbers on the gauge.

### Data

Readings are stored in SQLite at `/var/lib/gauge_monitor/gauge_monitor.db`. Query trends directly:

```bash
# Hourly averages for the past 24 hours
sqlite3 /var/lib/gauge_monitor/gauge_monitor.db \
  "SELECT strftime('%Y-%m-%d %H:00', timestamp) as hour,
   ROUND(AVG(humidity_pct),1) as avg_rh,
   ROUND(AVG(temperature_f),1) as avg_f
   FROM readings
   WHERE timestamp > datetime('now', '-24 hours', 'localtime')
   GROUP BY hour ORDER BY hour;"
```

## sound_monitor (planned)

Fan noise and SPL monitoring via microphone — spectrum analysis for detecting degraded PSU fans and other mechanical issues.
