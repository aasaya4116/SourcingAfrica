/* Sourcing Africa — Supabase Auth gate.
 *
 * Loads before app.js and does two jobs:
 *   1. hold the app behind a sign-in screen until there is a session
 *   2. expose apiFetch(), which attaches the access token to every API call
 *
 * Magic-link sign-in: no passwords to store, reset, or leak, and inviting a
 * tester is just adding their address to ALLOWED_EMAILS.
 */

let sb = null;              // Supabase client
let authReady = null;       // resolves once we know whether we have a session
let authDisabled = false;

const authGate   = document.getElementById('authGate');
const authForm   = document.getElementById('authForm');
const authEmail  = document.getElementById('authEmail');
const authSubmit = document.getElementById('authSubmit');
const authMsg    = document.getElementById('authMsg');

function showGate(show) {
  authGate.hidden = !show;
  document.body.classList.toggle('locked', show);
}

async function initAuth() {
  let cfg;
  try {
    cfg = await (await fetch('/api/config')).json();
  } catch {
    authMsg.textContent = 'Cannot reach the server.';
    showGate(true);
    return;
  }

  // Local development with AUTH_DISABLED=1 — no sign-in, no Supabase client.
  if (cfg.auth_disabled) {
    authDisabled = true;
    showGate(false);
    return;
  }

  if (!cfg.supabase_url || !cfg.supabase_anon_key) {
    authMsg.textContent = 'Auth is not configured on the server.';
    showGate(true);
    return;
  }

  sb = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key, {
    auth: {
      // localStorage, not the default, so the session survives an iOS PWA
      // relaunch from the home screen.
      storage: window.localStorage,
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  });

  const { data: { session } } = await sb.auth.getSession();
  showGate(!session);
  let wasSignedIn = !!session;

  sb.auth.onAuthStateChange((_event, s) => {
    const signedIn = !!s;
    // Act only on a real transition. supabase-js emits INITIAL_SESSION on every
    // page load when a session is stored, and TOKEN_REFRESHED periodically after
    // that. This used to call location.reload() whenever a session was present,
    // so the moment you signed in the page reloaded, fired INITIAL_SESSION again,
    // and reloaded forever — an endless refresh loop that ate every click.
    if (signedIn === wasSignedIn) return;
    wasSignedIn = signedIn;
    showGate(!signedIn);
    // Re-run the data loads in place rather than reloading the document.
    if (signedIn && typeof window.initAppData === 'function') window.initAppData();
  });
}

authReady = initAuth();

authForm?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = authEmail.value.trim();
  if (!email || !sb) return;

  authSubmit.disabled = true;
  authMsg.textContent = 'Sending…';
  const { error } = await sb.auth.signInWithOtp({
    email,
    options: { emailRedirectTo: window.location.origin },
  });
  authSubmit.disabled = false;
  authMsg.textContent = error
    ? `Could not send the link: ${error.message}`
    : 'Check your email for a sign-in link.';
});

/** fetch() with the caller's access token attached. Use for every /api call. */
async function apiFetch(url, options = {}) {
  await authReady;
  const headers = { ...(options.headers || {}) };

  if (!authDisabled && sb) {
    const { data: { session } } = await sb.auth.getSession();
    if (!session) {
      showGate(true);
      throw new Error('Not signed in');
    }
    headers['Authorization'] = `Bearer ${session.access_token}`;
  }

  const res = await fetch(url, { ...options, headers });
  if (res.status === 401) {
    // Token expired or revoked mid-session — send them back to the gate
    // rather than letting every view fail with an opaque error.
    showGate(true);
  }
  return res;
}

async function signOut() {
  if (sb) await sb.auth.signOut();
  showGate(true);
}

window.apiFetch = apiFetch;
window.signOut = signOut;
