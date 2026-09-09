const PALETTE = ['--c1', '--c2', '--c3', '--c4', '--c5', '--c6', '--c7', '--c8'].map(
  (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()
);

const state = {
  currency: 'ILS',
  categories: [],
  filters: { q: '', category: '', status: '', from: '', to: '' },
  months: 12,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ utils */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (response.status === 401) {
    location.href = '/login.html';
    throw new Error('unauthenticated');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `שגיאה ${response.status}`);
  return data;
}

function money(value, currency = state.currency, compact = false) {
  return new Intl.NumberFormat('he-IL', {
    style: 'currency',
    currency,
    maximumFractionDigits: compact ? 0 : 2,
    minimumFractionDigits: compact ? 0 : 2,
    notation: compact ? 'compact' : 'standard',
  }).format(value ?? 0);
}

function monthLabel(iso) {
  const [year, month] = iso.split('-');
  return new Intl.DateTimeFormat('he-IL', { month: 'short', year: '2-digit' }).format(
    new Date(Number(year), Number(month) - 1, 1)
  );
}

function dateLabel(iso) {
  return new Intl.DateTimeFormat('he-IL', { day: '2-digit', month: '2-digit', year: '2-digit' }).format(new Date(iso));
}

function toast(message, isError = false) {
  const element = $('toast');
  element.textContent = message;
  element.classList.toggle('error', isError);
  element.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove('show'), 3200);
}

const escapeHtml = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const svgEl = (markup) => `<svg viewBox="0 0 ${markup.w} ${markup.h}" role="img">${markup.body}</svg>`;

/* ----------------------------------------------------------------- charts */

function renderMonthsChart(byMonth) {
  const host = $('chart-months');
  if (!byMonth.length) {
    host.innerHTML = '<p class="empty">אין עדיין נתונים להצגה.</p>';
    return;
  }

  const W = 640;
  const H = 240;
  const padTop = 16;
  const padBottom = 28;
  const plotH = H - padTop - padBottom;
  const max = Math.max(...byMonth.map((m) => m.spent), 1);
  // Cap the slot so a short history draws a tight, centred group instead of a
  // couple of bars marooned at opposite ends of the panel.
  const slot = Math.min(W / byMonth.length, 84);
  const barW = Math.min(slot * 0.58, 46);
  const offset = (W - slot * byMonth.length) / 2;

  let body = '';
  for (let i = 1; i <= 3; i += 1) {
    const y = padTop + plotH - (plotH * i) / 3;
    body += `<line class="gridline" x1="0" y1="${y}" x2="${W}" y2="${y}"/>`;
    body += `<text class="axis" x="${W - 2}" y="${y - 4}" text-anchor="end">${money((max * i) / 3, state.currency, true)}</text>`;
  }

  byMonth.forEach((month, index) => {
    const height = Math.max((month.spent / max) * plotH, month.spent > 0 ? 2 : 0);
    const x = offset + index * slot + (slot - barW) / 2;
    const y = padTop + plotH - height;
    body += `<rect class="bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${height.toFixed(1)}" rx="4">`;
    body += `<title>${monthLabel(month.month)} — ${money(month.spent)} (${month.count} עסקאות)</title></rect>`;
    body += `<text class="axis" x="${(offset + index * slot + slot / 2).toFixed(1)}" y="${H - 8}" text-anchor="middle">${monthLabel(month.month)}</text>`;
  });

  host.innerHTML = svgEl({ w: W, h: H, body });
}

