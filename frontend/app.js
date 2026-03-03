/* Sourcing Africa — PWA App Logic */

// ── Status indicator ──────────────────────────────────────────────────────────

async function checkStatus() {
  const dot = document.getElementById('statusDot');
  try {
    const r = await fetch('/api/status');
    if (r.ok) {
      const d = await r.json();
      dot.className = 'status-dot ok';
      dot.title = `${d.total_articles} articles · ${(d.sources || []).join(', ')}`;
    } else {
      dot.className = 'status-dot error';
    }
  } catch {
    dot.className = 'status-dot error';
    dot.title = 'Server unreachable';
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

// ── Q&A ───────────────────────────────────────────────────────────────────────

const input   = document.getElementById('questionInput');
const sendBtn = document.getElementById('sendBtn');
const answerBox  = document.getElementById('answerBox');
const answerText = document.getElementById('answerText');
const answerMeta = document.getElementById('answerMeta');

// Auto-resize textarea
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 120) + 'px';
});

// Send on Enter (not Shift+Enter)
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendQuestion();
  }
});

sendBtn.addEventListener('click', sendQuestion);

// Suggestion chips
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    input.value = chip.textContent;
    input.dispatchEvent(new Event('input'));
    sendQuestion();
  });
});

async function sendQuestion() {
  const q = input.value.trim();
  if (!q) return;

  sendBtn.disabled = true;
  answerBox.hidden = false;
  answerMeta.textContent = '';
  answerText.textContent = 'Thinking…';
  answerText.className   = 'answer-text loading';

  // Scroll into view
  answerBox.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  try {
    const r = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, days: 30 }),
    });

    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || 'Server error');
    }

    const data = await r.json();
    answerText.className = 'answer-text';
    answerText.textContent = data.answer;
    answerMeta.textContent =
      `${data.article_count} articles · past ${data.days_covered} days`;
  } catch (err) {
    answerText.className = 'answer-text';
    answerText.textContent = `Error: ${err.message}`;
  } finally {
    sendBtn.disabled = false;
  }
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
  feed.innerHTML = articles.map(a => `
    <div class="article-card" data-id="${a.id}">
      <div class="article-source">
        ${escHtml(a.source)}
        <span class="article-date">${a.date}</span>
      </div>
      <div class="article-title">${escHtml(a.subject)}</div>
      <div class="article-preview">${escHtml(a.preview)}</div>
    </div>
  `).join('');

  feed.querySelectorAll('.article-card').forEach(card => {
    card.addEventListener('click', () => openArticle(card.dataset.id));
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

// ── Utility ───────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
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
  document.getElementById('summaryContent').hidden     = true;

  try {
    // Load metadata and summary in parallel
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
  } catch (err) {
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
