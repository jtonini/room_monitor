#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
gauge_monitor.py — reads an analog UPPOD hygrometer/thermometer
via a webcam snapshot (mjpg-streamer) and alerts on high temperature
or high humidity in the cluster room.

Run modes:
  --check       Normal monitoring check (default, for cron)
  --calibrate   Show detected needle overlaid on the image for tuning
  --history     Show recent readings from the database

Uses: TOML config, SQLite logging, email alerts with cooldown.
"""

import os
import sys
import math
import time
import sqlite3
import smtplib
import logging
import argparse
import datetime
import tempfile
import subprocess
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import numpy as np

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

try:
    import cv2
except ImportError:
    print("ERROR: opencv-python-headless required. Install with:")
    print("  pip install opencv-python-headless")
    sys.exit(1)


# ---------------------------------------------------------------
# Globals (populated by main)
# ---------------------------------------------------------------
myconfig = {}
logger   = None
db       = None

PROGRAM  = "gauge_monitor"
VERSION  = "1.0.0"

DEFAULT_CONFIG = "/usr/local/etc/gauge_monitor.toml"


# ---------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------
def setup_logger(log_file: str) -> logging.Logger:
    """Configure and return the logger."""
    log = logging.getLogger(PROGRAM)
    log.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(name)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File handler
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # Console handler (INFO and above)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    return log


# ---------------------------------------------------------------
# Database
# ---------------------------------------------------------------
def setup_database(db_file: str, schema_file: str) -> sqlite3.Connection:
    """Open (and optionally create) the SQLite database."""
    db_dir = os.path.dirname(db_file)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.Connection(db_file)
    conn.row_factory = sqlite3.Row

    if os.path.isfile(schema_file):
        with open(schema_file) as f:
            conn.executescript(f.read())
    else:
        # Inline fallback schema
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS readings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
                humidity_pct  REAL,
                temperature_f REAL,
                temperature_c REAL,
                snapshot_file TEXT,
                detection_ok  INTEGER NOT NULL DEFAULT 1,
                notes         TEXT
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
                alert_type    TEXT NOT NULL,
                reading_id    INTEGER,
                message       TEXT,
                email_sent    INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (reading_id) REFERENCES readings(id)
            );
        """)
    conn.commit()
    return conn


# ---------------------------------------------------------------
# Image acquisition
# ---------------------------------------------------------------
def fetch_snapshot(url: str, timeout: int = 10) -> np.ndarray:
    """Fetch a JPEG snapshot from mjpg-streamer and return as cv2 image."""
    logger.info(f"Fetching snapshot from {url}")

    # Use curl to fetch the image (handles proxies, redirects, etc.)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["curl", "-s", "-o", tmp_path, "-m", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl failed (rc={result.returncode}): {result.stderr}")

        img = cv2.imread(tmp_path)
        if img is None:
            raise RuntimeError(f"Failed to decode image from {tmp_path}")

        logger.info(f"Snapshot acquired: {img.shape[1]}x{img.shape[0]}")
        return img

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------
# Needle detection
# ---------------------------------------------------------------
def detect_needle_angle(gray: np.ndarray,
                        center_x: int, center_y: int,
                        scan_radius_min: int, scan_radius_max: int,
                        ) -> float:
    """
    Detect the angle of a dark needle on an analog gauge.

    Sweeps radially from center at 1-degree increments, computing
    mean darkness along each radial line. The darkest direction
    is the needle.

    Returns angle in degrees (math convention: 0=right, 90=up, CCW positive).
    """
    h, w = gray.shape
    best_angle = 0
    best_darkness = 999

    for deg in range(360):
        if deg > 300 or deg < 5:
            continue
        rad = math.radians(deg)
        values = []
        for r in range(scan_radius_min, scan_radius_max, 3):
            px = int(center_x + r * math.cos(rad))
            py = int(center_y - r * math.sin(rad))
            if 0 <= px < w and 0 <= py < h:
                values.append(int(gray[py, px]))

        if values:
            mean_bright = sum(values) / len(values)
            if mean_bright < best_darkness:
                best_darkness = mean_bright
                best_angle = deg

    # Refine with 0.5-degree steps around the peak
    refined_angle = float(best_angle)
    refined_darkness = best_darkness
    for offset in np.arange(-2.0, 2.0, 0.5):
        deg = best_angle + offset
        rad = math.radians(deg)
        values = []
        for r in range(scan_radius_min, scan_radius_max, 2):
            px = int(center_x + r * math.cos(rad))
            py = int(center_y - r * math.sin(rad))
            if 0 <= px < w and 0 <= py < h:
                values.append(int(gray[py, px]))
        if values:
            mean_bright = sum(values) / len(values)
            if mean_bright < refined_darkness:
                refined_darkness = mean_bright
                refined_angle = deg

    return refined_angle


