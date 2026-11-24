// Explicit, origin-bound local/sessionStorage sources. Never enumerate values.
import fs from 'node:fs/promises';
import path from 'node:path';

export async function captureStorage(page, sources) {
  if (!Array.isArray(sources) || sources.length > 20) throw Error('Expected at most 20 storage sources');
  const seen = new Set(), result = [];
  for (const source of sources) {
    if (!/^[a-zA-Z][a-zA-Z0-9_]{0,63}$/.test(source.id) || seen.has(source.id) ||
        !['localStorage', 'sessionStorage'].includes(source.area) ||
        typeof source.key !== 'string' || !source.key || source.key.length > 512 ||
        /password|passwd|passphrase|^(pwd|passcode|otp)$/i.test(source.key)) throw Error('Invalid storage source declaration');
    seen.add(source.id);
    const origin = new URL(await page.url()).origin;
    if (source.origin !== origin) throw Error('Storage source origin does not match the current page');
    const value = await page.evaluate(({origin, area, key}) => {
      if (location.origin !== origin) throw Error('Storage page changed origin');
      return (area === 'localStorage' ? localStorage : sessionStorage).getItem(key);
    }, {origin, area:source.area, key:source.key});
    if (value !== null && (typeof value !== 'string' || value.length > 1000000)) throw Error('Storage value exceeds the relay limit');
    function hasPassword(v, depth = 0) {
      if (depth > 12) throw Error('Storage nesting exceeds the relay limit');
      if (typeof v === 'string' && /^[\s]*[\[{]/.test(v)) {
        let nested; try { nested = JSON.parse(v); } catch { return false; }
        return hasPassword(nested, depth + 1);
      }
      return Array.isArray(v) ? v.some(x => hasPassword(x, depth + 1)) : v && typeof v === 'object' &&
        Object.entries(v).some(([k,x]) => /password|passwd|passphrase|^(pwd|passcode|otp)$/i.test(k) && x || hasPassword(x, depth + 1));
    }
    let decoded;
    try { decoded = JSON.parse(value); } catch {}
    if (hasPassword(decoded)) throw Error('Password-bearing storage cannot be captured');
    result.push({id:source.id, origin, area:source.area, key:source.key,
      captured_at:new Date().toISOString(), value});
  }
  return result;
}

export async function saveStorageSnapshot(page, sources, file) {
  const storage_sources = await captureStorage(page, sources);
  await fs.mkdir(path.dirname(file), {recursive:true, mode:0o700});
  await fs.chmod(path.dirname(file), 0o700);
  await fs.writeFile(file, JSON.stringify({format:'web2mcp-storage-v1', storage_sources}, null, 2), {mode:0o600});
  await fs.chmod(file, 0o600);
  return storage_sources.map(s=>({id:s.id, origin:s.origin, area:s.area, found:s.value!==null}));
}
