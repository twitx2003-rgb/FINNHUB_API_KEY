import fs from 'node:fs';
import path from 'node:path';
import Database from 'better-sqlite3';
import { config } from './config.js';

fs.mkdirSync(path.dirname(config.dbPath), { recursive: true });

export const db = new Database(config.dbPath);
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

db.exec(`
CREATE TABLE IF NOT EXISTS transactions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id    TEXT UNIQUE,
  occurred_at   TEXT NOT NULL,           -- ISO-8601 UTC
  amount        REAL NOT NULL,           -- positive = money out
  currency      TEXT NOT NULL,
  merchant      TEXT NOT NULL,
  category      TEXT NOT NULL DEFAULT 'אחר',
  account       TEXT,                    -- card suffix / bank account
  note          TEXT,
  sender        TEXT,
  subject       TEXT,
  confidence    REAL NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'confirmed', -- confirmed | review | ignored
  origin        TEXT NOT NULL DEFAULT 'email',     -- email | manual
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tx_occurred  ON transactions(occurred_at);
CREATE INDEX IF NOT EXISTS idx_tx_category  ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_tx_status    ON transactions(status);

-- Messages we looked at and decided were not expenses, so we never re-parse them.
CREATE TABLE IF NOT EXISTS seen_messages (
  message_id TEXT PRIMARY KEY,
  reason     TEXT,
  seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- User-defined categorisation rules, applied before the built-in ones.
CREATE TABLE IF NOT EXISTS rules (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  field     TEXT NOT NULL DEFAULT 'merchant',  -- merchant | subject | sender
  pattern   TEXT NOT NULL,                     -- case-insensitive substring
  category  TEXT NOT NULL,
  priority  INTEGER NOT NULL DEFAULT 100,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS budgets (
  category TEXT PRIMARY KEY,
  monthly_limit REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  scanned     INTEGER NOT NULL DEFAULT 0,
  imported    INTEGER NOT NULL DEFAULT 0,
  skipped     INTEGER NOT NULL DEFAULT 0,
  error       TEXT
);
`);

export function getSetting(key, fallback = null) {
  const row = db.prepare('SELECT value FROM settings WHERE key = ?').get(key);
  return row ? row.value : fallback;
}

export function setSetting(key, value) {
  db.prepare(
    'INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value'
  ).run(key, value == null ? null : String(value));
}

export function getJsonSetting(key, fallback = null) {
  const raw = getSetting(key);
  if (!raw) return fallback;
  try {
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function setJsonSetting(key, value) {
  setSetting(key, JSON.stringify(value));
}