def angle_to_value(angle_deg: float,
                   angle_at_min: float, angle_at_max: float,
                   value_min: float, value_max: float) -> float:
    """
    Convert a detected needle angle to a gauge reading (linear model).

    angle_at_min: math-convention angle (degrees) where the gauge reads value_min.
    angle_at_max: math-convention angle where the gauge reads value_max.
    The scale goes clockwise (decreasing math angle) from min to max.
    """
    total_sweep = angle_at_min - angle_at_max
    traversed = angle_at_min - angle_deg

    while traversed < -10:
        traversed += 360
    while traversed > total_sweep + 10:
        traversed -= 360

    fraction = traversed / total_sweep
    value = value_min + fraction * (value_max - value_min)

    return round(value, 1)


def angle_to_value_piecewise(angle_deg: float,
                             calibration_table: list) -> float:
    """
    Convert a detected needle angle to a gauge reading using piecewise
    linear interpolation from a calibration table.

    calibration_table: list of (value, angle_deg) tuples, sorted by
                       descending angle (i.e., increasing value).
                       Example: [(0, 210.8), (10, 189.8), ..., (100, -50.9)]
    """
    # Handle out-of-range: clamp to endpoints
    if angle_deg >= calibration_table[0][1]:
        return calibration_table[0][0]
    if angle_deg <= calibration_table[-1][1]:
        return calibration_table[-1][0]

    # Find the two bracketing entries
    for i in range(len(calibration_table) - 1):
        v1, a1 = calibration_table[i]
        v2, a2 = calibration_table[i + 1]
        if a1 >= angle_deg >= a2:
            frac = (a1 - angle_deg) / (a1 - a2)
            return round(v1 + frac * (v2 - v1), 1)

    # Fallback: linear between first and last
    v1, a1 = calibration_table[0]
    v2, a2 = calibration_table[-1]
    frac = (a1 - angle_deg) / (a1 - a2)
    return round(v1 + frac * (v2 - v1), 1)


