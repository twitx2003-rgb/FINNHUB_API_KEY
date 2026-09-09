import { htmlToText } from '../util/html.js';
import { extractAmount } from './money.js';
import { identifySender } from './senders.js';

/** Phrases that mark a message as a real charge / receipt. */
const CHARGE_SIGNALS = [
  /חיוב|חויב|חוייב|נחייב|לחיוב/u,
  /קבלה|חשבונית|אישור תשלום|אישור הזמנה|אישור רכישה/u,
  /בוצעה עסקה|עסקה בכרטיס|בוצע תשלום|שולם בהצלחה|התשלום התקבל/u,
  /receipt|invoice|order confirmation|payment (?:receipt|confirmation|successful)/i,
  /you (?:paid|were charged)|we(?:'| ha)?ve charged|charged to your|thanks for your (?:order|purchase|payment)/i,
  /your (?:order|purchase|subscription).{0,30}(?:confirmed|complete|renewed)/i,
];

/** Phrases that mean "this is not a charge", even when a number is present. */
const NOT_CHARGE_SIGNALS = [
  /דיוור|ניוזלטר|הצטרפ|סקר שביעות|עדכון תנאי|מדיניות פרטיות/u,
  /מבצע|מבצעים|קופון|הנחה בלעדית|במחיר של|רק ב-|החל מ-|הזדמנות אחרונה/u,
  /עגלה נטושה|שכחת משהו|מוצרים שאולי יעניינו/u,
  /newsletter|unsubscribe from|sale ends|% off|limited time|abandoned cart|wish ?list/i,
  /price drop|deal of the|recommended for you|invite you|webinar/i,
  /דוח חודשי|פירוט חיובים לחודש|סיכום שנתי|לצפייה בפירוט/u, // statements, not single charges
  /monthly statement|your statement is ready/i,
];

/** A credit rather than a debit. */
const REFUND_SIGNALS = [
  /זיכוי|החזר כספי|בוטלה העסקה|ביטול עסקה|הוחזר לכרטיס/u,
  /refund(?:ed)?|money back|reversal|credited back|cancell?ed your order/i,
];

const MERCHANT_PATTERNS = [
  /בבית\s?העסק\s+([^\n,.;:()]{2,45})/u,
  /בית\s?העסק\s*[:\-]\s*([^\n,.;:()]{2,45})/u,
  /בבית עסק\s+([^\n,.;:()]{2,45})/u,
  /(?:אצל|בחנות|לטובת|עבור)\s+([^\n,.;:()]{2,45})/u,
  /שם\s?(?:העסק|הספק|בית העסק)\s*[:\-]\s*([^\n,.;:()]{2,45})/u,
  /(?:merchant|vendor|seller|sold by|business)\s*[:\-]\s*([^\n,.;:()]{2,45})/i,
  /(?:you paid|payment to|paid to|purchase (?:at|from)|order from)\s+([^\n,.;:()]{2,45})/i,
  /your (?:receipt|invoice) from\s+([^\n,.;:()]{2,45})/i,
];

const CARD_PATTERNS = [
  /(?:כרטיס(?:ך)?|בכרטיס)[^\n\d]{0,25}?(\d{4})(?![\d])/u,
  /(?:המסתיים|שספרותיו האחרונות|ספרות אחרונות)[^\d]{0,12}(\d{4})/u,
  /(?:ending(?: in)?|last 4 digits|card)[^\d\n]{0,14}(\d{4})(?!\d)/i,
  /[*x•]{2,}\s?(\d{4})(?!\d)/i,
];

const DATE_PATTERNS = [
  /(?:בתאריך|תאריך\s?(?:העסקה|החיוב)?|ביום)\s*[:\-]?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})/u,
  /(?:date|on)\s*[:\-]?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})/i,
];

const NOISE_MERCHANT = /^(?:the|a|an|your|our|של|את|זה|הזה|us|you|me|it)$/i;

