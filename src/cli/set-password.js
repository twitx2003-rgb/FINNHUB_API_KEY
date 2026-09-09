#!/usr/bin/env node
import readline from 'node:readline';
import { hashPassword } from '../auth.js';

const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

function ask(question) {
  return new Promise((resolve) => rl.question(question, resolve));
}

const password = (await ask('בחר סיסמה לאתר / Choose a site password: ')).trim();
rl.close();

if (password.length < 6) {
  console.error('הסיסמה חייבת להיות באורך 6 תווים לפחות. / Password must be at least 6 characters.');
  process.exit(1);
}

console.log('\nהוסף את השורה הבאה לקובץ .env  /  Add this line to your .env file:\n');
console.log(`APP_PASSWORD_HASH=${hashPassword(password)}\n`);
