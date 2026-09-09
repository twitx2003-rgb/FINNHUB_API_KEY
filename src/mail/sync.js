import { config } from '../config.js';
import { db, getSetting, setSetting } from '../db.js';
import { parseMessage } from '../parse/parser.js';
import { categorize } from '../parse/categories.js';
import * as gmail from './gmail.js';
import * as imap from './imap.js';

const DEFAULT_GMAIL_QUERY =
  '(חיוב OR חויב OR קבלה OR חשבונית OR עסקה OR זיכוי OR "אישור תשלום" OR "אישור הזמנה" OR ' +
  'receipt OR invoice OR "order confirmation" OR "you paid" OR charged OR payment OR purchase)';

let running = false;

export function isSyncing() {
  return running;
}

export function connectionStatus() {
  const provider = config.mailProvider;
  const backend = provider === 'imap' ? imap : gmail;
  return {
    provider,
    connected: backend.isConnected(),
    account: backend.getConnectedAccount(),
    lastSyncAt: getSetting('last_sync_at'),
  };
}

function gmailDate(date) {
  const y = date.getUTCFullYear();
  const m = String(date.getUTCMonth() + 1).padStart(2, '0');
  const d = String(date.getUTCDate()).padStart(2, '0');
  return `${y}/${m}/${d}`;
}

function sinceDate() {
  const last = getSetting('last_sync_at');
  if (last) {
    // Re-scan a two-day overlap: mail can arrive out of order and dedup is by id.
    return new Date(new Date(last).getTime() - 2 * 24 * 3600 * 1000);
  }
  return new Date(Date.now() - config.initialSyncDays * 24 * 3600 * 1000);
}

const knownMessage = db.prepare(
  'SELECT 1 FROM transactions WHERE message_id = ? UNION ALL SELECT 1 FROM seen_messages WHERE message_id = ?'
);
const markSeen = db.prepare(
  'INSERT OR IGNORE INTO seen_messages (message_id, reason) VALUES (?, ?)'
);
const insertTx = db.prepare(`
  INSERT OR IGNORE INTO transactions
    (message_id, occurred_at, amount, currency, merchant, category, account, note, sender, subject, confidence, status, origin)
  VALUES
    (@messageId, @occurredAt, @amount, @currency, @merchant, @category, @account, @note, @sender, @subject, @confidence, @status, 'email')
`);
const findNearDuplicate = db.prepare(`
  SELECT id FROM transactions
  WHERE ABS(amount - @amount) < 0.005
    AND currency = @currency
    AND ABS(julianday(occurred_at) - julianday(@occurredAt)) <= 2
    AND status != 'ignored'
  LIMIT 1
`);

function userRules() {
  return db.prepare('SELECT field, pattern, category FROM rules ORDER BY priority ASC, id ASC').all();
}

async function* messageStream(since) {
  if (config.mailProvider === 'imap') {
    yield* imap.iterateMessages({ since });
    return;
  }
  const query = `${getSetting('gmail_query', DEFAULT_GMAIL_QUERY)} after:${gmailDate(since)}`;
  for await (const id of gmail.listMessageIds(query, { max: 1500 })) {
    // Cheap pre-check: skip the (billed, slow) full fetch for ids we already have.
    if (knownMessage.get(`gmail:${id}`, `gmail:${id}`)) continue;
    yield await gmail.fetchMessage(id);
  }
}

/**
 * Pull new mail, extract expenses, store them.
 * @returns {Promise<{scanned:number, imported:number, skipped:number, duplicates:number}>}
 */
export async function runSync({ onProgress } = {}) {
  if (running) throw new Error('סנכרון כבר רץ');
  running = true;

  const startedAt = new Date().toISOString();
  const runId = db
    .prepare('INSERT INTO sync_runs (started_at) VALUES (?)')
    .run(startedAt).lastInsertRowid;

  const stats = { scanned: 0, imported: 0, skipped: 0, duplicates: 0 };
  const rules = userRules();
  const since = sinceDate();

  try {
    for await (const message of messageStream(since)) {
      stats.scanned += 1;
      if (onProgress && stats.scanned % 25 === 0) onProgress({ ...stats });

      if (knownMessage.get(message.id, message.id)) continue;

      const result = parseMessage(message, { defaultCurrency: config.defaultCurrency });
      if (!result.ok) {
        markSeen.run(message.id, result.reason);
        stats.skipped += 1;
        continue;
      }

      const tx = result.transaction;
      const duplicate = findNearDuplicate.get({
        amount: tx.amount,
        currency: tx.currency,
        occurredAt: tx.occurredAt,
      });

      insertTx.run({
        messageId: tx.messageId,
        occurredAt: tx.occurredAt,
        amount: tx.amount,
        currency: tx.currency,
        merchant: tx.merchant,
        category: categorize(tx, rules),
        account: tx.account,
        note: duplicate ? 'ייתכן שזו כפילות של חיוב קיים' : tx.issuer ? `דרך ${tx.issuer}` : null,
        sender: tx.sender,
        subject: tx.subject,
        confidence: tx.confidence,
        status: duplicate ? 'review' : tx.status,
      });

      stats.imported += 1;
      if (duplicate) stats.duplicates += 1;
    }

    setSetting('last_sync_at', new Date().toISOString());
    db.prepare(
      'UPDATE sync_runs SET finished_at = ?, scanned = ?, imported = ?, skipped = ? WHERE id = ?'
    ).run(new Date().toISOString(), stats.scanned, stats.imported, stats.skipped, runId);
    return stats;
  } catch (error) {
    db.prepare(
      'UPDATE sync_runs SET finished_at = ?, scanned = ?, imported = ?, skipped = ?, error = ? WHERE id = ?'
    ).run(new Date().toISOString(), stats.scanned, stats.imported, stats.skipped, String(error.message || error), runId);
    throw error;
  } finally {
    running = false;
  }
}

export { DEFAULT_GMAIL_QUERY };
