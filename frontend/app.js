/* Sourcing Africa — PWA App Logic */

// ── Utility ───────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function timeAgo(isoString) {
  if (!isoString) return '';
  const diff = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
  if (diff < 60)   return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// ── Status + freshness ────────────────────────────────────────────────────────

const freshnessLabel = document.getElementById('freshnessLabel');
const statusDot      = document.getElementById('statusDot');

async function checkStatus() {
  try {
    const r = await fetch('/api/status');
    if (r.ok) {
      const d = await r.json();
      statusDot.className = 'status-dot ok';
      statusDot.title = `${d.total_articles} articles · ${(d.sources || []).join(', ')}`;
      if (d.last_sync_at) {
        freshnessLabel.textContent = 'Synced ' + timeAgo(d.last_sync_at);
      }
    } else {
      statusDot.className = 'status-dot error';
    }
  } catch {
    statusDot.className = 'status-dot error';
    statusDot.title = 'Server unreachable';
  }
}

// ── Tab switching ─────────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => {
      c.classList.remove('active');
      c.hidden = true;
    });
    btn.classList.add('active');
    const el = document.getElementById(`tab-${tab}`);
    el.classList.add('active');
    el.hidden = false;

    if (tab === 'feed') loadFeed();
  });
});

// ── Dynamic suggestions ───────────────────────────────────────────────────────

const chipList      = document.getElementById('chipList');
const suggestionsBox = document.getElementById('suggestionsBox');

const FALLBACK_CHIPS = [
  'What happened in Nigerian fintech this week?',
  'Any new funding rounds in East Africa?',
  'Summarise the latest Bloomberg Africa issue',
  'What macro trends should I watch?',
];

function renderChips(suggestions) {
  chipList.innerHTML = '';
  suggestions.forEach(text => {
    const btn = document.createElement('button');
    btn.className = 'chip';
    btn.textContent = text;
    btn.addEventListener('click', () => {
      questionInput.value = text;
      questionInput.dispatchEvent(new Event('input'));
      sendQuestion();
    });
    chipList.appendChild(btn);
  });
}

async function loadSuggestions() {
  try {
    const r = await fetch('/api/suggestions');
    if (r.ok) {
      const d = await r.json();
      if (d.suggestions && d.suggestions.length > 0) {
        renderChips(d.suggestions);
        return;
      }
    }
  } catch {}
  // Fallback to static chips
  renderChips(FALLBACK_CHIPS);
}

// ── Chat state ────────────────────────────────────────────────────────────────

// Full conversation history: [{role, content}, ...]
let conversationMessages = [];

// ── Q&A / Chat ────────────────────────────────────────────────────────────────

const chatMessages = document.getElementById('chatMessages');
const questionInput = document.getElementById('questionInput');
const sendBtn       = document.getElementById('sendBtn');

// Auto-resize textarea
questionInput.addEventListener('input', () => {
  questionInput.style.height = 'auto';
  questionInput.style.height = Math.min(questionInput.scrollHeight, 120) + 'px';
});

// Send on Enter (not Shift+Enter)
questionInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendQuestion();
  }
});

sendBtn.addEventListener('click', sendQuestion);

function appendUserBubble(text) {
  const div = document.createElement('div');
  div.className = 'chat-msg user';
  div.innerHTML = `<div class="bubble">${escHtml(text)}</div>`;
  chatMessages.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'end' });
  return div;
}

function appendAssistantBubble() {
  const div = document.createElement('div');
  div.className = 'chat-msg assistant';
  div.innerHTML = `<div class="bubble loading">Thinking…</div><div class="msg-meta"></div>`;
  chatMessages.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'end' });
  return div;
}

async function sendQuestion() {
  const q = questionInput.value.trim();
  if (!q) return;

  // Hide suggestions once chat starts
  suggestionsBox.hidden = true;

  // Add user message to history and render
  conversationMessages.push({ role: 'user', content: q });
  appendUserBubble(q);

  // Clear input
  questionInput.value = '';
  questionInput.style.height = 'auto';
  sendBtn.disabled = true;

  // Add assistant bubble (loading)
  const assistantDiv = appendAssistantBubble();
  const bubble  = assistantDiv.querySelector('.bubble');
  const metaDiv = assistantDiv.querySelector('.msg-meta');

  try {
    const r = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: q,
        days: 30,
        messages: conversationMessages,
      }),
    });

    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || 'Server error');
    }

    const data = await r.json();
    bubble.className = 'bubble';
    bubble.textContent = data.answer;

    const webNote = data.web_results > 0 ? ` · ${data.web_results} web` : '';
    metaDiv.textContent = `${data.article_count} articles · ${data.days_covered}d${webNote}`;

    // Save assistant reply to history
    conversationMessages.push({ role: 'assistant', content: data.answer });

  } catch (err) {
    bubble.className = 'bubble';
    bubble.textContent = `Error: ${err.message}`;
    // Remove failed user message from history
    conversationMessages.pop();
  } finally {
    sendBtn.disabled = false;
    assistantDiv.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }
}

