import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeAmount, extractAmount, findMoneyMentions } from '../src/parse/money.js';
import { parseMessage } from '../src/parse/parser.js';
import { categorize } from '../src/parse/categories.js';
import { htmlToText } from '../src/util/html.js';

test('normalizeAmount handles locale separators', () => {
  assert.equal(normalizeAmount('1,234.56'), 1234.56);
  assert.equal(normalizeAmount('1.234,56'), 1234.56);
  assert.equal(normalizeAmount('1 234,50'), 1234.5);
  assert.equal(normalizeAmount('12,5'), 12.5);
  assert.equal(normalizeAmount('1.234'), 1234);
  assert.equal(normalizeAmount('99'), 99);
  assert.equal(normalizeAmount('abc'), null);
});

test('extractAmount prefers the labelled total over a balance', () => {
  const text = 'סכום העסקה: 254.90 ₪\nיתרת מסגרת: 12,000 ₪';
  const result = extractAmount(text, 'ILS');
  assert.equal(result.amount, 254.9);
  assert.equal(result.currency, 'ILS');
  assert.ok(result.confident);
});

test('extractAmount prefers the order total over tax and shipping', () => {
  const result = extractAmount('Shipping: $4.99\nTax: $3.20\nOrder Total: $49.99', 'ILS');
  assert.equal(result.amount, 49.99);
  assert.equal(result.currency, 'USD');
});

test('shekel word amounts are matched despite Hebrew word boundaries', () => {
  const mentions = findMoneyMentions('סה"כ לתשלום 1,240.50 ש"ח', 'ILS');
  assert.equal(mentions[0].amount, 1240.5);
  assert.equal(mentions[0].currency, 'ILS');
});

test('loyalty points are not read as money', () => {
  assert.equal(extractAmount('צברת 1,500 נקודות! יתרת הנקודות: 8,000', 'ILS'), null);
});

test('htmlToText keeps table cells apart', () => {
  const text = htmlToText('<table><tr><td>סה"כ</td><td>112.50&nbsp;&#8362;</td></tr></table>');
  assert.match(text, /סה"כ/);
  assert.match(text, /112\.50 ₪/);
});

test('parses an Israeli credit-card charge notification', () => {
  const result = parseMessage(
    {
      id: 'm1',
      from: '"ישראכרט" <noreply@isracard.co.il>',
      subject: 'הודעה על חיוב בכרטיס אשראי',
      text: 'בוצעה עסקה בכרטיס המסתיים ב-4821\nבבית העסק שופרסל דיל\nסכום העסקה: 254.90 ₪\nבתאריך 12/08/2026\nיתרת מסגרת: 12,000 ₪',
      date: '2026-08-12T10:00:00Z',
    },
    { defaultCurrency: 'ILS' }
  );

  assert.ok(result.ok);
  const tx = result.transaction;
  assert.equal(tx.amount, 254.9);
  assert.equal(tx.currency, 'ILS');
  assert.equal(tx.merchant, 'שופרסל דיל');
  assert.equal(tx.account, '4821');
  assert.equal(tx.occurredAt.slice(0, 10), '2026-08-12');
  assert.equal(tx.status, 'confirmed');
  assert.equal(tx.issuer, 'ישראכרט', 'the card issuer is recorded but is not the merchant');
  assert.equal(categorize(tx), 'מזון וסופר');
});

test('rejects marketing mail that merely mentions a price', () => {
  const result = parseMessage({
    id: 'm2',
    from: 'Zara <news@zara.com>',
    subject: 'מבצע סוף עונה - עד 50% הנחה!',
    text: 'הזדמנות אחרונה! מוצרים החל מ-49.90 ₪',
    date: '2026-08-15T09:00:00Z',
  });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'marketing');
});

test('a refund becomes a negative amount', () => {
  const result = parseMessage({
    id: 'm3',
    from: '"כאל" <noreply@cal-online.co.il>',
    subject: 'זיכוי בכרטיס',
    text: 'בוצע זיכוי בסך 89.90 ש"ח בבית העסק קסטרו',
    date: '2026-08-20T09:00:00Z',
  });
  assert.ok(result.ok);
  assert.equal(result.transaction.amount, -89.9);
  assert.ok(result.transaction.isRefund);
  assert.equal(categorize(result.transaction), 'החזר');
});

test('reads an html-only receipt', () => {
  const result = parseMessage({
    id: 'm4',
    from: 'Wolt <noreply@wolt.com>',
    subject: 'קבלה על הזמנתך',
    html: '<div>תודה!</div><table><tr><td>סה"כ לתשלום</td><td>112.50 ₪</td></tr></table>',
    date: '2026-08-21T19:00:00Z',
  });
  assert.ok(result.ok);
  assert.equal(result.transaction.amount, 112.5);
  assert.equal(categorize(result.transaction), 'מסעדות ובתי קפה');
});

test('an English receipt yields merchant and foreign currency', () => {
  const result = parseMessage({
    id: 'm5',
    from: 'PayPal <service@paypal.com>',
    subject: 'Receipt for your payment to Spotify',
    text: 'You paid €10,99 to Spotify AB',
    date: '2026-08-01T09:00:00Z',
  });
  assert.ok(result.ok);
  assert.equal(result.transaction.amount, 10.99);
  assert.equal(result.transaction.currency, 'EUR');
  assert.match(result.transaction.merchant, /Spotify/);
});

test('a message with no amount is skipped', () => {
  const result = parseMessage({
    id: 'm6',
    from: 'Bank <noreply@bankleumi.co.il>',
    subject: 'אישור הזמנה',
    text: 'ההזמנה שלך התקבלה ותטופל בקרוב.',
    date: '2026-08-02T09:00:00Z',
  });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'no-amount');
});

test('an out-of-range in-body date is ignored in favour of the mail date', () => {
  const result = parseMessage({
    id: 'm7',
    from: 'Shop <noreply@shop.com>',
    subject: 'Receipt',
    text: 'Total: $20.00\nOur terms updated on 01/01/2019.',
    date: '2026-08-02T09:00:00Z',
  });
  assert.ok(result.ok);
  assert.equal(result.transaction.occurredAt.slice(0, 4), '2026');
});

test('user rules beat the built-in keyword rules', () => {
  const tx = { merchant: 'שופרסל דיל', subject: '', amount: 100 };
  assert.equal(categorize(tx), 'מזון וסופר');
  assert.equal(
    categorize(tx, [{ field: 'merchant', pattern: 'שופרסל', category: 'דיור' }]),
    'דיור'
  );
});
