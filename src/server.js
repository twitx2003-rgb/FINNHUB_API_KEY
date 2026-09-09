import path from 'node:path';
import express from 'express';
import session from 'express-session';
import { config, isConfigured } from './config.js';
import { requireAuth, verifyPassword, passwordRequired } from './auth.js';
import { api } from './routes/api.js';
import { oauth } from './routes/oauth.js';
import { runSync, connectionStatus, isSyncing } from './mail/sync.js';

const app = express();
app.set('trust proxy', 1);
app.use(express.json({ limit: '256kb' }));
app.use(
  session({
    name: 'expenses.sid',
    secret: config.sessionSecret,
    resave: false,
    saveUninitialized: false,
    cookie: { httpOnly: true, sameSite: 'lax', maxAge: 30 * 24 * 3600 * 1000 },
  })
);

// --- session endpoints (must stay outside requireAuth) ---
app.post('/api/login', (req, res) => {
  if (!passwordRequired()) {
    req.session.authenticated = true;
    return res.json({ ok: true });
  }
  if (!verifyPassword(String(req.body?.password || ''), config.passwordHash)) {
    return res.status(401).json({ error: 'סיסמה שגויה' });
  }
  req.session.authenticated = true;
  res.json({ ok: true });
});

app.post('/api/logout', (req, res) => {
  req.session.destroy(() => res.json({ ok: true }));
});

app.get('/api/session', (req, res) => {
  res.json({
    authenticated: !passwordRequired() || Boolean(req.session?.authenticated),
    passwordRequired: passwordRequired(),
    configured: isConfigured(),
    provider: config.mailProvider,
  });
});

app.get('/login.html', (req, res) =>
  res.sendFile(path.join(config.rootDir, 'public', 'login.html'))
);

// The login page needs its stylesheet before a session exists, so this one asset
// is served ahead of the auth gate. Everything else stays behind it.
app.get('/styles.css', (req, res) =>
  res.sendFile(path.join(config.rootDir, 'public', 'styles.css'))
);

// --- everything below needs a session ---
app.use(requireAuth);
app.use('/api', api);
app.use('/oauth', oauth);
app.use(express.static(path.join(config.rootDir, 'public')));

app.use((err, req, res, next) => { // eslint-disable-line no-unused-vars
  console.error('[error]', err);
  res.status(500).json({ error: err.message || 'שגיאת שרת' });
});

const server = app.listen(config.port, () => {
  console.log(`\n  מעקב הוצאות רץ על  http://localhost:${config.port}`);
  if (!isConfigured()) {
    console.log('  ⚠ התיבה עוד לא מוגדרת — העתק .env.example ל-.env ומלא את הפרטים (ראה README).');
  } else if (!connectionStatus().connected) {
    console.log('  ⚠ יש להתחבר לחשבון הדואר דרך הכפתור באתר.');
  }
  if (!passwordRequired()) {
    console.log('  ⚠ לא הוגדרה סיסמה — האתר פתוח. הרץ `npm run set-password`.');
  }
  console.log('');
});

if (config.syncIntervalMinutes > 0) {
  const interval = config.syncIntervalMinutes * 60 * 1000;
  setInterval(async () => {
    if (isSyncing() || !connectionStatus().connected) return;
    try {
      const stats = await runSync();
      if (stats.imported > 0) {
        console.log(`[sync] יובאו ${stats.imported} הוצאות חדשות`);
      }
    } catch (error) {
      console.error('[sync] נכשל:', error.message);
    }
  }, interval).unref();
}

const shutdown = () => server.close(() => process.exit(0));
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

export { app, server };
