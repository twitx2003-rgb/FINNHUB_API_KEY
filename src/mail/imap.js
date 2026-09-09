import { ImapFlow } from 'imapflow';
import { simpleParser } from 'mailparser';
import { config } from '../config.js';

/**
 * IMAP is the fallback path for people who do not want to create a Google Cloud
 * project: any mailbox plus an app password works. IMAP search cannot express
 * the keyword filter Gmail search can, so we pull everything since `since` and
 * let the parser gate decide.
 */
export async function* iterateMessages({ since, max = 2000 }) {
  const client = new ImapFlow({
    host: config.imap.host,
    port: config.imap.port,
    secure: config.imap.port === 993,
    auth: { user: config.imap.user, pass: config.imap.password },
    logger: false,
  });

  await client.connect();
  const lock = await client.getMailboxLock(config.imap.mailbox);
  try {
    const uids = await client.search({ since }, { uid: true });
    // Newest first, so a capped run still covers the most relevant window.
    const selected = (uids || []).slice(-max).reverse();

    for (const uid of selected) {
      const message = await client.fetchOne(String(uid), { source: true, envelope: true }, { uid: true });
      if (!message?.source) continue;
      const parsed = await simpleParser(message.source);
      yield {
        id: `imap:${config.imap.user}:${parsed.messageId || uid}`,
        from: parsed.from?.text || '',
        subject: parsed.subject || '',
        text: parsed.text || '',
        html: parsed.html || '',
        date: parsed.date || message.envelope?.date || new Date(),
      };
    }
  } finally {
    lock.release();
    await client.logout().catch(() => {});
  }
}

export function isConnected() {
  return Boolean(config.imap.user && config.imap.password);
}

export function getConnectedAccount() {
  return config.imap.user || null;
}
