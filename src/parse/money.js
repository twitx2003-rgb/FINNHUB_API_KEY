/**
 * Amount + currency extraction.
 *
 * Receipt emails routinely contain several numbers (VAT, shipping, loyalty
 * points, an old balance). We therefore collect every "money mention" and score
 * it by the words around it, weighted by distance, instead of trusting the
 * first or the largest match.
 *
 * Note on boundaries: `\b` is useless next to Hebrew, because JS `\w` is ASCII
 * only and every Hebrew letter counts as a non-word character. We use explicit
 * Unicode lookarounds instead.
 */

export const CURRENCY_BY_SYMBOL = {
  '₪': 'ILS', $: 'USD', '€': 'EUR', '£': 'GBP', '¥': 'JPY', '₩': 'KRW', '₹': 'INR',
};

const CURRENCY_WORDS = [
  [/ש"?ח|שקלים|שקל|ils|nis/iu, 'ILS'],
  [/usd|דולר|us\$/iu, 'USD'],
  [/eur|אירו|יורו/iu, 'EUR'],
  [/gbp|ליש"?ט/iu, 'GBP'],
  [/jpy|ין/iu, 'JPY'],
];

const SYMBOLS = '₪$€£¥₩₹';
const NUM = String.raw`\d[\d.,   ]{0,15}\d|\d`;
const WORDS = String.raw`ש"ח|ש״ח|שקלים|שקל|ILS|NIS|USD|EUR|GBP`;
const NOT_ALNUM_BEFORE = String.raw`(?<![\p{L}\p{N}])`;
const NOT_ALNUM_AFTER = String.raw`(?![\p{L}\p{N}])`;

// The symbol/word can sit on either side of the number — both happen in Hebrew mail.
const MONEY_PATTERNS = [
  new RegExp(String.raw`([${SYMBOLS}])\s?(${NUM})`, 'gu'),
  new RegExp(String.raw`(${NUM})\s?([${SYMBOLS}])`, 'gu'),
  new RegExp(String.raw`(${NUM})\s?(${WORDS})${NOT_ALNUM_AFTER}`, 'giu'),
  new RegExp(String.raw`${NOT_ALNUM_BEFORE}(${WORDS})\s?(${NUM})`, 'giu'),
];

const POSITIVE_CONTEXT = [
  [/סכום\s*(?:העסקה|החיוב|לתשלום)?/iu, 6],
  [/סה"?כ|סה״כ|סך הכל|סך של|לתשלום|לחיוב/iu, 6],
  [/חויב|חוייב|נחייב|חיוב|שולם|תשלום|עסקה|רכישה/iu, 4],
  [/total|amount|charged|you paid|payment of|paid|receipt for/iu, 6],
  [/subtotal|price|item/iu, 2],
];

const NEGATIVE_CONTEXT = [
  [/יתרה|יתרת|מסגרת|נקוד|נקודות|הנחה|חסכת|קופון|מתנה|שובר|צבירה/iu, -8],
  [/points|balance|discount|saved|coupon|voucher|reward|credit limit|you save/iu, -8],
  [/מע"?מ|מע״מ|משלוח|דמי משלוח|עמלה/iu, -4],
  [/vat|tax|shipping|delivery fee|fee(?![a-z])/iu, -4],
  [/מתוך|לחודש|החל מ/iu, -3],
  [/out of|per month|starting at|from only/iu, -3],
];

const ALL_CONTEXT = [...POSITIVE_CONTEXT, ...NEGATIVE_CONTEXT];

/**
 * Turn a localised number string into a Number.
 * Handles 1,234.56 / 1.234,56 / 1 234,56 / 1234 / 12,5
 */
export function normalizeAmount(raw) {
  if (raw == null) return null;
  let s = String(raw).replace(/[\s  ‎‏]/gu, '');
  if (!/\d/.test(s)) return null;

  const lastComma = s.lastIndexOf(',');
  const lastDot = s.lastIndexOf('.');

  if (lastComma !== -1 && lastDot !== -1) {
    // Whichever separator comes last is the decimal separator.
    const decimalSep = lastComma > lastDot ? ',' : '.';
    const thousandSep = decimalSep === ',' ? '.' : ',';
    s = s.split(thousandSep).join('').replace(decimalSep, '.');
  } else if (lastComma !== -1) {
    s = /^\d{1,3}(,\d{3})+$/.test(s) ? s.split(',').join('') : s.replace(',', '.');
  } else if (lastDot !== -1) {
    if (/^\d{1,3}(\.\d{3})+$/.test(s)) s = s.split('.').join('');
  }

  const value = Number.parseFloat(s);
  return Number.isFinite(value) ? value : null;
}

function detectCurrencyWord(token) {
  for (const [re, code] of CURRENCY_WORDS) {
    if (re.test(token)) return code;
  }
  return null;
}

/**
 * Words right before the amount describe it; words further away or after it are
 * weaker evidence. Without the distance weighting, a nearby "balance" label and
 * a far-away "total" label cancel out and the wrong number wins.
 */
function scoreContext(text, index, length) {
  const near = text.slice(Math.max(0, index - 22), index);
  const far = text.slice(Math.max(0, index - 55), Math.max(0, index - 22));
  const after = text.slice(index + length, index + length + 18);

  let score = 0;
  for (const [re, weight] of ALL_CONTEXT) {
    if (re.test(near)) score += weight;
    else if (re.test(far)) score += weight * 0.4;
    if (re.test(after)) score += weight * 0.25;
  }
  // A label that ends the line immediately before the figure is the strongest cue.
  if (/(?:סה"?כ|סה״כ|סך הכל|לתשלום|סכום|total)[^\n]{0,14}$/iu.test(near)) score += 5;
  return score;
}

/**
 * @returns {Array<{amount:number, currency:string|null, score:number, index:number, text:string}>}
 *          sorted best-first.
 */
export function findMoneyMentions(text, defaultCurrency = null) {
  if (!text) return [];
  const found = new Map(); // start index -> best mention at that position

  for (const pattern of MONEY_PATTERNS) {
    pattern.lastIndex = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const [full, a, b] = match;
      const aIsNumber = /^\s*[\d]/.test(a);
      const numberPart = aIsNumber ? a : b;
      const currencyPart = aIsNumber ? b : a;

      const amount = normalizeAmount(numberPart);
      if (amount === null || amount <= 0 || amount > 5_000_000) continue;

      const mention = {
        amount,
        currency:
          CURRENCY_BY_SYMBOL[currencyPart] || detectCurrencyWord(currencyPart) || defaultCurrency,
        index: match.index,
        text: full.trim(),
        score: scoreContext(text, match.index, full.length),
      };
      const existing = found.get(match.index);
      if (!existing || existing.score < mention.score) found.set(match.index, mention);
    }
  }

  return [...found.values()].sort((x, y) => y.score - x.score || y.amount - x.amount);
}

/** Best single amount for a message, or null. */
export function extractAmount(text, defaultCurrency = null) {
  const mentions = findMoneyMentions(text, defaultCurrency);
  if (mentions.length === 0) return null;

  const best = mentions[0];
  if (best.score <= 0) {
    // Nothing was labelled: fall back to the largest figure, since receipts put
    // the total last and it is almost always the biggest number on the page.
    const largest = [...mentions].sort((x, y) => y.amount - x.amount)[0];
    return { ...largest, confident: false };
  }
  return { ...best, confident: best.score >= 4 };
}
