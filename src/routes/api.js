import express from 'express';
import { db } from '../db.js';
import { config } from '../config.js';
import { CATEGORIES, categorize } from '../parse/categories.js';
import { runSync, connectionStatus, isSyncing } from '../mail/sync.js';

export const api = express.Router();

const ok = (res, payload) => res.json(payload);
const fail = (res, status, message) => res.status(status).json({ error: message });

function buildFilter(query) {
  const clauses = [];
  const params = {};

  if (query.from) {
    clauses.push('occurred_at >= @from');
    params.from = new Date(query.from).toISOString();
  }
  if (query.to) {
    const to = new Date(query.to);
    to.setUTCHours(23, 59, 59, 999);
    clauses.push('occurred_at <= @to');
    params.to = to.toISOString();
  }
  if (query.category) {
    clauses.push('category = @category');
    params.category = query.category;
  }
  if (query.status) {
    clauses.push('status = @status');
    params.status = query.status;
  } else {
    clauses.push("status != 'ignored'");
  }
  if (query.q) {
    clauses.push('(merchant LIKE @q OR subject LIKE @q OR note LIKE @q)');
    params.q = `%${query.q}%`;
  }
  return { where: clauses.length ? `WHERE ${clauses.join(' AND ')}` : '', params };
}

api.get('/status', (req, res) => {
  const counts = db
    .prepare(
      `SELECT
         COUNT(*) AS total,
         SUM(CASE WHEN status = 'review' THEN 1 ELSE 0 END) AS review
       FROM transactions`
    )
    .get();
  ok(res, {
    ...connectionStatus(),
    syncing: isSyncing(),
    defaultCurrency: config.defaultCurrency,
    transactions: counts.total || 0,
    needsReview: counts.review || 0,
  });
});

api.get('/categories', (req, res) => ok(res, { categories: CATEGORIES }));

api.get('/transactions', (req, res) => {
  const { where, params } = buildFilter(req.query);
  const limit = Math.min(Number(req.query.limit) || 200, 1000);
  const offset = Number(req.query.offset) || 0;

  const rows = db
    .prepare(
      `SELECT * FROM transactions ${where} ORDER BY occurred_at DESC, id DESC LIMIT @limit OFFSET @offset`
    )
    .all({ ...params, limit, offset });
  const { total } = db.prepare(`SELECT COUNT(*) AS total FROM transactions ${where}`).get(params);

  ok(res, { transactions: rows, total, limit, offset });
});

api.post('/transactions', (req, res) => {
  const { occurredAt, amount, currency, merchant, category, note } = req.body || {};
  const value = Number(amount);
  if (!merchant || !Number.isFinite(value)) {
    return fail(res, 400, 'נדרשים בית עסק וסכום תקין');
  }
  const info = db
    .prepare(
      `INSERT INTO transactions (message_id, occurred_at, amount, currency, merchant, category, note, confidence, status, origin)
       VALUES (NULL, @occurredAt, @amount, @currency, @merchant, @category, @note, 1, 'confirmed', 'manual')`
    )
    .run({
      occurredAt: new Date(occurredAt || Date.now()).toISOString(),
      amount: value,
      currency: (currency || config.defaultCurrency).toUpperCase(),
      merchant,
      category: category || categorize({ merchant, amount: value }),
      note: note || null,
    });
  ok(res, db.prepare('SELECT * FROM transactions WHERE id = ?').get(info.lastInsertRowid));
});

const EDITABLE = new Set(['merchant', 'category', 'amount', 'currency', 'note', 'status', 'occurred_at']);

api.patch('/transactions/:id', (req, res) => {
  const updates = [];
  const params = { id: Number(req.params.id) };

  for (const [key, value] of Object.entries(req.body || {})) {
    const column = key === 'occurredAt' ? 'occurred_at' : key;
    if (!EDITABLE.has(column)) continue;
    updates.push(`${column} = @${column}`);
    params[column] = column === 'amount' ? Number(value) : value;
  }
  if (updates.length === 0) return fail(res, 400, 'לא נשלחו שדות לעדכון');

  const info = db
    .prepare(`UPDATE transactions SET ${updates.join(', ')}, updated_at = datetime('now') WHERE id = @id`)
    .run(params);
  if (info.changes === 0) return fail(res, 404, 'לא נמצא');
  ok(res, db.prepare('SELECT * FROM transactions WHERE id = ?').get(params.id));
});

api.delete('/transactions/:id', (req, res) => {
  const info = db.prepare('DELETE FROM transactions WHERE id = ?').run(Number(req.params.id));
  if (info.changes === 0) return fail(res, 404, 'לא נמצא');
  ok(res, { deleted: true });
});