// ── Unread state ──────────────────────────────────────────────────────────────

const READ_KEY = 'sa_read_ids';

function getReadIds() {
  try {
    return new Set(JSON.parse(localStorage.getItem(READ_KEY) || '[]'));
  } catch { return new Set(); }
}

function markRead(id) {
  const ids = getReadIds();
  ids.add(String(id));
  localStorage.setItem(READ_KEY, JSON.stringify([...ids]));
}

// ── Feed ──────────────────────────────────────────────────────────────────────

let currentSource = '';

async function loadFeed(source = '') {
  const feed = document.getElementById('feed');
  feed.innerHTML = '<div class="loading">Loading…</div>';

  try {
    const url = `/api/articles?limit=30${source ? `&source=${encodeURIComponent(source)}` : ''}`;
    const r = await fetch(url);
    const data = await r.json();
    renderFeed(data.articles);

    // Build source filters once
    if (!currentSource && document.querySelectorAll('.filter-chip').length === 1) {
      buildFilters();
    }
  } catch {
    feed.innerHTML = '<div class="empty">Could not load articles.</div>';
  }
}

function renderFeed(articles) {
  const feed = document.getElementById('feed');
  if (!articles.length) {
    feed.innerHTML = '<div class="empty">No articles yet. The ingestor is warming up.</div>';
    return;
  }

  const readIds = getReadIds();

  feed.innerHTML = articles.map(a => {
    const unread = !readIds.has(String(a.id));
    return `
      <div class="article-card" data-id="${a.id}">
        ${unread ? '<span class="unread-dot" aria-label="Unread"></span>' : ''}
        <div class="article-source">
          ${escHtml(a.source)}
          <span class="article-date">${a.date}</span>
        </div>
        <div class="article-title">${escHtml(a.subject)}</div>
        <div class="article-preview">${escHtml(a.preview)}</div>
      </div>
    `;
  }).join('');

  feed.querySelectorAll('.article-card').forEach(card => {
    card.addEventListener('click', () => {
      const id = card.dataset.id;
      markRead(id);
      // Remove unread dot immediately
      const dot = card.querySelector('.unread-dot');
      if (dot) dot.remove();
      openArticle(id);
    });
  });
}

async function buildFilters() {
  try {
    const r = await fetch('/api/sources');
    const { sources } = await r.json();
    const bar = document.querySelector('.filter-bar');
    sources.forEach(s => {
      const btn = document.createElement('button');
      btn.className = 'filter-chip';
      btn.dataset.source = s;
      btn.textContent = s;
      btn.addEventListener('click', () => setFilter(s, btn));
      bar.appendChild(btn);
    });
  } catch {}
}

function setFilter(source, btn) {
  document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  currentSource = source;
  loadFeed(source);
}

// ── Article modal ─────────────────────────────────────────────────────────────

const modalOverlay = document.getElementById('modalOverlay');
const modalClose   = document.getElementById('modalClose');

async function openArticle(id) {
  modalOverlay.hidden = false;
  document.getElementById('modalTitle').textContent    = 'Loading…';
  document.getElementById('modalSource').textContent   = '';
  document.getElementById('modalDate').textContent     = '';
  document.getElementById('summaryLoading').hidden     = false;
  document.getElementById('summaryLoading').textContent = 'Summarising…';
  document.getElementById('summaryContent').hidden     = true;

  try {
    const [metaRes, summaryRes] = await Promise.all([
      fetch(`/api/articles/${id}`),
      fetch(`/api/articles/${id}/summary`),
    ]);
    const a = await metaRes.json();

    document.getElementById('modalSource').textContent = a.source;
    document.getElementById('modalDate').textContent   = a.date;
    document.getElementById('modalTitle').textContent  = a.subject;

    if (!summaryRes.ok) throw new Error('Summary failed');
    const s = await summaryRes.json();

    document.getElementById('summaryHeadline').textContent = s.headline || '';
    document.getElementById('summarySoWhat').textContent   = s.so_what  || '';
    const ul = document.getElementById('summaryHighlights');
    ul.innerHTML = (s.highlights || []).map(h => `<li>${escHtml(h)}</li>`).join('');

    document.getElementById('summaryLoading').hidden  = true;
    document.getElementById('summaryContent').hidden  = false;
  } catch {
    document.getElementById('summaryLoading').hidden = false;
    document.getElementById('summaryLoading').textContent = 'Could not load summary. Try again.';
  }
}

modalClose.addEventListener('click', () => { modalOverlay.hidden = true; });
modalOverlay.addEventListener('click', e => {
  if (e.target === modalOverlay) modalOverlay.hidden = true;
});

// ── Init ──────────────────────────────────────────────────────────────────────

checkStatus();
loadSuggestions();
