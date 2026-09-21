// Runs the real app.js against a minimal fake DOM and canned API responses.
// stdin: {"js": "...", "hash": "#/", "routes": {"GET /api/v1/me": [200, {...}], ...}, "clicks": ["APPROVE", ...], "typed": {"textarea": "..."}}
// stdout: {"texts": [...], "calls": [{method, path, body}], "attrs": [...], "inputs": [...]}
const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', async () => {
  const cfg = JSON.parse(Buffer.concat(chunks).toString());
  let UID = 0;
  function N(tag) { this.uid = ++UID; this.hidden = false; this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this._text = ''; this.className = ''; this.value = ''; this.disabled = false; this.checked = false; this.scrollTop = 0; this.clientHeight = 0; this._lineHeight = 0; }
  // A scrollable box: its height is however many lines it holds, so appending really does
  // make it taller, and the page's own scroll arithmetic is what is under test.
  Object.defineProperty(N.prototype, 'scrollHeight', { get() { return this._lineHeight ? this.children.length * this._lineHeight : 0; } });
  N.prototype.appendChild = function (c) { this.children.push(c); if (this.tag === 'select' && c.tag === 'option' && !this.value) this.value = c.attrs.value; return c; };
  N.prototype.insertBefore = function (c, ref) { const i = this.children.indexOf(ref); if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; };
  N.prototype.removeChild = function (c) { this.children = this.children.filter((x) => x !== c); };
  Object.defineProperty(N.prototype, 'firstChild', { get() { return this.children[0] || null; } });
  N.prototype.addEventListener = function (t, f) { this.listeners[t] = f; };
  N.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
  Object.defineProperty(N.prototype, 'textContent', { get() { return this._text + this.children.map((c) => c.textContent).join(''); }, set(v) { this._text = String(v); this.children = []; } });
  N.prototype.walk = function (fn) { fn(this); this.children.forEach((c) => c.walk(fn)); };
  N.prototype.querySelector = function (sel) { let hit = null; this.walk((n) => { if (!hit && n.tag === sel) hit = n; }); return hit; };
  const app = new N('main');
  const calls = [];
  let NOW = 1000000000000; Date.now = () => NOW;
  const intervals = [];
  global.document = { activeElement: null, createElement: (t) => new N(t), createTextNode: (t) => { const n = new N('#text'); n._text = t; return n; }, getElementById: () => app, hidden: false };
  global.window = { crypto: { randomUUID: () => 'uuid-' + calls.length }, addEventListener() {} };
  global.crypto = global.window.crypto;
  global.location = { hash: cfg.hash || '' };
  global.setInterval = (fn) => { intervals.push(fn); return intervals.length; }; global.clearInterval = () => { intervals.length = 0; };
  global.fetch = (path, opts) => {
    const method = (opts && opts.method) || 'GET';
    calls.push({ method, path, body: opts && opts.body ? JSON.parse(opts.body) : null, headers: (opts && opts.headers) || {} });
    const hit = cfg.routes[method + ' ' + path] || [404, {}];
    return Promise.resolve({ status: hit[0], json: () => Promise.resolve(hit[1]) });
  };
  new Function(cfg.js)();
  const tick = () => new Promise((r) => setTimeout(r, 20));
  await tick(); await tick();
  const snaps = {};
  const boxes = () => { const o = []; app.walk((n) => { if (n.tag === 'textarea' || n.tag === 'input') o.push({ tag: n.tag, uid: n.uid, value: n.value }); }); return o; };
  const find = (cls) => { let hit = null; app.walk((n) => { if (!hit && String(n.className).split(' ').includes(cls)) hit = n; }); return hit; };
  // What a reader of the transcript can actually see: its lines, where it is scrolled,
  // and whether the "new updates" pill is showing.
  const transcript = () => {
    const t = find('transcript');
    if (!t) return null;
    const pill = find('pill');
    return { lines: t.children.map((c) => c.textContent), scrollTop: t.scrollTop, scrollHeight: t.scrollHeight,
             clientHeight: t.clientHeight, atBottom: t.scrollHeight - t.scrollTop - t.clientHeight <= 40,
             pillHidden: pill ? pill.hidden : null, pillText: pill ? pill.textContent : null };
  };
  for (const step of cfg.steps || []) {
    if (step.routes) { Object.assign(cfg.routes, step.routes); continue; }
    // Give the transcript a real geometry: each line is `lineHeight` tall in a `clientHeight` viewport.
    if (step.measure) { const t = find('transcript'); if (t) { t._lineHeight = step.measure.lineHeight || 10; t.clientHeight = step.measure.clientHeight || 100; if (step.measure.toBottom) t.scrollTop = t.scrollHeight - t.clientHeight; } continue; }
    // The reader drags the box: set the offset and fire the page's own scroll listener.
    if (step.scrollTo !== undefined) { const t = find('transcript'); if (t) { t.scrollTop = step.scrollTo === 'bottom' ? t.scrollHeight - t.clientHeight : step.scrollTo; if (t.listeners.scroll) t.listeners.scroll({}); } await tick(); continue; }
    if (step.readTranscript) { snaps[step.readTranscript] = transcript(); continue; }
    if (step.poll) { intervals.slice().forEach((f) => f()); await tick(); await tick(); continue; }
    if (step.advance) { NOW += step.advance; continue; }
    if (step.focus) { let t = null; app.walk((n) => { if (n.tag === step.focus && !t) t = n; }); document.activeElement = t; if (t && t.listeners.focus) t.listeners.focus({}); continue; }
    if (step.blur) { const t = document.activeElement; document.activeElement = null; if (t && t.listeners.blur) t.listeners.blur({}); await tick(); continue; }
    if (step.snap) { snaps[step.snap] = { boxes: boxes(), text: app.textContent, buttons: (() => { const b = []; app.walk((n) => { if (n.tag === 'button') b.push(n.textContent); }); return b; })() }; continue; }
    if (step.aria) { let t = null; app.walk((n) => { if (n.attrs['aria-label'] === step.aria && !t) t = n; }); if (t) { t.value = step.value; if (t.listeners.change) t.listeners.change({}); } continue; }
    if (step.type) { let t = null; app.walk((n) => { if (n.tag === step.type && !t) t = n; }); if (t) t.value = step.value; continue; }
    let b = null; app.walk((n) => { if (n.tag === 'button' && n.textContent === step.click && !b) b = n; });
    if (b && b.listeners.click) b.listeners.click({});
    await tick(); await tick();
  }
  const texts = []; const attrs = []; const inputs = []; const buttons = [];
  app.walk((n) => { if (n.tag === '#text' || (n._text && n.tag !== 'main')) texts.push(n._text); Object.keys(n.attrs).forEach((k) => attrs.push([n.tag, k, n.attrs[k]])); if (['input', 'textarea', 'select'].includes(n.tag)) inputs.push(n.tag); if (n.tag === 'button') buttons.push(n.textContent); });
  process.stdout.write(JSON.stringify({ snaps, texts, calls, attrs, inputs, buttons, transcript: transcript(), all: app.textContent }));
});
