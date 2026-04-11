#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
daily_report.py — generates a daily status report for Arachne's room
combining gauge_monitor (humidity/temp), node_temps, and George's
collecttemps data.

Sends to João always, and to George only if any reading is above threshold.

Intended to run from cron at 7 AM and 4 PM.
"""

import os
import sys
import sqlite3
import smtplib
import subprocess
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


DEFAULT_CONFIG = "/usr/local/etc/daily_report.toml"


def get_george_temps(ssh_target: str, db_path: str) -> list:
    """Query George's collecttemps database on arachne."""
    query = (
        f"SELECT node, ROUND(mean,1), ROUND(sigma,1), "
        f"ROUND(maxtemp,1), ROUND(load,1), t "
        f"FROM facts WHERE t > datetime('now', '-2 minutes') GROUP BY node "
        f"ORDER BY node"
    )
    try:
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             ssh_target, f"sqlite3 {db_path} \"{query}\""],
            capture_output=True, text=True, timeout=15
        )
        if proc.returncode != 0:
            return []

        results = []
        for line in proc.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 5:
                results.append({
                    "node": parts[0],
                    "mean": parts[1],
                    "sigma": parts[2],
                    "max": parts[3],
                    "load": parts[4],
                })
        return results
    except Exception:
        return []


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

    # --- Gather George's collecttemps data ---
    george_cfg = cfg.get("george_temps", {})
    george_data = get_george_temps(
        george_cfg.get("ssh_target", "zeus@arachne"),
        george_cfg.get("db_path", "/home/zeus/temps/temperatures.db")
    )

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

    for n in george_data:
        try:
            max_temp = float(n["max"])
            if max_temp > thresh["max_cpu_temp_c"]:
                abnormal = True
                warnings.append(f"{n['node']} max temp {n['max']}°C > {thresh['max_cpu_temp_c']}°C threshold")
        except (ValueError, KeyError):
            pass

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
    lines.append("Node temperatures (collecttemps):")
    if george_data:
        lines.append(f"  {'Node':<10s} {'Mean':>6s} {'Sigma':>6s} {'Max':>6s} {'Load':>6s}")
        lines.append(f"  {'-'*38}")
        for n in george_data:
            lines.append(f"  {n['node']:<10s} {n['mean']:>6s} {n['sigma']:>6s} {n['max']:>6s} {n['load']:>6s}")
    else:
        lines.append("  No recent node data available (collecttemps may not be running).")

    lines.extend([
        "",
        f"Webcam: http://mingus.richmond.edu:8080/",
    ])

    body = "\n".join(lines)

    # --- Send email ---
    tag = "[REPORT]"
    subject = f"{tag} Arachne's Room Status — {now.strftime('%H:%M')}"

    recipients = list(cfg["email"]["to_addresses"])

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
