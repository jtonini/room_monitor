#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
node_temps.py — collects CPU and GPU temperatures from arachne compute nodes
via SSH and logs them to the gauge_monitor database.

Run modes:
  --check       Collect temps and check thresholds (default, for cron)
  --history     Show recent readings
  --status      Show current temps for all nodes

Shares the gauge_monitor SQLite database for easy correlation with
room humidity and temperature data.
"""

import os
import sys
import sqlite3
import smtplib
import logging
import argparse
import datetime
import subprocess
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


# ---------------------------------------------------------------
# Globals
# ---------------------------------------------------------------
myconfig = {}
logger   = None
db       = None

PROGRAM  = "node_temps"
VERSION  = "1.0.0"

DEFAULT_CONFIG = "/usr/local/etc/node_temps.toml"


# ---------------------------------------------------------------
# Logging
# ---------------------------------------------------------------
def setup_logger(log_file: str) -> logging.Logger:
    log = logging.getLogger(PROGRAM)
    log.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(name)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    return log


# ---------------------------------------------------------------
# Database
# ---------------------------------------------------------------
def setup_database(db_file: str) -> sqlite3.Connection:
    db_dir = os.path.dirname(db_file)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.Connection(db_file)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS node_temps (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
            node            TEXT NOT NULL,
            cpu0_tctl       REAL,
            cpu1_tctl       REAL,
            gpu_temps       TEXT,
            gpu_max         REAL,
            gpu_count       INTEGER DEFAULT 0,
            collection_ok   INTEGER NOT NULL DEFAULT 1,
            notes           TEXT
        );

        CREATE TABLE IF NOT EXISTS node_temp_alerts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
            alert_type      TEXT NOT NULL,
            node            TEXT,
            message         TEXT,
            email_sent      INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_node_temps_ts ON node_temps(timestamp);
        CREATE INDEX IF NOT EXISTS idx_node_temps_node ON node_temps(node);
        CREATE INDEX IF NOT EXISTS idx_node_temp_alerts_ts ON node_temp_alerts(timestamp);
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------
# Temperature collection
# ---------------------------------------------------------------
def _ssh_cmd(node: str, ssh_user: str, remote_cmd: str) -> list:
    """Build SSH command, with optional jump host."""
    jump_host = myconfig.get("nodes", {}).get("ssh_jump_host", "")
    cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no"]
    if jump_host:
        cmd.extend(["-J", jump_host])
    cmd.extend([f"{ssh_user}@{node}", remote_cmd])
    return cmd


def collect_cpu_temps(node: str, ssh_user: str, timeout: int = 15) -> dict:
    """Collect CPU temps from a node via SSH + lm_sensors."""
    result = {
        "cpu0_tctl": None,
        "cpu1_tctl": None,
        "ok": True,
        "error": None,
    }

    try:
        proc = subprocess.run(
            _ssh_cmd(node, ssh_user, "sensors"),
            capture_output=True, text=True, timeout=timeout
        )
        if proc.returncode != 0:
            result["ok"] = False
            result["error"] = f"ssh sensors failed: {proc.stderr.strip()}"
            return result

        # Parse Tctl values from sensors output.
        # There are typically two k10temp adapters (one per CPU socket).
        tctl_values = []
        for line in proc.stdout.splitlines():
            if line.startswith("Tctl:"):
                try:
                    temp_str = line.split("+")[1].split("°")[0].split(" C")[0].strip()
                    tctl_values.append(float(temp_str))
                except (IndexError, ValueError):
                    pass

        if len(tctl_values) >= 1:
            result["cpu0_tctl"] = tctl_values[0]
        if len(tctl_values) >= 2:
            result["cpu1_tctl"] = tctl_values[1]

    except subprocess.TimeoutExpired:
        result["ok"] = False
        result["error"] = "SSH timeout"
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)

    return result


def collect_gpu_temps(node: str, ssh_user: str, timeout: int = 15) -> dict:
    """Collect GPU temps from a node via SSH + nvidia-smi."""
    result = {
        "temps": [],
        "max": None,
        "count": 0,
        "ok": True,
        "error": None,
    }

    try:
        proc = subprocess.run(
            _ssh_cmd(node, ssh_user,
                     "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader"),
            capture_output=True, text=True, timeout=timeout
        )
        if proc.returncode != 0:
            # nvidia-smi not available or no GPUs — not an error for CPU nodes
            result["ok"] = True
            return result

        for line in proc.stdout.strip().splitlines():
            try:
                temp = float(line.strip())
                result["temps"].append(temp)
            except ValueError:
                pass

        if result["temps"]:
            result["max"] = max(result["temps"])
            result["count"] = len(result["temps"])

    except subprocess.TimeoutExpired:
        result["ok"] = False
        result["error"] = "SSH timeout"
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)

    return result


def collect_node(node: str, ssh_user: str) -> dict:
    """Collect all temps from a single node."""
    logger.debug(f"Collecting temps from {node}")

    cpu = collect_cpu_temps(node, ssh_user)
    gpu = collect_gpu_temps(node, ssh_user)

    result = {
        "node": node,
        "cpu0_tctl": cpu["cpu0_tctl"],
        "cpu1_tctl": cpu["cpu1_tctl"],
        "gpu_temps": gpu["temps"],
        "gpu_max": gpu["max"],
        "gpu_count": gpu["count"],
        "ok": cpu["ok"] and gpu["ok"],
        "error": cpu.get("error") or gpu.get("error"),
    }

    if result["cpu0_tctl"] is not None:
        gpu_str = ""
        if result["gpu_max"] is not None:
            gpu_str = f", GPU max: {result['gpu_max']}°C ({result['gpu_count']} GPUs)"
        logger.info(f"{node}: CPU0={result['cpu0_tctl']}°C, "
                    f"CPU1={result['cpu1_tctl']}°C{gpu_str}")
    else:
        logger.warning(f"{node}: collection failed — {result['error']}")

    return result


# ---------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------
def check_alert_cooldown(alert_type: str, node: str, cooldown_minutes: int) -> bool:
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(minutes=cooldown_minutes)).isoformat()
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM node_temp_alerts "
        "WHERE alert_type = ? AND node = ? AND timestamp > ? AND email_sent = 1",
        (alert_type, node, cutoff)
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


def evaluate_alerts(results: list) -> None:
    cfg_thresh = myconfig["thresholds"]
    cfg_alert = myconfig["alerts"]
    cooldown = cfg_alert.get("cooldown_minutes", 30)

    alerts = []

    for r in results:
        if not r["ok"]:
            continue

        # High CPU temp
        for cpu_label, cpu_temp in [("CPU0", r["cpu0_tctl"]), ("CPU1", r["cpu1_tctl"])]:
            if cpu_temp is not None and cpu_temp > cfg_thresh["max_cpu_temp_c"]:
                atype = "high_cpu_temp"
                if not check_alert_cooldown(atype, r["node"], cooldown):
                    alerts.append((
                        atype, r["node"],
                        f"{r['node']} {cpu_label}: {cpu_temp}°C "
                        f"(threshold: {cfg_thresh['max_cpu_temp_c']}°C)"
                    ))

        # High GPU temp
        if r["gpu_max"] is not None and r["gpu_max"] > cfg_thresh["max_gpu_temp_c"]:
            atype = "high_gpu_temp"
            if not check_alert_cooldown(atype, r["node"], cooldown):
                alerts.append((
                    atype, r["node"],
                    f"{r['node']} GPU max: {r['gpu_max']}°C "
                    f"(threshold: {cfg_thresh['max_gpu_temp_c']}°C)"
                ))

    if not alerts:
        logger.info("All node temps within thresholds.")
        return

    subject = "Arachne's Room — High Node Temperature"
    body_parts = [
        f"Node Temperature Alert — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Triggered alerts:",
    ]
    for atype, node, message in alerts:
        body_parts.append(f"  - {message}")

    body_parts.extend([
        "",
        "Current readings:",
    ])
    for r in results:
        if r["ok"] and r["cpu0_tctl"] is not None:
            line = f"  {r['node']}: CPU0={r['cpu0_tctl']}°C, CPU1={r['cpu1_tctl']}°C"
            if r["gpu_max"] is not None:
                line += f", GPU max={r['gpu_max']}°C ({r['gpu_count']} GPUs)"
            body_parts.append(line)

    body = "\n".join(body_parts)
    email_sent = send_alert_email(subject, body)

    for atype, node, message in alerts:
        db.execute(
            "INSERT INTO node_temp_alerts (alert_type, node, message, email_sent) "
            "VALUES (?, ?, ?, ?)",
            (atype, node, message, int(email_sent))
        )
    db.commit()


# ---------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------
def do_check() -> int:
    cfg_nodes = myconfig["nodes"]
    ssh_user = cfg_nodes.get("ssh_user", "root")
    cpu_nodes = cfg_nodes.get("cpu_nodes", [])
    gpu_nodes = cfg_nodes.get("gpu_nodes", [])
    all_nodes = cpu_nodes + gpu_nodes

    results = []
    for node in all_nodes:
        r = collect_node(node, ssh_user)
        results.append(r)

        # Store in database
        gpu_temps_str = ",".join(str(t) for t in r["gpu_temps"]) if r["gpu_temps"] else None
        db.execute(
            "INSERT INTO node_temps (node, cpu0_tctl, cpu1_tctl, gpu_temps, "
            "gpu_max, gpu_count, collection_ok, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (r["node"], r["cpu0_tctl"], r["cpu1_tctl"], gpu_temps_str,
             r["gpu_max"], r["gpu_count"], int(r["ok"]), r.get("error"))
        )

    db.commit()
    evaluate_alerts(results)
    return 0


def do_status() -> int:
    cfg_nodes = myconfig["nodes"]
    ssh_user = cfg_nodes.get("ssh_user", "root")
    cpu_nodes = cfg_nodes.get("cpu_nodes", [])
    gpu_nodes = cfg_nodes.get("gpu_nodes", [])
    all_nodes = cpu_nodes + gpu_nodes

    print(f"\n{'Node':<12s} {'CPU0':>7s} {'CPU1':>7s} {'GPU max':>8s} {'GPUs':>5s}")
    print("-" * 45)
    for node in all_nodes:
        r = collect_node(node, ssh_user)
        cpu0 = f"{r['cpu0_tctl']:.1f}" if r["cpu0_tctl"] is not None else "  -  "
        cpu1 = f"{r['cpu1_tctl']:.1f}" if r["cpu1_tctl"] is not None else "  -  "
        gpu = f"{r['gpu_max']:.1f}" if r["gpu_max"] is not None else "  -  "
        gpus = str(r["gpu_count"]) if r["gpu_count"] > 0 else "  -"
        status = "" if r["ok"] else " ← FAILED"
        print(f"{node:<12s} {cpu0:>7s} {cpu1:>7s} {gpu:>8s} {gpus:>5s}{status}")

    return 0


def do_history(n: int = 20) -> int:
    rows = db.execute(
        "SELECT * FROM node_temps ORDER BY timestamp DESC LIMIT ?", (n,)
    ).fetchall()

    if not rows:
        print("No readings recorded yet.")
        return 0

    print(f"\n{'Timestamp':<22s} {'Node':<12s} {'CPU0':>7s} {'CPU1':>7s} {'GPU max':>8s}")
    print("-" * 58)
    for r in rows:
        cpu0 = f"{r['cpu0_tctl']:.1f}" if r['cpu0_tctl'] is not None else "  -  "
        cpu1 = f"{r['cpu1_tctl']:.1f}" if r['cpu1_tctl'] is not None else "  -  "
        gpu = f"{r['gpu_max']:.1f}" if r['gpu_max'] is not None else "  -  "
        print(f"{r['timestamp']:<22s} {r['node']:<12s} {cpu0:>7s} {cpu1:>7s} {gpu:>8s}")

    alerts = db.execute(
        "SELECT * FROM node_temp_alerts ORDER BY timestamp DESC LIMIT 5"
    ).fetchall()
    if alerts:
        print(f"\nRecent alerts:")
        for a in alerts:
            sent = "sent" if a["email_sent"] else "NOT sent"
            print(f"  {a['timestamp']}  [{a['node']}] {a['message']} ({sent})")

    return 0


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main() -> int:
    global myconfig, logger, db

    parser = argparse.ArgumentParser(
        description="Arachne's room node temperature monitor"
    )
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG,
                        help=f"Path to TOML config (default: {DEFAULT_CONFIG})")
    parser.add_argument("--check", action="store_true", default=True,
                        help="Collect temps and check thresholds (default)")
    parser.add_argument("--status", action="store_true",
                        help="Show current temps for all nodes")
    parser.add_argument("--history", action="store_true",
                        help="Show recent readings")
    parser.add_argument("--rows", "-n", type=int, default=20,
                        help="Number of history rows to show")
    parser.add_argument("--version", action="version",
                        version=f"{PROGRAM} {VERSION}")

    args = parser.parse_args()

    config_path = args.config
    if not os.path.isfile(config_path):
        print(f"ERROR: config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "rb") as f:
        myconfig = tomllib.load(f)

    log_file = myconfig.get("logging", {}).get("log_file", f"/tmp/{PROGRAM}.log")
    logger = setup_logger(log_file)
    logger.info(f"{PROGRAM} v{VERSION} starting")

    db_file = myconfig.get("logging", {}).get("db_file",
              "/var/lib/gauge_monitor/gauge_monitor.db")
    db = setup_database(db_file)

    if args.status:
        return do_status()
    elif args.history:
        return do_history(args.rows)
    else:
        return do_check()


if __name__ == "__main__":
    sys.exit(main())
