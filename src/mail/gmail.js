import { config } from '../config.js';
import { getJsonSetting, setJsonSetting, setSetting } from '../db.js';

const AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth';
const TOKEN_URL = 'https://oauth2.googleapis.com/token';
const API = 'https://gmail.googleapis.com/gmail/v1/users/me';
const SCOPE = 'https://www.googleapis.com/auth/gmail.readonly';
const TOKENS_KEY = 'google_tokens';

export function isConnected() {
  return Boolean(getJsonSetting(TOKENS_KEY)?.refresh_token);
}

export function getConnectedAccount() {
  return getJsonSetting(TOKENS_KEY)?.email || null;
}

export function buildAuthUrl(state) {
  const params = new URLSearchParams({
    client_id: config.google.clientId,
    redirect_uri: config.google.redirectUri,
    response_type: 'code',
    scope: SCOPE,
    access_type: 'offline',
    prompt: 'consent', // force a refresh_token on re-consent
    include_granted_scopes: 'true',
    state,
  });
  return `${AUTH_URL}?${params}`;
}

async function postToken(body) {
  const response = await fetch(TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`Google token request failed (${response.status}): ${data.error_description || data.error || 'unknown error'}`);
  }
  return data;
}

export async function exchangeCode(code) {
  const data = await postToken({
    code,
    client_id: config.google.clientId,
    client_secret: config.google.clientSecret,
    redirect_uri: config.google.redirectUri,
    grant_type: 'authorization_code',
  });

  const tokens = {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Date.now() + (data.expires_in ?? 3600) * 1000,
  };
  setJsonSetting(TOKENS_KEY, tokens);

  const profile = await apiGet('/profile').catch(() => null);
  if (profile?.emailAddress) {
    setJsonSetting(TOKENS_KEY, { ...tokens, email: profile.emailAddress });
  }
  return tokens;
}

export function disconnect() {
  setSetting(TOKENS_KEY, null);
}

async function getAccessToken() {
  const tokens = getJsonSetting(TOKENS_KEY);
  if (!tokens?.refresh_token) {
    throw new Error('החשבון לא מחובר. יש להתחבר דרך "חיבור לגוגל".');
  }
  if (tokens.access_token && tokens.expires_at > Date.now() + 60_000) {
    return tokens.access_token;
  }
  const data = await postToken({
    refresh_token: tokens.refresh_token,
    client_id: config.google.clientId,
    client_secret: config.google.clientSecret,
    grant_type: 'refresh_token',
  });
  const refreshed = {
    ...tokens,
    access_token: data.access_token,
    expires_at: Date.now() + (data.expires_in ?? 3600) * 1000,
  };
  setJsonSetting(TOKENS_KEY, refreshed);
  return refreshed.access_token;
}

async function apiGet(path, params) {
  const token = await getAccessToken();
  const url = new URL(API + path);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
  }
  const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`Gmail API ${response.status} on ${path}: ${detail.slice(0, 300)}`);
  }
  return response.json();
}

function decodeBase64Url(data) {
  if (!data) return '';
  return Buffer.from(data.replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
}

/** Walk the MIME tree and pull out the first text/plain and text/html parts. */
function collectBodies(part, out = { text: '', html: '' }) {
  if (!part) return out;
  const mime = part.mimeType || '';
  if (mime === 'text/plain' && !out.text) out.text = decodeBase64Url(part.body?.data);
  else if (mime === 'text/html' && !out.html) out.html = decodeBase64Url(part.body?.data);
  for (const child of part.parts || []) collectBodies(child, out);
  return out;
}

function headerValue(headers, name) {
  const hit = (headers || []).find((h) => h.name.toLowerCase() === name.toLowerCase());
  return hit ? hit.value : '';
}

export async function* listMessageIds(query, { max = 1000 } = {}) {
  let pageToken;
  let yielded = 0;
  do {
    const page = await apiGet('/messages', {
      q: query,
      maxResults: Math.min(100, max - yielded),
      pageToken,
    });
    for (const message of page.messages || []) {
      yield message.id;
      yielded += 1;
      if (yielded >= max) return;
    }
    pageToken = page.nextPageToken;
  } while (pageToken);
}

export async function fetchMessage(id) {
  const raw = await apiGet(`/messages/${id}`, { format: 'full' });
  const headers = raw.payload?.headers;
  const bodies = collectBodies(raw.payload);
  return {
    id: `gmail:${raw.id}`,
    from: headerValue(headers, 'From'),
    subject: headerValue(headers, 'Subject'),
    text: bodies.text,
    html: bodies.html,
    date: raw.internalDate ? new Date(Number(raw.internalDate)) : new Date(headerValue(headers, 'Date')),
  };
}
