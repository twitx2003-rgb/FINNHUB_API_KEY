import crypto from 'node:crypto';
import express from 'express';
import { config } from '../config.js';
import * as gmail from '../mail/gmail.js';

export const oauth = express.Router();

oauth.get('/start', (req, res) => {
  if (config.mailProvider !== 'gmail') {
    return res.status(400).send('MAIL_PROVIDER אינו gmail — אין צורך בהתחברות OAuth.');
  }
  if (!config.google.clientId || !config.google.clientSecret) {
    return res
      .status(400)
      .send('חסרים GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET בקובץ .env. ראה README.');
  }
  const state = crypto.randomBytes(16).toString('hex');
  req.session.oauthState = state;
  res.redirect(gmail.buildAuthUrl(state));
});

oauth.get('/callback', async (req, res) => {
  const { code, state, error } = req.query;
  if (error) return res.redirect(`/?connected=0&reason=${encodeURIComponent(String(error))}`);
  if (!code || !state || state !== req.session.oauthState) {
    return res.redirect('/?connected=0&reason=state');
  }
  delete req.session.oauthState;

  try {
    await gmail.exchangeCode(String(code));
    res.redirect('/?connected=1');
  } catch (err) {
    res.redirect(`/?connected=0&reason=${encodeURIComponent(err.message.slice(0, 120))}`);
  }
});

oauth.post('/disconnect', (req, res) => {
  gmail.disconnect();
  res.json({ disconnected: true });
});
