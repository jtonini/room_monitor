#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
daily_report.py — generates a daily status report for Arachne's room
combining gauge_monitor (humidity/temp) and node_temps data.

Sends to João always, and to George only if any reading is above threshold.

Intended to run from cron at 7 AM and 4 PM.
"""

import os
import sys
import sqlite3
import smtplib
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


DEFAULT_CONFIG = "/usr/local/etc/daily_report.toml"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Arachne's room daily report")
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    db = sqlite3.connect(cfg["logging"]["db_file"])
    db.row_factory = sqlite3.Row

    now = datetime.datetime.now()
    thresh = cfg["thresholds"]

    # --- Gather room data ---
    current = db.execute(
        "SELECT * FROM readings ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()

    hourly = db.execute(
        "SELECT strftime('%H:%M', timestamp) as time, "
        "ROUND(AVG(humidity_pct),1) as avg_rh, "
        "ROUND(AVG(temperature_f),1) as avg_f, "
        "ROUND(AVG(temperature_c),1) as avg_c "
        "FROM readings "
        "WHERE timestamp > datetime('now', '-6 hours', 'localtime') "
        "GROUP BY strftime('%H', timestamp) ORDER BY timestamp"
    ).fetchall()

    # --- Gather node data ---
    # Get latest reading per node
    nodes = db.execute(
        "SELECT node, cpu0_tctl, cpu1_tctl, gpu_max, gpu_count, timestamp "
        "FROM node_temps "
        "WHERE timestamp > datetime('now', '-10 minutes', 'localtime') "
        "GROUP BY node "
        "HAVING MAX(timestamp) "
        "ORDER BY node"
    ).fetchall()

    # --- Check if anything is abnormal ---
    abnormal = False
    warnings = []

    if current:
        if current["humidity_pct"] and current["humidity_pct"] > thresh["max_humidity_pct"]:
            abnormal = True
            warnings.append(f"Humidity {current['humidity_pct']}% RH > {thresh['max_humidity_pct']}% threshold")
        if current["temperature_f"] and current["temperature_f"] > thresh["max_temperature_f"]:
            abnormal = True
            warnings.append(f"Temperature {current['temperature_f']}°F > {thresh['max_temperature_f']}°F threshold")

    for n in nodes:
        if n["cpu0_tctl"] and n["cpu0_tctl"] > thresh["max_cpu_temp_c"]:
            abnormal = True
            warnings.append(f"{n['node']} CPU0 {n['cpu0_tctl']}°C > {thresh['max_cpu_temp_c']}°C threshold")
        if n["cpu1_tctl"] and n["cpu1_tctl"] > thresh["max_cpu_temp_c"]:
            abnormal = True
            warnings.append(f"{n['node']} CPU1 {n['cpu1_tctl']}°C > {thresh['max_cpu_temp_c']}°C threshold")
        if n["gpu_max"] and n["gpu_max"] > thresh["max_gpu_temp_c"]:
            abnormal = True
            warnings.append(f"{n['node']} GPU max {n['gpu_max']}°C > {thresh['max_gpu_temp_c']}°C threshold")

    # --- Build report ---
    lines = [
        f"Arachne's Room Report — {now.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    if warnings:
        lines.append("⚠ WARNINGS:")
        for w in warnings:
            lines.append(f"  - {w}")
        lines.append("")

    lines.append("Room conditions:")
    if current:
        lines.append(f"  Humidity:    {current['humidity_pct']}% RH")
        lines.append(f"  Temperature: {current['temperature_f']} F / {current['temperature_c']} C")
    else:
        lines.append("  No recent readings available.")

    lines.append("")
    lines.append("Last 6 hours (hourly avg):")
    if hourly:
        lines.append(f"  {'Time':>5s}  {'RH':>7s}  {'Temp':>10s}")
        for h in hourly:
            lines.append(f"  {h['time']:>5s}  {h['avg_rh']:>5.1f}%  {h['avg_f']:>5.1f} F / {h['avg_c']:.1f} C")
    else:
        lines.append("  No data available.")

    lines.append("")
    lines.append("Node temperatures:")
    if nodes:
        lines.append(f"  {'Node':<10s} {'CPU0':>7s} {'CPU1':>7s} {'GPU max':>8s} {'GPUs':>5s}")
        lines.append(f"  {'-'*42}")
        for n in nodes:
            cpu0 = f"{n['cpu0_tctl']:.1f}" if n['cpu0_tctl'] else "  -  "
            cpu1 = f"{n['cpu1_tctl']:.1f}" if n['cpu1_tctl'] else "  -  "
            gpu = f"{n['gpu_max']:.1f}" if n['gpu_max'] else "  -  "
            gpus = str(n['gpu_count']) if n['gpu_count'] and n['gpu_count'] > 0 else "  -"
            lines.append(f"  {n['node']:<10s} {cpu0:>7s} {cpu1:>7s} {gpu:>8s} {gpus:>5s}")
    else:
        lines.append("  No recent node data available.")

    lines.extend([
        "",
        f"Webcam: http://mingus.richmond.edu:8080/",
    ])

    body = "\n".join(lines)

    # --- Send email ---
    tag = "[REPORT]"
    subject = f"{tag} Arachne's Room Status — {now.strftime('%H:%M')}"

    # Always send to primary recipients
    recipients = list(cfg["email"]["to_addresses"])

    # Add conditional recipients (George) only if abnormal
    if abnormal and cfg["email"].get("to_addresses_abnormal"):
        recipients.extend(cfg["email"]["to_addresses_abnormal"])

    msg = MIMEMultipart()
    msg["From"] = cfg["email"]["from_address"]
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(cfg["email"]["smtp_server"], cfg["email"]["smtp_port"]) as smtp:
            smtp.sendmail(cfg["email"]["from_address"], recipients, msg.as_string())
        print(f"Report sent to: {', '.join(recipients)}")
        if abnormal:
            print(f"Abnormal conditions detected — George notified")
    except Exception as e:
        print(f"Failed to send report: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