def read_gauge(img: np.ndarray) -> dict:
    """
    Read both the humidity and temperature dials from the gauge image.

    Returns dict with keys: humidity_pct, temperature_c, temperature_f,
                            humidity_angle, temperature_angle, detection_ok
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cfg = myconfig["gauge"]

    result = {
        "humidity_pct": None,
        "temperature_c": None,
        "temperature_f": None,
        "humidity_angle": None,
        "temperature_angle": None,
        "detection_ok": True,
    }

    # --- Main dial: Humidity ---
    hum_cx = cfg["humidity_center_x"]
    hum_cy = cfg["humidity_center_y"]
    hum_r = cfg["humidity_scan_radius"]

    hum_angle = detect_needle_angle(
        gray, hum_cx, hum_cy,
        scan_radius_min=max(60, hum_r // 4),
        scan_radius_max=hum_r,
    )
    result["humidity_angle"] = hum_angle

    # Use piecewise calibration table if available, else linear.
    hum_cal = cfg.get("humidity_calibration")
    if hum_cal:
        # TOML gives us a list of [value, angle] pairs.
        hum_value = angle_to_value_piecewise(hum_angle, hum_cal)
    else:
        hum_value = angle_to_value(
            hum_angle,
            cfg["humidity_angle_min_deg"],
            cfg["humidity_angle_max_deg"],
            cfg["humidity_min_value"],
            cfg["humidity_max_value"],
        )
    result["humidity_pct"] = max(0.0, min(100.0, hum_value))
    logger.info(f"Humidity: needle at {hum_angle:.1f} deg -> {result['humidity_pct']:.1f} %RH")

    # --- Sub dial: Temperature ---
    temp_cx = cfg["temperature_center_x"]
    temp_cy = cfg["temperature_center_y"]
    temp_r = cfg["temperature_scan_radius"]

    temp_angle = detect_needle_angle(
        gray, temp_cx, temp_cy,
        scan_radius_min=max(40, temp_r // 2),
        scan_radius_max=temp_r,
    )
    result["temperature_angle"] = temp_angle

    temp_cal = cfg.get("temperature_calibration")
    if temp_cal:
        temp_c = angle_to_value_piecewise(temp_angle, temp_cal)
    else:
        temp_c = angle_to_value(
            temp_angle,
            cfg["temperature_angle_min_deg"],
            cfg["temperature_angle_max_deg"],
            cfg["temperature_min_c"],
            cfg["temperature_max_c"],
        )
    result["temperature_c"] = round(temp_c, 1)
    result["temperature_f"] = round(temp_c * 9.0 / 5.0 + 32.0, 1)
    logger.info(f"Temperature: needle at {temp_angle:.1f} deg -> "
                f"{result['temperature_c']:.1f} C / {result['temperature_f']:.1f} F")

    return result


# ---------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------
def check_alert_cooldown(alert_type: str, cooldown_minutes: int) -> bool:
    """Return True if we should suppress this alert (within cooldown)."""
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(minutes=cooldown_minutes)).isoformat()
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM alerts "
        "WHERE alert_type = ? AND timestamp > ? AND email_sent = 1",
        (alert_type, cutoff)
    ).fetchone()
    return row["cnt"] > 0


def _in_quiet_hours() -> bool:
    """Check if we are in quiet hours (no alerts)."""
    cfg_quiet = myconfig.get("quiet_hours", {})
    if not cfg_quiet:
        return False
    now = datetime.datetime.now()
    if cfg_quiet.get("suppress_weekends", False) and now.weekday() >= 5:
        logger.debug("Quiet hours: weekend")
        return True
    start = cfg_quiet.get("start_hour", 0)
    end = cfg_quiet.get("end_hour", 0)
    hour = now.hour
    if start > end:
        if hour >= start or hour < end:
            logger.debug(f"Quiet hours: {hour}:00 outside {end}:00-{start}:00")
            return True
    elif start < end:
        if start <= hour < end:
            return True
    return False


def send_alert_email(subject: str, body: str) -> bool:
    """Send an alert email. Returns True on success."""
    if _in_quiet_hours():
        logger.info(f"Alert suppressed (quiet hours): {subject}")
        return False

    cfg = myconfig["alerts"]

    msg = MIMEMultipart()
    msg["From"] = cfg["from_address"]
    msg["To"] = ", ".join(cfg["to_addresses"])
    msg["Subject"] = f"[{PROGRAM}] {subject}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as smtp:
            smtp.sendmail(cfg["from_address"], cfg["to_addresses"], msg.as_string())
        logger.info(f"Alert email sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")
        return False


def evaluate_alerts(reading: dict, reading_id: int) -> None:
    """Check thresholds and send alerts if needed."""
    cfg_thresh = myconfig["thresholds"]
    cfg_alert = myconfig["alerts"]
    cooldown = cfg_alert.get("cooldown_minutes", 30)

    alerts_to_send = []

    # High temperature
    if (reading["temperature_f"] is not None
            and reading["temperature_f"] > cfg_thresh["max_temperature_f"]):
        atype = "high_temperature"
        if not check_alert_cooldown(atype, cooldown):
            alerts_to_send.append((
                atype,
                f"High Temperature: {reading['temperature_f']} F "
                f"(threshold: {cfg_thresh['max_temperature_f']} F)",
            ))

    # High humidity
    if (reading["humidity_pct"] is not None
            and reading["humidity_pct"] > cfg_thresh["max_humidity_pct"]):
        atype = "high_humidity"
        if not check_alert_cooldown(atype, cooldown):
            alerts_to_send.append((
                atype,
                f"High Humidity: {reading['humidity_pct']}% RH "
                f"(threshold: {cfg_thresh['max_humidity_pct']}% RH)",
            ))

    if not alerts_to_send:
        logger.info("All readings within thresholds.")
        return

    # Build combined alert message
    subject = "[ALERT] Arachne's Room Environment"
    body_parts = [
        f"Arachne's Room Alert — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"Humidity:    {reading['humidity_pct']}% RH",
        f"Temperature: {reading['temperature_f']} F / {reading['temperature_c']} C",
        "",
        "Triggered alerts:",
    ]
    for atype, message in alerts_to_send:
        body_parts.append(f"  - {message}")

    body_parts.extend([
        "",
        f"Webcam: http://mingus.richmond.edu:8080/",
        f"Check the gauge at: {myconfig['webcam']['snapshot_url']}",
    ])
    body = "\n".join(body_parts)

    email_sent = send_alert_email(subject, body)

    for atype, message in alerts_to_send:
        db.execute(
            "INSERT INTO alerts (alert_type, reading_id, message, email_sent) "
            "VALUES (?, ?, ?, ?)",
            (atype, reading_id, message, int(email_sent))
        )
    db.commit()


# ---------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------
def do_check() -> int:
    """Normal monitoring check. Returns 0 on success, 1 on failure."""
    try:
        img = fetch_snapshot(
            myconfig["webcam"]["snapshot_url"],
            myconfig["webcam"].get("timeout_seconds", 10),
        )
    except Exception as e:
        logger.error(f"Failed to fetch snapshot: {e}")
        db.execute(
            "INSERT INTO readings (detection_ok, notes) VALUES (0, ?)",
            (f"Snapshot fetch failed: {e}",)
        )
        db.commit()
        return 1

    reading = read_gauge(img)

    # Save snapshot only if a threshold is exceeded.
    snapshot_file = None
    cfg_thresh = myconfig["thresholds"]
    over_threshold = (
        (reading["humidity_pct"] is not None
         and reading["humidity_pct"] > cfg_thresh["max_humidity_pct"])
        or
        (reading["temperature_f"] is not None
         and reading["temperature_f"] > cfg_thresh["max_temperature_f"])
    )
    if over_threshold:
        cfg_log = myconfig.get("logging", {})
        snap_dir = cfg_log.get("snapshot_dir", "/tmp/gauge_snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_file = os.path.join(snap_dir, f"gauge_{ts}.jpg")
        cv2.imwrite(snapshot_file, img)
        logger.debug(f"Snapshot saved (threshold exceeded): {snapshot_file}")

    # Store reading
    cur = db.execute(
        "INSERT INTO readings (humidity_pct, temperature_f, temperature_c, "
        "snapshot_file, detection_ok) VALUES (?, ?, ?, ?, ?)",
        (reading["humidity_pct"], reading["temperature_f"],
         reading["temperature_c"], snapshot_file,
         int(reading["detection_ok"]))
    )
    db.commit()
    reading_id = cur.lastrowid

    # Evaluate alert thresholds
    evaluate_alerts(reading, reading_id)

    return 0


def do_calibrate(image_path: str = None) -> int:
    """
    Calibration mode: fetch (or load) an image, detect needles,
    and save an annotated debug image for visual verification.
    """
    if image_path and os.path.isfile(image_path):
        img = cv2.imread(image_path)
        if img is None:
            logger.error(f"Cannot read image: {image_path}")
            return 1
    else:
        try:
            img = fetch_snapshot(
                myconfig["webcam"]["snapshot_url"],
                myconfig["webcam"].get("timeout_seconds", 10),
            )
        except Exception as e:
            logger.error(f"Failed to fetch snapshot: {e}")
            return 1

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    debug = img.copy()
    cfg = myconfig["gauge"]
    h, w = gray.shape

    print(f"\nImage size: {w} x {h}")

    # --- Humidity dial ---
    hum_cx = cfg["humidity_center_x"]
    hum_cy = cfg["humidity_center_y"]
    hum_r = cfg["humidity_scan_radius"]

    # Draw center and scan circle
    cv2.circle(debug, (hum_cx, hum_cy), 8, (0, 255, 0), -1)
    cv2.circle(debug, (hum_cx, hum_cy), hum_r, (0, 255, 0), 1)

    hum_angle = detect_needle_angle(
        gray, hum_cx, hum_cy,
        scan_radius_min=max(60, hum_r // 4),
        scan_radius_max=hum_r,
    )

    # Draw detected needle
    rad = math.radians(hum_angle)
    nx = int(hum_cx + hum_r * math.cos(rad))
    ny = int(hum_cy - hum_r * math.sin(rad))
    cv2.line(debug, (hum_cx, hum_cy), (nx, ny), (0, 0, 255), 3)

    hum_value = angle_to_value(
        hum_angle,
        cfg["humidity_angle_min_deg"], cfg["humidity_angle_max_deg"],
        cfg["humidity_min_value"], cfg["humidity_max_value"],
    )
    hum_value_linear = max(0.0, min(100.0, hum_value))

    # Use piecewise if available
    hum_cal = cfg.get("humidity_calibration")
    if hum_cal:
        hum_value_pw = angle_to_value_piecewise(hum_angle, hum_cal)
        hum_value_pw = max(0.0, min(100.0, hum_value_pw))
        print(f"\nHumidity dial:")
        print(f"  Center: ({hum_cx}, {hum_cy})")
        print(f"  Detected needle angle: {hum_angle:.1f} deg")
        print(f"  Piecewise reading: {hum_value_pw:.1f} %RH")
        print(f"  Linear reading:    {hum_value_linear:.1f} %RH (for comparison)")
        hum_display = hum_value_pw
    else:
        print(f"\nHumidity dial:")
        print(f"  Center: ({hum_cx}, {hum_cy})")
        print(f"  Detected needle angle: {hum_angle:.1f} deg")
        print(f"  Computed reading: {hum_value_linear:.1f} %RH")
        hum_display = hum_value_linear

    # Draw reference marks — from calibration table if available, else linear
    if hum_cal:
        for entry in hum_cal:
            pct, ref_angle = entry[0], entry[1]
            rr = math.radians(ref_angle)
            x1 = int(hum_cx + (hum_r - 15) * math.cos(rr))
            y1 = int(hum_cy - (hum_r - 15) * math.sin(rr))
            x2 = int(hum_cx + (hum_r + 10) * math.cos(rr))
            y2 = int(hum_cy - (hum_r + 10) * math.sin(rr))
            color = (255, 255, 0) if int(pct) % 50 == 0 else (0, 255, 255)
            cv2.line(debug, (x1, y1), (x2, y2), color, 2)
            cv2.putText(debug, str(int(pct)), (x2 + 3, y2 + 3),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
    else:
        for pct in range(0, 101, 10):
            frac = pct / 100.0
            ref_angle = cfg["humidity_angle_min_deg"] - frac * (
                cfg["humidity_angle_min_deg"] - cfg["humidity_angle_max_deg"]
            )
            rr = math.radians(ref_angle)
            x1 = int(hum_cx + (hum_r - 20) * math.cos(rr))
            y1 = int(hum_cy - (hum_r - 20) * math.sin(rr))
            x2 = int(hum_cx + (hum_r + 10) * math.cos(rr))
            y2 = int(hum_cy - (hum_r + 10) * math.sin(rr))
            color = (255, 255, 0) if pct % 50 == 0 else (200, 200, 0)
            cv2.line(debug, (x1, y1), (x2, y2), color, 2)
            if pct % 20 == 0:
                cv2.putText(debug, str(pct), (x2 + 5, y2 + 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

    # --- Temperature dial ---
    temp_cx = cfg["temperature_center_x"]
    temp_cy = cfg["temperature_center_y"]
    temp_r = cfg["temperature_scan_radius"]

    cv2.circle(debug, (temp_cx, temp_cy), 6, (0, 255, 255), -1)
    cv2.circle(debug, (temp_cx, temp_cy), temp_r, (0, 255, 255), 1)

    temp_angle = detect_needle_angle(
        gray, temp_cx, temp_cy,
        scan_radius_min=max(40, temp_r // 2),
        scan_radius_max=temp_r,
    )

    rad_t = math.radians(temp_angle)
    tx = int(temp_cx + temp_r * math.cos(rad_t))
    ty = int(temp_cy - temp_r * math.sin(rad_t))
    cv2.line(debug, (temp_cx, temp_cy), (tx, ty), (0, 0, 255), 2)

    temp_c = angle_to_value(
        temp_angle,
        cfg["temperature_angle_min_deg"], cfg["temperature_angle_max_deg"],
        cfg["temperature_min_c"], cfg["temperature_max_c"],
    )
    temp_f = round(temp_c * 9.0 / 5.0 + 32.0, 1)

    print(f"\nTemperature dial:")
    print(f"  Center: ({temp_cx}, {temp_cy})")
    print(f"  Detected needle angle: {temp_angle:.1f} deg")
    print(f"  Computed reading: {temp_c:.1f} C / {temp_f:.1f} F")

    # Add text overlay
    cv2.putText(debug, f"Humidity: {hum_display:.1f}% RH (angle={hum_angle:.1f})",
                (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
    cv2.putText(debug, f"Temp: {temp_c:.1f} C / {temp_f:.1f} F (angle={temp_angle:.1f})",
                (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)

    out_path = "gauge_calibration.png"
    cv2.imwrite(out_path, debug)
    print(f"\nCalibration image saved to: {out_path}")
    print("\nTo adjust, edit the [gauge] section in your TOML config.")
    print("The red line shows the detected needle direction.")
    print("Cyan/yellow tick marks show where the scale values should be.")
    print("If they don't align with the printed numbers on the gauge,")
    print("adjust *_angle_min_deg and *_angle_max_deg accordingly.")

    return 0


def do_history(n: int = 20) -> int:
    """Show recent readings from the database."""
    rows = db.execute(
        "SELECT * FROM readings ORDER BY timestamp DESC LIMIT ?", (n,)
    ).fetchall()

    if not rows:
        print("No readings recorded yet.")
        return 0

    print(f"\n{'Timestamp':<22s} {'Humid%':>7s} {'Temp F':>7s} {'Temp C':>7s} {'OK':>3s}")
    print("-" * 55)
    for r in rows:
        ok_str = "Y" if r["detection_ok"] else "N"
        hum = f"{r['humidity_pct']:.1f}" if r['humidity_pct'] is not None else "  -  "
        tf = f"{r['temperature_f']:.1f}" if r['temperature_f'] is not None else "  -  "
        tc = f"{r['temperature_c']:.1f}" if r['temperature_c'] is not None else "  -  "
        print(f"{r['timestamp']:<22s} {hum:>7s} {tf:>7s} {tc:>7s} {ok_str:>3s}")

    # Also show recent alerts
    alerts = db.execute(
        "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT 10"
    ).fetchall()
    if alerts:
        print(f"\nRecent alerts:")
        for a in alerts:
            sent = "sent" if a["email_sent"] else "NOT sent"
            print(f"  {a['timestamp']}  [{a['alert_type']}] {a['message']} ({sent})")

    return 0


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main() -> int:
    global myconfig, logger, db

    parser = argparse.ArgumentParser(
        description="Cluster room gauge monitor — reads analog hygrometer via webcam"
    )
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG,
                        help=f"Path to TOML config (default: {DEFAULT_CONFIG})")
    parser.add_argument("--check", action="store_true", default=True,
                        help="Run a monitoring check (default mode)")
    parser.add_argument("--calibrate", action="store_true",
                        help="Calibration mode: show detected needles on image")
    parser.add_argument("--image", "-i",
                        help="Image file for calibration (instead of fetching live)")
    parser.add_argument("--history", action="store_true",
                        help="Show recent readings from database")
    parser.add_argument("--rows", "-n", type=int, default=20,
                        help="Number of history rows to show")
    parser.add_argument("--version", action="version",
                        version=f"{PROGRAM} {VERSION}")

    args = parser.parse_args()

    # Load config
    config_path = args.config
    if not os.path.isfile(config_path):
        print(f"ERROR: config file not found: {config_path}")
        print(f"Copy the example config to {DEFAULT_CONFIG} and edit it.")
        sys.exit(1)

    with open(config_path, "rb") as f:
        myconfig = tomllib.load(f)

    # Setup logger
    log_file = myconfig.get("logging", {}).get("log_file", f"/tmp/{PROGRAM}.log")
    logger = setup_logger(log_file)
    logger.info(f"{PROGRAM} v{VERSION} starting")

    # Setup database
    schema_dir = os.path.dirname(os.path.abspath(__file__))
    schema_file = os.path.join(schema_dir, "gauge_monitor.sql")
    db_file = myconfig.get("logging", {}).get("db_file", f"/tmp/{PROGRAM}.db")
    db = setup_database(db_file, schema_file)

    # Dispatch
    if args.calibrate:
        return do_calibrate(args.image)
    elif args.history:
        return do_history(args.rows)
    else:
        return do_check()


if __name__ == "__main__":
    sys.exit(main())