api.get('/summary', (req, res) => {
  const months = Math.min(Number(req.query.months) || 12, 60);
  const since = new Date();
  since.setUTCMonth(since.getUTCMonth() - (months - 1), 1);
  since.setUTCHours(0, 0, 0, 0);
  const params = { since: since.toISOString() };

  const base = "FROM transactions WHERE status != 'ignored' AND occurred_at >= @since";

  const byMonth = db
    .prepare(
      `SELECT substr(occurred_at, 1, 7) AS month,
              ROUND(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2) AS spent,
              ROUND(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 2) AS refunded,
              COUNT(*) AS count
       ${base} GROUP BY month ORDER BY month`
    )
    .all(params);

  const byCategory = db
    .prepare(
      `SELECT category, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
       ${base} GROUP BY category HAVING total > 0 ORDER BY total DESC`
    )
    .all(params);

  const topMerchants = db
    .prepare(
      `SELECT merchant, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
       ${base} GROUP BY merchant HAVING total > 0 ORDER BY total DESC LIMIT 10`
    )
    .all(params);

  const startOfMonth = new Date();
  startOfMonth.setUTCDate(1);
  startOfMonth.setUTCHours(0, 0, 0, 0);
  const thisMonth = db
    .prepare(
      `SELECT ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
       FROM transactions WHERE status != 'ignored' AND occurred_at >= ?`
    )
    .get(startOfMonth.toISOString());

  const budgets = db.prepare('SELECT category, monthly_limit FROM budgets').all();

  ok(res, {
    currency: config.defaultCurrency,
    byMonth,
    byCategory,
    topMerchants,
    thisMonth: { total: thisMonth.total || 0, count: thisMonth.count || 0 },
    budgets,
  });
});

api.get('/rules', (req, res) => {
  ok(res, { rules: db.prepare('SELECT * FROM rules ORDER BY priority ASC, id ASC').all() });
});

api.post('/rules', (req, res) => {
  const { field = 'merchant', pattern, category, applyToExisting } = req.body || {};
  if (!pattern || !category) return fail(res, 400, 'נדרשים טקסט לזיהוי וקטגוריה');
  if (!['merchant', 'subject', 'sender'].includes(field)) return fail(res, 400, 'שדה לא חוקי');

  const info = db
    .prepare('INSERT INTO rules (field, pattern, category) VALUES (?, ?, ?)')
    .run(field, pattern, category);

  let updated = 0;
  if (applyToExisting) {
    const column = field === 'subject' ? 'subject' : field === 'sender' ? 'sender' : 'merchant';
    updated = db
      .prepare(`UPDATE transactions SET category = ?, updated_at = datetime('now') WHERE ${column} LIKE ?`)
      .run(category, `%${pattern}%`).changes;
  }
  ok(res, { rule: db.prepare('SELECT * FROM rules WHERE id = ?').get(info.lastInsertRowid), updated });
});

api.delete('/rules/:id', (req, res) => {
  db.prepare('DELETE FROM rules WHERE id = ?').run(Number(req.params.id));
  ok(res, { deleted: true });
});

api.put('/budgets', (req, res) => {
  const { category, monthlyLimit } = req.body || {};
  if (!category) return fail(res, 400, 'נדרשת קטגוריה');
  const limit = Number(monthlyLimit);
  if (!Number.isFinite(limit) || limit <= 0) {
    db.prepare('DELETE FROM budgets WHERE category = ?').run(category);
    return ok(res, { removed: true });
  }
  db.prepare(
    `INSERT INTO budgets (category, monthly_limit) VALUES (?, ?)
     ON CONFLICT(category) DO UPDATE SET monthly_limit = excluded.monthly_limit`
  ).run(category, limit);
  ok(res, { category, monthlyLimit: limit });
});

api.post('/sync', async (req, res) => {
  if (isSyncing()) return fail(res, 409, 'סנכרון כבר רץ');
  if (!connectionStatus().connected) return fail(res, 400, 'התיבה לא מחוברת');
  try {
    ok(res, await runSync());
  } catch (error) {
    fail(res, 500, error.message || 'הסנכרון נכשל');
  }
});

api.get('/export.csv', (req, res) => {
  const { where, params } = buildFilter(req.query);
  const rows = db
    .prepare(`SELECT occurred_at, amount, currency, merchant, category, account, note FROM transactions ${where} ORDER BY occurred_at DESC`)
    .all(params);

  const escape = (value) => {
    const text = value == null ? '' : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const header = ['תאריך', 'סכום', 'מטבע', 'בית עסק', 'קטגוריה', 'כרטיס', 'הערה'];
  const csv = [
    header.join(','),
    ...rows.map((r) =>
      [r.occurred_at.slice(0, 10), r.amount, r.currency, r.merchant, r.category, r.account, r.note]
        .map(escape)
        .join(',')
    ),
  ].join('\n');

  res.setHeader('Content-Type', 'text/csv; charset=utf-8');
  res.setHeader('Content-Disposition', 'attachment; filename="expenses.csv"');
  res.send(`﻿${csv}`); // BOM so Excel reads the Hebrew correctly
});
