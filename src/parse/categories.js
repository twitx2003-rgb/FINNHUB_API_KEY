export const CATEGORIES = [
  'מזון וסופר',
  'מסעדות ובתי קפה',
  'תחבורה ודלק',
  'קניות',
  'בילויים ופנאי',
  'בריאות',
  'חשבונות ותשתיות',
  'תקשורת',
  'מנויים ודיגיטל',
  'דיור',
  'חינוך',
  'נסיעות וחופשות',
  'ביטוח',
  'העברות',
  'החזר',
  'אחר',
];

/** Built-in keyword rules, checked in order against merchant + subject. */
export const BUILTIN_RULES = [
  ['מזון וסופר', /שופרסל|רמי לוי|ויקטורי|יינות ביתן|טיב טעם|אושר עד|מגה|יוחננוף|am:?pm|סופר|מכולת|קצביה|supermarket|grocer/iu],
  ['מסעדות ובתי קפה', /wolt|וולט|10bis|תן ביס|מסעד|פיצה|קפה|בורגר|סושי|ארומה|קפולסקי|לנדוור|מקדונלד|kfc|domino|restaurant|cafe|coffee|bakery|בית קפה|פלאפל|חומוס/iu],
  ['תחבורה ודלק', /פז|דלק|סונול|דור אלון|ten\b|רב קו|רב-קו|מוניות|gett|uber|יאנגו|yango|בזק חניה|פנגו|pango|cellopark|סלופארק|חניון|רכבת|אגד|דן|מטרופולין|parking|fuel|gas station|toll|כביש 6/iu],
  ['קניות', /amazon|aliexpress|ebay|shein|asos|zara|castro|fox|terminal x|golf|ikea|איקאה|ace|הום סנטר|עזריאלי|קסטרו|רנואר|shop|store|בגדים|נעליים/iu],
  ['בילויים ופנאי', /סינמה|יס פלאנט|רב חן|הוט סינמה|cinema|theater|תיאטרון|הופעה|כרטיס|ticket|מוזיאון|בריכה|כושר|holmes place|גו אקטיב|icon|קאנטרי|gym|spotify|netflix|disney|hbo/iu],
  ['בריאות', /סופרפארם|super-?pharm|be phar|ניו פארם|clalit|כללית|מכבי|מאוחדת|לאומית|בית מרקחת|pharmacy|רופא|שיניים|dental|clinic|מרפאה|אופטיק/iu],
  ['חשבונות ותשתיות', /חברת החשמל|iec|מקורות|מים|תאגיד|ארנונה|עיריית|גז|supergas|פזגז|amisragas|electric|water bill/iu],
  ['תקשורת', /פרטנר|partner|סלקום|cellcom|פלאפון|pelephone|hot\b|בזק|bezeq|golan|רמי לוי תקשורת|012|019|yes\b|internet|isp/iu],
  ['מנויים ודיגיטל', /google|apple|microsoft|adobe|openai|anthropic|github|dropbox|icloud|youtube|subscription|מנוי|חידוש מנוי|domain|hosting|aws|azure/iu],
  ['דיור', /שכר דירה|שכירות|ועד בית|משכנתא|rent\b|mortgage|hoa/iu],
  ['חינוך', /גן ילדים|בית ספר|אוניברסיט|מכללה|קורס|צהרון|חוג|tuition|course|udemy|coursera/iu],
  ['נסיעות וחופשות', /booking|airbnb|expedia|טיסה|אל על|el al|wizz|ryanair|israir|arkia|מלון|hotel|hostel|flight|נופש|צימר|rent ?a ?car|הרץ|hertz|avis|sixt/iu],
  ['ביטוח', /ביטוח|הראל|כלל ביטוח|מגדל|הפניקס|מנורה|ayalon|איילון|insurance/iu],
  ['העברות', /\bbit\b|paybox|העברה|העברת כספים|paypal.*(?:sent|שלחת)|transfer to|zelle|venmo/iu],
];

/**
 * @param {{merchant?:string, subject?:string, sender?:string, amount?:number}} tx
 * @param {Array<{field:string, pattern:string, category:string}>} userRules highest priority first
 */
export function categorize(tx, userRules = []) {
  const fields = {
    merchant: tx.merchant || '',
    subject: tx.subject || '',
    sender: tx.sender || '',
  };

  for (const rule of userRules) {
    const haystack = fields[rule.field] ?? fields.merchant;
    if (haystack && haystack.toLowerCase().includes(String(rule.pattern).toLowerCase())) {
      return rule.category;
    }
  }

  if (typeof tx.amount === 'number' && tx.amount < 0) return 'החזר';

  const combined = `${fields.merchant} ${fields.subject} ${fields.sender}`;
  for (const [category, pattern] of BUILTIN_RULES) {
    if (pattern.test(combined)) return category;
  }
  return 'אחר';
}
