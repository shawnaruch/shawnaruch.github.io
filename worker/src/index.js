// Home dashboard on Cloudflare Workers, behind Cloudflare Access (Google sign-in).
//
// Every request must carry a valid Cloudflare Access token. Access checks it at
// the edge; this Worker checks it again (signature, audience, issuer, expiry),
// so a misconfigured Access policy fails closed instead of exposing data.
//
//   GET  /, /privacy/...          dashboard pages (static assets in ./site)
//   GET  /data/<name>             latest.json, camera.json, frame.jpg from KV
//   PUT  /ingest/<name>           writers only: the GitHub Action (sensors, Nest)
//                                 and the Mac mini (camera). Needs an Access
//                                 *service token* plus the INGEST_TOKEN secret.
//
// Config: vars ACCESS_TEAM_DOMAIN (e.g. "esemay.cloudflareaccess.com") and
// ACCESS_AUD (the Access application's AUD tag); secret INGEST_TOKEN; KV HOME_DATA.

const DATA_FILES = {
  "latest.json": "application/json",
  "camera.json": "application/json",
  "frame.jpg": "image/jpeg",
};
const MAX_UPLOAD = 5 * 1024 * 1024;

const SECURITY_HEADERS = {
  "content-security-policy":
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; " +
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
  "x-frame-options": "DENY",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "strict-transport-security": "max-age=31536000",
  "permissions-policy": "camera=(), microphone=(), geolocation=()",
  "x-robots-tag": "noindex, nofollow",
};

function withHeaders(response, extra = {}) {
  const r = new Response(response.body, response);
  for (const [k, v] of Object.entries({ ...SECURITY_HEADERS, ...extra })) r.headers.set(k, v);
  return r;
}

function text(body, status) {
  return withHeaders(new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } }),
    { "cache-control": "no-store" });
}

// ---------------------------------------------------------------- Access JWT
const b64url = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)),
  (c) => c.charCodeAt(0));
const decodeJson = (s) => JSON.parse(new TextDecoder().decode(b64url(s)));

let keyCache = { at: 0, keys: new Map() };

async function accessKey(team, kid) {
  const fresh = Date.now() - keyCache.at < 60 * 60 * 1000;
  if (!fresh || !keyCache.keys.has(kid)) {
    const res = await fetch(`https://${team}/cdn-cgi/access/certs`);
    if (!res.ok) throw new Error(`certs ${res.status}`);
    const { keys = [] } = await res.json();
    const map = new Map();
    for (const jwk of keys) {
      map.set(jwk.kid, await crypto.subtle.importKey(
        "jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]));
    }
    keyCache = { at: Date.now(), keys: map };
  }
  return keyCache.keys.get(kid);
}

function tokenFrom(request) {
  const h = request.headers.get("cf-access-jwt-assertion");
  if (h) return h;
  const m = (request.headers.get("cookie") || "").match(/(?:^|;\s*)CF_Authorization=([^;]+)/);
  return m ? m[1] : null;
}

// Returns the verified claims, or null.
export async function verifyAccess(request, env) {
  const team = env.ACCESS_TEAM_DOMAIN, aud = env.ACCESS_AUD;
  if (!team || !aud) return null;                       // not configured: fail closed
  const token = tokenFrom(request);
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const header = decodeJson(parts[0]);
    if (header.alg !== "RS256") return null;
    const key = await accessKey(team, header.kid);
    if (!key) return null;
    const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64url(parts[2]),
      new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
    if (!ok) return null;
    const claims = decodeJson(parts[1]);
    const now = Math.floor(Date.now() / 1000);
    const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!auds.includes(aud)) return null;
    if (claims.iss !== `https://${team}`) return null;
    if (typeof claims.exp !== "number" || claims.exp < now) return null;
    if (typeof claims.nbf === "number" && claims.nbf > now + 60) return null;
    return claims;
  } catch {
    return null;
  }
}

function sameSecret(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || !b) return false;
  const x = new TextEncoder().encode(a), y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] || 0) ^ (y[i] || 0);
  return diff === 0;
}

// ---------------------------------------------------------------- handler
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const claims = await verifyAccess(request, env);
    if (!claims) return text("Sign-in required.", 403);

    const [, area, name] = url.pathname.match(/^\/(data|ingest)\/([^/]+)$/) || [];

    if (area === "ingest") {
      if (request.method !== "PUT") return text("Use PUT.", 405);
      // Writers are machines: an Access service token (no email) plus the ingest secret.
      if (claims.email || !sameSecret(request.headers.get("x-ingest-token"), env.INGEST_TOKEN)) {
        return text("Not allowed.", 403);
      }
      if (!DATA_FILES[name]) return text("Unknown file.", 404);
      const body = await request.arrayBuffer();
      if (body.byteLength === 0 || body.byteLength > MAX_UPLOAD) return text("Bad size.", 413);
      if (DATA_FILES[name] === "application/json") {
        try { JSON.parse(new TextDecoder().decode(body)); } catch { return text("Not JSON.", 400); }
      }
      await env.HOME_DATA.put(name, body, { metadata: { type: DATA_FILES[name], at: Date.now() } });
      return text("Stored.", 200);
    }

    if (area === "data") {
      if (request.method !== "GET" && request.method !== "HEAD") return text("Use GET.", 405);
      if (!DATA_FILES[name]) return text("Unknown file.", 404);
      const { value, metadata } = await env.HOME_DATA.getWithMetadata(name, { type: "arrayBuffer" });
      if (!value) return text("No data yet.", 404);
      return withHeaders(new Response(value, { headers: { "content-type": metadata?.type || DATA_FILES[name] } }),
        { "cache-control": "private, no-store" });
    }

    // Dashboard pages. Machines have no business here.
    if (!claims.email) return text("Not allowed.", 403);
    const res = await env.ASSETS.fetch(request);
    return withHeaders(res, { "cache-control": "private, no-cache" });
  },
};
