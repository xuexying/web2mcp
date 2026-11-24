// Import inside `ego-browser nodejs`. Capture exists only for this invocation.
// Browser actions are supplied by the AI/user; this module never submits forms.
import fs from 'node:fs/promises';
import path from 'node:path';
import {captureStorage} from './browser_storage.mjs';

export async function startCapture(page) {
  await page.cdp('Network.enable', {maxTotalBufferSize: 100000000, maxResourceBufferSize: 10000000});
  await page.events();
}

function cookieInOrigin(cookie, origin) {
  const host = new URL(origin).hostname.toLowerCase();
  const domain = String(cookie.domain || '').toLowerCase();
  const bare = domain.replace(/^\./, '');
  return !!bare && (host === bare || domain.startsWith('.') && host.endsWith('.' + bare));
}

function hasPasswordInput(request) {
  // Never persist passwords/OTP entered while the user has browser control.
  // Cookie and temporary auth tokens are the explicitly authorized relay data.
  const key = /password|passwd|passphrase|^(pwd|passcode|otp)$/i;
  function contains(value) {
    if (Array.isArray(value)) return value.some(contains);
    if (value && typeof value === 'object') return Object.entries(value).some(([k,v]) => key.test(k) || contains(v));
    if (typeof value === 'string' && /^[\[{]/.test(value.trim())) {
      try { return contains(JSON.parse(value)); } catch {}
    }
    return false;
  }
  const query = new URL(request.url).searchParams;
  if (Object.entries(request.headers || {}).some(([k,v]) =>
      key.test(k) || /^(authorization|proxy-authorization)$/i.test(k) && /^Basic\s/i.test(v))) return true;
  if ([...query].some(([k,v]) => key.test(k) || contains(v))) return true;
  if (!request.postData) return false;
  try { if (contains(JSON.parse(request.postData))) return true; } catch {}
  return [...new URLSearchParams(request.postData)].some(([k,v]) => key.test(k) || contains(v)) ||
    /name=["'](?:password|passwd|pwd|passcode|otp)["']/i.test(request.postData);
}

export async function saveCapture(page, directory, label = 'observed browser operation', options = {}) {
  const events = await page.events();
  // responseReceived precedes loadingFinished. Reading a body too early can
  // fail even when the DOM already shows another completed response. Drain
  // only already-observed Fetch/XHR requests, with a bounded wait (no replay).
  const deadline = Date.now() + 1500;
  while (Date.now() < deadline) {
    const pending = new Set();
    for (const event of events) {
      const p = event.params || {};
      if (event.method === 'Network.requestWillBeSent' && ['Fetch', 'XHR'].includes(p.type)) pending.add(p.requestId);
      if (['Network.loadingFinished', 'Network.loadingFailed'].includes(event.method)) pending.delete(p.requestId);
    }
    if (!pending.size) break;
    await new Promise(resolve => setTimeout(resolve, 40));
    events.push(...await page.events());
  }
  const requests = new Map();
  const responses = new Map();
  const extraHeaders = new Map();
  for (const event of events) {
    const p = event.params || {};
    if (event.method === 'Network.requestWillBeSent') requests.set(p.requestId, p);
    if (event.method === 'Network.responseReceived') responses.set(p.requestId, p);
    if (event.method === 'Network.requestWillBeSentExtraInfo') extraHeaders.set(p.requestId, p.headers || {});
  }
  const exchanges = [];
  for (const [id, p] of responses) {
    const req = requests.get(id);
    if (!req || !['Fetch', 'XHR'].includes(p.type)) continue;
    const response = p.response;
    if (!/^https?:/.test(req.request.url)) continue;
    if (options.origins && !options.origins.includes(new URL(req.request.url).origin)) continue;
    if (options.methods && !options.methods.includes(req.request.method)) continue;
    const request = {...req.request, headers:{...req.request.headers, ...extraHeaders.get(id)}};
    if (hasPasswordInput(request)) continue;
    let body = null, unavailable = null;
    try {
      const value = await page.cdp('Network.getResponseBody', {requestId: id});
      body = value.base64Encoded ? Buffer.from(value.body, 'base64').toString('utf8') : value.body;
    } catch (e) { unavailable = 'Response body unavailable'; }
    exchanges.push({request, response: {status: response.status, headers: response.headers,
      mimeType: response.mimeType, body, unavailable}, label, captured_at: new Date().toISOString()});
  }
  // Full observed paths are needed for cookies scoped below the origin root.
  const urls = [...new Set(exchanges.map(e => e.request.url.split('#')[0]))];
  let cookies = options.includeCookies !== false && urls.length ? (await page.cdp('Network.getCookies', {urls})).cookies : [];
  const capturedAt = new Date().toISOString();
  const sessions = {};
  // A scoped authoritative snapshot also captures HttpOnly cookies when the
  // operation's main CDP request event omitted Cookie. Never persist other sites.
  if (options.includeCookies !== false && options.origins?.length) {
    let all;
    try { all = (await page.cdp('Network.getAllCookies')).cookies; }
    catch { all = (await page.cdp('Network.getCookies', {urls:[...options.origins.map(o=>o+'/'), ...urls]})).cookies; }
    for (const origin of options.origins) {
      sessions[origin] = {captured_at:capturedAt,
        cookies:all.filter(c=>cookieInOrigin(c,origin)).map(c=>({...c,hostOnly:!c.domain.startsWith('.')}))};
    }
    cookies = all.filter(c=>options.origins.some(o=>cookieInOrigin(c,o)));
  }
  await fs.mkdir(directory, {recursive: true, mode: 0o700});
  await fs.chmod(directory, 0o700);
  const tracePath = path.join(directory, 'capture.json');
  let previous = {format: 'web2mcp-capture-v1', exchanges: [], cookies: []};
  try { previous = JSON.parse(await fs.readFile(tracePath, 'utf8')); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  const previousCookies = (previous.cookies || []).filter(c=>!options.origins?.some(o=>cookieInOrigin(c,o)));
  const merged = new Map(previousCookies.map(c => [c.domain + '|' + c.path + '|' + c.name, c]));
  for (const c of cookies) merged.set(c.domain + '|' + c.path + '|' + c.name, c);
  const storage = options.storageSources ? await captureStorage(page, options.storageSources) : [];
  const storageMap = new Map((previous.storage_sources || []).map(s=>[s.id,s]));
  for (const source of storage) storageMap.set(source.id, source);
  const value = {format: previous.format, captured_at:capturedAt,
    exchanges: [...previous.exchanges, ...exchanges], cookies: [...merged.values()],
    sessions:{...(previous.sessions || {}),...sessions}, storage_sources:[...storageMap.values()]};
  await fs.writeFile(tracePath, JSON.stringify(value, null, 2), {mode: 0o600});
  await fs.chmod(tracePath, 0o600);
  return {captured: exchanges.length, total: value.exchanges.length,
    sessions:Object.entries(sessions).map(([origin,s])=>({origin,captured_at:s.captured_at,cookie_names:s.cookies.map(c=>c.name)})),
    operations: exchanges.map(e => ({method: e.request.method, path: new URL(e.request.url).pathname.replace(/(\/tokens\/)[^/]+/gi, '$1[REDACTED]'), status: e.response.status}))};
}
