/**
 * Minimal HTML -> text conversion. We do not need a full DOM: receipt emails are
 * table-heavy, so the goal is to keep the reading order and turn block edges into
 * newlines, which is what the amount/merchant extractors key off.
 */
const BLOCK_TAGS = /<\/?(?:p|div|tr|table|br|li|ul|ol|h[1-6]|section|header|footer|td|th)\b[^>]*>/gi;

const ENTITIES = {
  '&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"',
  '&#39;': "'", '&apos;': "'", '&shy;': '', '&ndash;': '-', '&mdash;': '-',
  '&euro;': '€', '&pound;': '£', '&yen;': '¥', '&#8362;': '₪', '&#x20aa;': '₪',
};

export function htmlToText(html) {
  if (!html) return '';
  return String(html)
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, ' ')
    .replace(BLOCK_TAGS, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x?[0-9a-f]+;|&[a-z]+;/gi, (m) => {
      const known = ENTITIES[m.toLowerCase()];
      if (known !== undefined) return known;
      const dec = /^&#(\d+);$/.exec(m);
      if (dec) return String.fromCodePoint(Number(dec[1]));
      const hex = /^&#x([0-9a-f]+);$/i.exec(m);
      if (hex) return String.fromCodePoint(Number.parseInt(hex[1], 16));
      return ' ';
    })
    .replace(/[ \t ‎‏]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/^[ \t]+|[ \t]+$/gm, '')
    .trim();
}
