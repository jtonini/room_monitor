-- gauge_monitor.sql — schema for gauge monitor readings and alerts

CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    humidity_pct    REAL,
    temperature_f   REAL,
    temperature_c   REAL,
    snapshot_file   TEXT,
    detection_ok    INTEGER NOT NULL DEFAULT 1,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    alert_type      TEXT    NOT NULL,
    reading_id      INTEGER,
    message         TEXT,
    email_sent      INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (reading_id) REFERENCES readings(id)
);

CREATE INDEX IF NOT EXISTS idx_readings_ts  ON readings(timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_ts    ON alerts(timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_type  ON alerts(alert_type);
