import 'dotenv/config';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function int(value, fallback) {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const config = {
  rootDir,
  dataDir: path.join(rootDir, 'data'),
  dbPath: process.env.DB_PATH || path.join(rootDir, 'data', 'expenses.db'),
  port: int(process.env.PORT, 3000),
  sessionSecret: process.env.SESSION_SECRET || 'insecure-dev-secret',
  passwordHash: process.env.APP_PASSWORD_HASH || '',
  defaultCurrency: (process.env.DEFAULT_CURRENCY || 'ILS').toUpperCase(),
  timezone: process.env.TIMEZONE || 'Asia/Jerusalem',
  mailProvider: (process.env.MAIL_PROVIDER || 'gmail').toLowerCase(),
  google: {
    clientId: process.env.GOOGLE_CLIENT_ID || '',
    clientSecret: process.env.GOOGLE_CLIENT_SECRET || '',
    redirectUri: process.env.GOOGLE_REDIRECT_URI || 'http://localhost:3000/oauth/callback',
  },
  imap: {
    host: process.env.IMAP_HOST || 'imap.gmail.com',
    port: int(process.env.IMAP_PORT, 993),
    user: process.env.IMAP_USER || '',
    password: process.env.IMAP_PASSWORD || '',
    mailbox: process.env.IMAP_MAILBOX || 'INBOX',
  },
  initialSyncDays: int(process.env.INITIAL_SYNC_DAYS, 180),
  syncIntervalMinutes: int(process.env.SYNC_INTERVAL_MINUTES, 30),
};

export function isConfigured() {
  if (config.mailProvider === 'imap') {
    return Boolean(config.imap.user && config.imap.password && config.imap.host);
  }
  return Boolean(config.google.clientId && config.google.clientSecret);
}
