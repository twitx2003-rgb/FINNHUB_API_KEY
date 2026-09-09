#!/usr/bin/env node
import { runSync, connectionStatus } from '../mail/sync.js';

const status = connectionStatus();
if (!status.connected) {
  console.error(`התיבה לא מחוברת (provider=${status.provider}). ראה README.`);
  process.exit(1);
}

console.log(`מסנכרן מ-${status.account}...`);
try {
  const stats = await runSync({
    onProgress: (s) => process.stdout.write(`\r  נסרקו ${s.scanned}, יובאו ${s.imported}`),
  });
  console.log(`\nהסתיים: נסרקו ${stats.scanned}, יובאו ${stats.imported}, דולגו ${stats.skipped}.`);
  process.exit(0);
} catch (error) {
  console.error('\nהסנכרון נכשל:', error.message);
  process.exit(1);
}
