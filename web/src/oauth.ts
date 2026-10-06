/**
 * Browser side of OAuth 2.0 authorization code + PKCE (RFC 7636).
 *
 * Only the short-lived values that must survive the redirect (PKCE verifier, state, handoff token)
 * go into sessionStorage, and they're removed as soon as the callback is handled. The access token
 * is kept in memory only and is used once, to open the chat session.
 */

const PENDING_KEY = "tidelink.oauth.pending";
export const CLIENT_ID = "tidelink-web";
export const REDIRECT_PATH = "/chat/callback";
export const SCOPES = "account:read account:manage";

interface Pending {
  verifier: string;
  state: string;
  ctx: string;
  createdAt: number;
}

function base64url(bytes: Uint8Array): string {
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(byteLength = 32): string {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
}

async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

function redirectUri(): string {
  return `${window.location.origin}${REDIRECT_PATH}`;
}

/** Start sign-in: remember what we need for the callback, then go to the identity provider. */
export async function beginSignIn(ctx: string, scenarioId: string | null): Promise<void> {
  const verifier = randomString(32);
  const state = randomString(16);
  const pending: Pending = { verifier, state, ctx, createdAt: Date.now() };
  sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending));
  const params = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: redirectUri(),
    scope: SCOPES,
    state,
    code_challenge: await challengeFor(verifier),
    code_challenge_method: "S256",
  });
  if (scenarioId) params.set("login_hint", `scenario:${scenarioId}`);
  window.location.assign(`/oauth/authorize?${params.toString()}`);
}

export class SignInError extends Error {}

/** Handle /chat/callback: check state, exchange the code (with the PKCE verifier) for an access token. */
export async function completeSignIn(): Promise<{ ctx: string; accessToken: string; scope: string }> {
  const query = new URLSearchParams(window.location.search);
  const raw = sessionStorage.getItem(PENDING_KEY);
  sessionStorage.removeItem(PENDING_KEY); // one shot, whatever happens next
  if (query.get("error") === "access_denied") throw new SignInError("Sign-in was cancelled, so nothing was connected.");
  if (!raw) throw new SignInError("This sign-in link has expired. Please start again from search.");
  const pending = JSON.parse(raw) as Pending;
  if (Date.now() - pending.createdAt > 10 * 60 * 1000) throw new SignInError("Sign-in took too long. Please try again.");
  const code = query.get("code");
  if (!code || query.get("state") !== pending.state) {
    throw new SignInError("We couldn't verify this sign-in. Please start again.");
  }
  const res = await fetch("/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri(),
      client_id: CLIENT_ID,
      code_verifier: pending.verifier,
    }),
    credentials: "same-origin",
  });
  const body = (await res.json().catch(() => ({}))) as { access_token?: string; scope?: string };
  if (!res.ok || !body.access_token) throw new SignInError("Sign-in failed. Please try again.");
  return { ctx: pending.ctx, accessToken: body.access_token, scope: body.scope ?? "" };
}
