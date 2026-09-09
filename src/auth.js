import crypto from 'node:crypto';
import { config } from './config.js';

const KEYLEN = 64;
const SCRYPT_PARAMS = { N: 16384, r: 8, p: 1, maxmem: 64 * 1024 * 1024 };

export function hashPassword(password) {
  const salt = crypto.randomBytes(16);
  const derived = crypto.scryptSync(password, salt, KEYLEN, SCRYPT_PARAMS);
  return `scrypt:${salt.toString('hex')}:${derived.toString('hex')}`;
}

export function verifyPassword(password, stored) {
  if (!stored) return false;
  const [scheme, saltHex, hashHex] = String(stored).split(':');
  if (scheme !== 'scrypt' || !saltHex || !hashHex) return false;
  const expected = Buffer.from(hashHex, 'hex');
  let derived;
  try {
    derived = crypto.scryptSync(password, Buffer.from(saltHex, 'hex'), expected.length, SCRYPT_PARAMS);
  } catch {
    return false;
  }
  return expected.length === derived.length && crypto.timingSafeEqual(expected, derived);
}

/** With no password configured the app is open — only sane on localhost. */
export function passwordRequired() {
  return Boolean(config.passwordHash);
}

export function requireAuth(req, res, next) {
  if (!passwordRequired() || req.session?.authenticated) return next();
  if (req.path.startsWith('/api/')) {
    return res.status(401).json({ error: 'נדרשת התחברות' });
  }
  return res.redirect('/login.html');
}