function renderCategoryChart(byCategory) {
  const host = $('chart-categories');
  const legend = $('legend-categories');

  const items = byCategory.slice(0, 8);
  const total = items.reduce((sum, item) => sum + item.total, 0);
  if (!total) {
    host.innerHTML = '<p class="empty">אין עדיין נתונים.</p>';
    legend.innerHTML = '';
    return;
  }

  const size = 200;
  const cx = size / 2;
  const cy = size / 2;
  const outer = 88;
  const inner = 56;
  let angle = -Math.PI / 2;
  let body = '';

  items.forEach((item, index) => {
    const sweep = (item.total / total) * Math.PI * 2;
    const end = angle + sweep;
    const large = sweep > Math.PI ? 1 : 0;
    const point = (radius, a) => `${(cx + radius * Math.cos(a)).toFixed(2)} ${(cy + radius * Math.sin(a)).toFixed(2)}`;
    // A full circle cannot be drawn as one arc, so nudge the single-slice case.
    const safeEnd = sweep >= Math.PI * 2 - 1e-6 ? end - 1e-4 : end;
    body +=
      `<path d="M ${point(outer, angle)} A ${outer} ${outer} 0 ${large} 1 ${point(outer, safeEnd)} ` +
      `L ${point(inner, safeEnd)} A ${inner} ${inner} 0 ${large} 0 ${point(inner, angle)} Z" ` +
      `fill="${PALETTE[index % PALETTE.length]}"><title>${escapeHtml(item.category)} — ${money(item.total)}</title></path>`;
    angle = end;
  });

  body += `<text x="${cx}" y="${cy - 4}" text-anchor="middle" class="axis" style="font-size:11px">סה״כ</text>`;
  body += `<text x="${cx}" y="${cy + 15}" text-anchor="middle" style="font-size:15px;font-weight:600;fill:var(--text)">${money(total, state.currency, true)}</text>`;

  host.innerHTML = svgEl({ w: size, h: size, body });
  legend.innerHTML = items
    .map(
      (item, index) =>
        `<li><span class="swatch" style="background:${PALETTE[index % PALETTE.length]}"></span>` +
        `<span class="name">${escapeHtml(item.category)}</span>` +
        `<span class="amt">${money(item.total)} · ${Math.round((item.total / total) * 100)}%</span></li>`
    )
    .join('');
}

function renderTopMerchants(merchants) {
  $('top-merchants').innerHTML = merchants.length
    ? merchants
        .map(
          (m, i) =>
            `<li><span class="swatch" style="background:${PALETTE[i % PALETTE.length]}"></span>` +
            `<span class="name">${escapeHtml(m.merchant)}</span>` +
            `<span class="amt">${money(m.total)} · ${m.count}×</span></li>`
        )
        .join('')
    : '<li class="hint">אין נתונים.</li>';
}

function renderBudgets(budgets, byCategory) {
  const host = $('budgets');
  if (!budgets.length) {
    host.innerHTML = '<p class="hint">לא הוגדרו תקציבים. לחץ על "הגדר" כדי להוסיף תקרה חודשית לקטגוריה.</p>';
    return;
  }
  const spentByCategory = Object.fromEntries(byCategory.map((c) => [c.category, c.total]));
  host.innerHTML = budgets
    .map((budget) => {
      const spent = spentByCategory[budget.category] || 0;
      const percent = Math.min((spent / budget.monthly_limit) * 100, 100);
      const over = spent > budget.monthly_limit;
      return (
        `<div class="budget-row"><div class="top"><span>${escapeHtml(budget.category)}</span>` +
        `<span class="${over ? 'up' : ''}">${money(spent)} / ${money(budget.monthly_limit)}</span></div>` +
        `<div class="meter"><i class="${over ? 'over' : ''}" style="width:${percent.toFixed(1)}%"></i></div></div>`
      );
    })
    .join('');
}

/* ----------------------------------------------------------- transactions */

function renderTransactions({ transactions, total }) {
  const host = $('tx-container');
  if (!transactions.length) {
    host.innerHTML =
      '<div class="empty"><p>לא נמצאו עסקאות.</p><p class="hint">נסה לסנכרן, או לשנות את הסינון.</p></div>';
    return;
  }

  const options = (selected) =>
    state.categories
      .map((c) => `<option value="${escapeHtml(c)}"${c === selected ? ' selected' : ''}>${escapeHtml(c)}</option>`)
      .join('');

  host.innerHTML =
    '<table><thead><tr><th>תאריך</th><th>בית עסק</th><th>סכום</th><th>קטגוריה</th><th>פרטים</th><th></th></tr></thead><tbody>' +
    transactions
      .map(
        (tx) => `<tr data-id="${tx.id}">
        <td class="date">${dateLabel(tx.occurred_at)}</td>
        <td class="merchant">${escapeHtml(tx.merchant)}${tx.status === 'review' ? ' <span class="tag review">לבדיקה</span>' : ''}</td>
        <td class="amount${tx.amount < 0 ? ' refund' : ''}">${money(tx.amount, tx.currency)}</td>
        <td><select data-action="category">${options(tx.category)}</select></td>
        <td class="hint">${escapeHtml([tx.account && `כרטיס ${tx.account}`, tx.note].filter(Boolean).join(' · ') || tx.subject || '')}</td>
        <td class="actions">
          ${tx.status === 'review' ? '<button class="ghost" data-action="confirm" title="אשר">✓</button>' : ''}
          <button class="ghost" data-action="ignore" title="הסתר">🚫</button>
          <button class="ghost" data-action="delete" title="מחק">🗑</button>
        </td></tr>`
      )
      .join('') +
    `</tbody></table><p class="hint" style="margin-top:12px">מוצגות ${transactions.length} מתוך ${total} עסקאות.</p>`;
}