function cleanMerchant(raw) {
  if (!raw) return null;
  let name = String(raw)
    .replace(/[‎‏]/g, '')
    .replace(/\s+/g, ' ')
    .replace(/^["'`\-–—\s]+|["'`\-–—\s]+$/g, '')
    .trim();
  // Drop a trailing amount that leaked in ("שופרסל 254.90 ₪").
  name = name.replace(/\s*[\d.,]+\s*[₪$€£]?\s*$/u, '').trim();
  if (name.length < 2 || name.length > 45) return null;
  if (NOISE_MERCHANT.test(name)) return null;
  if (!/[\p{L}]/u.test(name)) return null;
  return name;
}

function parseDayFirstDate(day, month, year, fallbackYear) {
  let y = Number(year);
  if (y < 100) y += y < 70 ? 2000 : 1900;
  if (!y) y = fallbackYear;
  const d = Number(day);
  const m = Number(month);
  if (d < 1 || d > 31 || m < 1 || m > 12) return null;
  const date = new Date(Date.UTC(y, m - 1, d, 12, 0, 0));
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Display name / address of the sender, used as merchant of last resort. */
function senderDisplayName(from) {
  if (!from) return null;
  const named = /^\s*"?([^"<]+?)"?\s*</.exec(from);
  if (named) return cleanMerchant(named[1]);
  const address = /([\w.+-]+)@([\w.-]+)/.exec(from);
  if (!address) return null;
  const domain = address[2].replace(/^(?:www|mail|email|no-?reply|info)\./i, '');
  const core = domain.split('.').filter((p) => !/^(com|co|il|net|org|io|inc)$/i.test(p))[0];
  return core ? core.charAt(0).toUpperCase() + core.slice(1) : null;
}

/**
 * Decide whether an email describes a single expense, and extract it.
 *
 * @param {object} message
 * @param {string} message.id        stable mailbox message id
 * @param {string} message.from      raw From header
 * @param {string} message.subject
 * @param {string} [message.text]    plain-text body
 * @param {string} [message.html]    html body (used when text is missing)
 * @param {Date|string} message.date message date header
 * @param {object} [options]
 * @param {string} [options.defaultCurrency]
 * @returns {{ok: true, transaction: object} | {ok: false, reason: string}}
 */
export function parseMessage(message, options = {}) {
  const defaultCurrency = options.defaultCurrency || 'ILS';
  const subject = message.subject || '';
  const body = message.text && message.text.trim() ? message.text : htmlToText(message.html);
  const haystack = `${subject}\n${body}`;
  const sender = identifySender(`${message.from || ''}`);

  const isRefund = REFUND_SIGNALS.some((re) => re.test(haystack));
  // A refund is a real transaction too, so it counts as a charge signal.
  const chargeHits =
    CHARGE_SIGNALS.filter((re) => re.test(haystack)).length + (isRefund ? 1 : 0);
  const notChargeHits = NOT_CHARGE_SIGNALS.filter((re) => re.test(haystack)).length;

  if (chargeHits === 0 && notChargeHits > 0) {
    return { ok: false, reason: 'marketing' };
  }
  if (chargeHits === 0 && !sender) {
    return { ok: false, reason: 'no-charge-signal' };
  }

  const money = extractAmount(haystack, defaultCurrency);
  if (!money) return { ok: false, reason: 'no-amount' };

  let merchant = null;
  for (const pattern of MERCHANT_PATTERNS) {
    const hit = pattern.exec(haystack);
    if (hit) {
      merchant = cleanMerchant(hit[1]);
      if (merchant) break;
    }
  }
  // A card issuer only forwards the charge, so its name is never the merchant.
  if (!merchant && sender && !sender.issuer) merchant = sender.label;
  if (!merchant) merchant = senderDisplayName(message.from) || sender?.label || 'לא ידוע';

  let account = null;
  for (const pattern of CARD_PATTERNS) {
    const hit = pattern.exec(haystack);
    if (hit) {
      account = hit[1];
      break;
    }
  }

  const messageDate = message.date ? new Date(message.date) : new Date();
  let occurredAt = Number.isNaN(messageDate.getTime()) ? new Date() : messageDate;
  for (const pattern of DATE_PATTERNS) {
    const hit = pattern.exec(haystack);
    if (hit) {
      const parsed = parseDayFirstDate(hit[1], hit[2], hit[3], occurredAt.getUTCFullYear());
      // Trust an in-body date only if it is close to when the mail arrived;
      // promo copy and footers are full of unrelated dates.
      if (parsed && Math.abs(parsed - occurredAt) < 90 * 24 * 3600 * 1000) {
        occurredAt = parsed;
        break;
      }
    }
  }

  let confidence = 0.3;
  if (chargeHits > 0) confidence += 0.2 + Math.min(chargeHits - 1, 2) * 0.05;
  if (money.confident) confidence += 0.2;
  if (sender) confidence += 0.1;
  if (account) confidence += 0.1;
  if (merchant !== 'לא ידוע') confidence += 0.05;
  if (notChargeHits > 0) confidence -= 0.15;
  confidence = Math.max(0, Math.min(1, Number(confidence.toFixed(2))));

  return {
    ok: true,
    transaction: {
      messageId: message.id,
      occurredAt: occurredAt.toISOString(),
      amount: isRefund ? -Math.abs(money.amount) : Math.abs(money.amount),
      currency: money.currency || defaultCurrency,
      merchant,
      account,
      sender: message.from || null,
      subject,
      confidence,
      status: confidence >= 0.6 ? 'confirmed' : 'review',
      isRefund,
      issuer: sender?.issuer ? sender.label : null,
    },
  };
}
