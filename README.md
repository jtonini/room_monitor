# Room Monitor

Environmental monitoring tools for Arachne's server room at the University of Richmond.

## Components

### gauge_monitor
Reads an analog UPPOD hygrometer/thermometer via a webcam (mjpg-streamer) and sends email alerts when temperature or humidity exceed safe thresholds. Uses OpenCV to detect needle angles via radial darkness sweep, with piecewise calibration for the non-linear humidity scale. Logs every minute to SQLite.

### node_temps
Collects CPU temperatures (via lm_sensors) and GPU temperatures (via nvidia-smi) from all 6 arachne compute nodes (3 CPU, 3 GPU with 8x RTX 6000 Ada each). SSH jumps through the arachne headnode. Logs every 5 minutes to the same SQLite database.

### daily_report
Combined status report sent at 7 AM and 4 PM. Includes current room conditions, 6-hour humidity/temperature trend, and node temperatures. Always sent to João; sent to George only when readings are abnormal.

## Email Tags

- `[ALERT]` — immediate threshold violation (humidity > 65% RH, temp > 80°F, CPU > 85°C, GPU > 80°C)
- `[REPORT]` — scheduled daily status report

## Deployment

All components run from cron on badenpowell and share a single SQLite database at `/var/lib/gauge_monitor/gauge_monitor.db`.

- Config files: `/usr/local/etc/{gauge_monitor,node_temps,daily_report}.toml`
- Scripts: `/usr/local/lib/gauge_monitor/`
- Log files: `/var/log/{gauge_monitor,node_temps}.log`
- Database: `/var/lib/gauge_monitor/gauge_monitor.db`
- Snapshots (on threshold violation): `/var/lib/gauge_monitor/snapshots/`

## Querying Trends
```bash
# Hourly humidity/temp averages for the past 24 hours
sqlite3 /var/lib/gauge_monitor/gauge_monitor.db \
  "SELECT strftime('%Y-%m-%d %H:00', timestamp) as hour,
   ROUND(AVG(humidity_pct),1) as avg_rh,
   ROUND(AVG(temperature_f),1) as avg_f
   FROM readings
   WHERE timestamp > datetime('now', '-24 hours', 'localtime')
   GROUP BY hour ORDER BY hour;"

# Node temp averages by node for the past hour
sqlite3 /var/lib/gauge_monitor/gauge_monitor.db \
  "SELECT node, ROUND(AVG(cpu0_tctl),1) as cpu0,
   ROUND(AVG(gpu_max),1) as gpu_max
   FROM node_temps
   WHERE timestamp > datetime('now', '-1 hour', 'localtime')
   GROUP BY node ORDER BY node;"
```

## Planned

- **sound_monitor** — fan noise and SPL monitoring via microphone for detecting degraded PSU fans