async function loadTransactions() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state.filters)) if (value) params.set(key, value);
  params.set('limit', '300');
  $('export-link').href = `/api/export.csv?${params}`;
  renderTransactions(await api(`/api/transactions?${params}`));
}

/* ------------------------------------------------------------ status/data */

async function loadStatus() {
  const status = await api('/api/status');
  state.currency = status.defaultCurrency || 'ILS';

  $('conn-dot').className = `dot ${status.connected ? 'on' : 'off'}`;
  $('conn-text').textContent = status.connected
    ? `${status.account || status.provider} · סונכרן ${status.lastSyncAt ? dateLabel(status.lastSyncAt) : 'טרם'}`
    : 'לא מחובר';

  const setup = $('setup');
  setup.hidden = status.connected;
  if (!status.connected) {
    $('setup-text').textContent =
      status.provider === 'imap'
        ? 'חסרים פרטי IMAP בקובץ .env (IMAP_USER ו-IMAP_PASSWORD). ראה את קובץ ה-README.'
        : 'כדי להתחיל, חבר את חשבון ה-Gmail שלך. האתר מבקש הרשאת קריאה בלבד.';
    $('connect-link').hidden = status.provider === 'imap';
  }

  $('kpi-count').textContent = status.transactions.toLocaleString('he-IL');
  $('kpi-review').textContent = (status.needsReview || 0).toLocaleString('he-IL');
  $('sync-btn').disabled = !status.connected || status.syncing;
  return status;
}

async function loadSummary() {
  const summary = await api(`/api/summary?months=${state.months}`);
  state.currency = summary.currency || state.currency;

  renderMonthsChart(summary.byMonth);
  renderCategoryChart(summary.byCategory);
  renderTopMerchants(summary.topMerchants);
  renderBudgets(summary.budgets, summary.byCategory);

  const months = summary.byMonth;
  const current = months.at(-1);
  const previous = months.at(-2);

  $('kpi-month').textContent = money(summary.thisMonth.total || 0);
  if (previous && current) {
    const delta = current.spent - previous.spent;
    const percent = previous.spent ? Math.round((delta / previous.spent) * 100) : 0;
    $('kpi-month-sub').innerHTML = `<span class="${delta > 0 ? 'up' : 'down'}">${delta > 0 ? '▲' : '▼'} ${Math.abs(percent)}%</span> מול ${monthLabel(previous.month)}`;
  } else {
    $('kpi-month-sub').textContent = `${summary.thisMonth.count} עסקאות`;
  }

  const complete = months.slice(0, -1);
  const average = complete.length ? complete.reduce((sum, m) => sum + m.spent, 0) / complete.length : 0;
  $('kpi-avg').textContent = money(average);
  $('kpi-avg-sub').textContent = `על בסיס ${complete.length} חודשים מלאים`;

  const refunded = months.reduce((sum, m) => sum + (m.refunded || 0), 0);
  $('kpi-count-sub').textContent = refunded ? `כולל ${money(refunded)} החזרים` : '';
}

async function loadRules() {
  const { rules } = await api('/api/rules');
  $('rules').innerHTML = rules.length
    ? rules
        .map(
          (rule) =>
            `<li><span class="name">${escapeHtml(rule.pattern)} → <strong>${escapeHtml(rule.category)}</strong></span>` +
            `<button class="ghost" data-rule="${rule.id}" title="מחק">✕</button></li>`
        )
        .join('')
    : '<li class="hint">אין כללים. הוסף כלל כדי לסווג אוטומטית בתי עסק חוזרים.</li>';
}

async function refreshAll() {
  await Promise.all([loadStatus(), loadSummary(), loadTransactions(), loadRules()]);
}

/* ----------------------------------------------------------------- events */

