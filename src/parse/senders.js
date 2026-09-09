/**
 * Known senders. `match` is tested against the From address and display name.
 * `hints` raise the confidence that a message is a real charge, and `merchant`
 * pins the merchant when the issuer *is* the merchant (utilities, telecom).
 */
export const KNOWN_SENDERS = [
  // Israeli credit-card issuers — the issuer is the messenger, not the merchant.
  { id: 'isracard', match: /isracard|ישראכרט|premium\.co\.il/i, label: 'ישראכרט', issuer: true },
  { id: 'cal', match: /cal-online|calpay|כאל|visacal/i, label: 'כאל', issuer: true },
  { id: 'max', match: /max\.co\.il|leumi-card|מקס איט|מקס/i, label: 'מקס', issuer: true },
  { id: 'amex', match: /americanexpress|amex/i, label: 'אמריקן אקספרס', issuer: true },

  // Banks
  { id: 'leumi', match: /bankleumi|leumi\.co\.il|לאומי/i, label: 'בנק לאומי', issuer: true },
  { id: 'poalim', match: /bankhapoalim|poalim|הפועלים/i, label: 'בנק הפועלים', issuer: true },
  { id: 'discount', match: /discountbank|דיסקונט/i, label: 'בנק דיסקונט', issuer: true },
  { id: 'mizrahi', match: /mizrahi|טפחות/i, label: 'מזרחי טפחות', issuer: true },
  { id: 'onezero', match: /onezero/i, label: 'ONE ZERO', issuer: true },

  // Wallets / P2P
  { id: 'bit', match: /\bbit\b|bitpay\.co\.il/i, label: 'bit', issuer: true },
  { id: 'paybox', match: /paybox/i, label: 'PayBox', issuer: true },
  { id: 'paypal', match: /paypal/i, label: 'PayPal', issuer: true },

  // Merchants that bill directly.
  { id: 'google', match: /google.*(play|payments)|payments-noreply@google/i, label: 'Google' },
  { id: 'apple', match: /apple\.com/i, label: 'Apple' },
  { id: 'amazon', match: /amazon\./i, label: 'Amazon' },
  { id: 'ebay', match: /ebay\./i, label: 'eBay' },
  { id: 'aliexpress', match: /aliexpress/i, label: 'AliExpress' },
  { id: 'netflix', match: /netflix/i, label: 'Netflix' },
  { id: 'spotify', match: /spotify/i, label: 'Spotify' },
  { id: 'wolt', match: /wolt/i, label: 'Wolt' },
  { id: 'tenbis', match: /10bis|tenbis|תן ביס/i, label: 'תן ביס' },
  { id: 'shufersal', match: /shufersal|שופרסל/i, label: 'שופרסל' },
  { id: 'ramilevy', match: /rami-?levy|רמי לוי/i, label: 'רמי לוי' },
  { id: 'ubereats', match: /uber/i, label: 'Uber' },
  { id: 'booking', match: /booking\.com/i, label: 'Booking.com' },
  { id: 'airbnb', match: /airbnb/i, label: 'Airbnb' },
  { id: 'partner', match: /partner\.co\.il|פרטנר/i, label: 'פרטנר' },
  { id: 'cellcom', match: /cellcom|סלקום/i, label: 'סלקום' },
  { id: 'pelephone', match: /pelephone|פלאפון/i, label: 'פלאפון' },
  { id: 'hot', match: /hot\.net\.il/i, label: 'HOT' },
  { id: 'bezeq', match: /bezeq|בזק/i, label: 'בזק' },
  { id: 'iec', match: /iec\.co\.il|חברת החשמל/i, label: 'חברת החשמל' },
];

export function identifySender(from) {
  if (!from) return null;
  return KNOWN_SENDERS.find((s) => s.match.test(from)) || null;
}