$('tx-container').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const id = button.closest('tr').dataset.id;
  const action = button.dataset.action;
  try {
    if (action === 'delete') {
      if (!confirm('למחוק את העסקה?')) return;
      await api(`/api/transactions/${id}`, { method: 'DELETE' });
    } else {
      await api(`/api/transactions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: action === 'confirm' ? 'confirmed' : 'ignored' }),
      });
    }
    await Promise.all([loadTransactions(), loadSummary(), loadStatus()]);
  } catch (error) {
    toast(error.message, true);
  }
});

$('tx-container').addEventListener('change', async (event) => {
  const select = event.target.closest('select[data-action="category"]');
  if (!select) return;
  const id = select.closest('tr').dataset.id;
  try {
    await api(`/api/transactions/${id}`, { method: 'PATCH', body: JSON.stringify({ category: select.value }) });
    toast('הקטגוריה עודכנה');
    loadSummary();
  } catch (error) {
    toast(error.message, true);
  }
});

$('rules').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-rule]');
  if (!button) return;
  await api(`/api/rules/${button.dataset.rule}`, { method: 'DELETE' });
  loadRules();
});

$('sync-btn').addEventListener('click', async () => {
  const button = $('sync-btn');
  button.disabled = true;
  button.textContent = 'מסנכרן…';
  try {
    const stats = await api('/api/sync', { method: 'POST' });
    toast(`נסרקו ${stats.scanned} הודעות, נוספו ${stats.imported} עסקאות`);
    await refreshAll();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.textContent = 'סנכרן עכשיו';
    button.disabled = false;
  }
});

$('range').addEventListener('change', (event) => {
  state.months = Number(event.target.value);
  loadSummary();
});

let searchTimer;
const bindFilter = (id, key, delay = 0) =>
  $(id).addEventListener('input', (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.filters[key] = event.target.value;
      loadTransactions().catch((error) => toast(error.message, true));
    }, delay);
  });

bindFilter('q', 'q', 280);
bindFilter('f-category', 'category');
bindFilter('f-status', 'status');
bindFilter('f-from', 'from');
bindFilter('f-to', 'to');

for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => button.closest('dialog').close());
}

$('add-btn').addEventListener('click', () => {
  $('a-date').value = new Date().toISOString().slice(0, 10);
  $('add-dialog').showModal();
});
$('rule-btn').addEventListener('click', () => $('rule-dialog').showModal());
$('budget-btn').addEventListener('click', () => $('budget-dialog').showModal());

$('add-form').addEventListener('submit', async () => {
  try {
    await api('/api/transactions', {
      method: 'POST',
      body: JSON.stringify({
        merchant: $('a-merchant').value,
        amount: $('a-amount').value,
        occurredAt: $('a-date').value,
        category: $('a-category').value,
        note: $('a-note').value,
      }),
    });
    $('add-form').reset();
    toast('נוספה הוצאה');
    await Promise.all([loadTransactions(), loadSummary(), loadStatus()]);
  } catch (error) {
    toast(error.message, true);
  }
});

$('rule-form').addEventListener('submit', async () => {
  try {
    const result = await api('/api/rules', {
      method: 'POST',
      body: JSON.stringify({
        field: $('r-field').value,
        pattern: $('r-pattern').value,
        category: $('r-category').value,
        applyToExisting: $('r-apply').checked,
      }),
    });
    $('rule-form').reset();
    $('r-apply').checked = true;
    toast(result.updated ? `הכלל נשמר, עודכנו ${result.updated} עסקאות` : 'הכלל נשמר');
    await Promise.all([loadRules(), loadTransactions(), loadSummary()]);
  } catch (error) {
    toast(error.message, true);
  }
});

$('budget-form').addEventListener('submit', async () => {
  try {
    await api('/api/budgets', {
      method: 'PUT',
      body: JSON.stringify({ category: $('b-category').value, monthlyLimit: $('b-limit').value }),
    });
    toast('התקציב נשמר');
    loadSummary();
  } catch (error) {
    toast(error.message, true);
  }
});

/* ------------------------------------------------------------------- boot */

(async function init() {
  const session = await fetch('/api/session').then((r) => r.json());
  if (!session.authenticated) {
    location.href = '/login.html';
    return;
  }

  const { categories } = await api('/api/categories');
  state.categories = categories;
  const optionsHtml = categories.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  $('a-category').innerHTML = optionsHtml;
  $('r-category').innerHTML = optionsHtml;
  $('b-category').innerHTML = optionsHtml;
  $('f-category').insertAdjacentHTML('beforeend', optionsHtml);

  const params = new URLSearchParams(location.search);
  if (params.get('connected') === '1') toast('החשבון חובר בהצלחה');
  if (params.get('connected') === '0') toast(`החיבור נכשל: ${params.get('reason') || 'שגיאה'}`, true);
  if (params.has('connected')) history.replaceState({}, '', '/');

  await refreshAll().catch((error) => toast(error.message, true));
})();
