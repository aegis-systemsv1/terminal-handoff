// Runs the real app.js against a minimal fake DOM and canned API responses.
// stdin: {"js": "...", "hash": "#/", "routes": {"GET /api/v1/me": [200, {...}], ...}, "clicks": ["APPROVE", ...], "typed": {"textarea": "..."}}
// stdout: {"texts": [...], "calls": [{method, path, body}], "attrs": [...], "inputs": [...]}
const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', async () => {
  const cfg = JSON.parse(Buffer.concat(chunks).toString());
  function N(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this._text = ''; this.className = ''; this.value = ''; this.disabled = false; this.checked = false; }
  N.prototype.appendChild = function (c) { this.children.push(c); if (this.tag === 'select' && c.tag === 'option' && !this.value) this.value = c.attrs.value; return c; };
  N.prototype.removeChild = function (c) { this.children = this.children.filter((x) => x !== c); };
  Object.defineProperty(N.prototype, 'firstChild', { get() { return this.children[0] || null; } });
  N.prototype.addEventListener = function (t, f) { this.listeners[t] = f; };
  N.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
  Object.defineProperty(N.prototype, 'textContent', { get() { return this._text + this.children.map((c) => c.textContent).join(''); }, set(v) { this._text = String(v); this.children = []; } });
  N.prototype.walk = function (fn) { fn(this); this.children.forEach((c) => c.walk(fn)); };
  N.prototype.querySelector = function (sel) { let hit = null; this.walk((n) => { if (!hit && n.tag === sel) hit = n; }); return hit; };
  const app = new N('main');
  const calls = [];
  global.document = { createElement: (t) => new N(t), createTextNode: (t) => { const n = new N('#text'); n._text = t; return n; }, getElementById: () => app, hidden: false };
  global.window = { crypto: { randomUUID: () => 'uuid-' + calls.length }, addEventListener() {} };
  global.crypto = global.window.crypto;
  global.location = { hash: cfg.hash || '' };
  global.setInterval = () => 1; global.clearInterval = () => {};
  global.fetch = (path, opts) => {
    const method = (opts && opts.method) || 'GET';
    calls.push({ method, path, body: opts && opts.body ? JSON.parse(opts.body) : null, headers: (opts && opts.headers) || {} });
    const hit = cfg.routes[method + ' ' + path] || [404, {}];
    return Promise.resolve({ status: hit[0], json: () => Promise.resolve(hit[1]) });
  };
  new Function(cfg.js)();
  const tick = () => new Promise((r) => setTimeout(r, 20));
  await tick(); await tick();
  for (const step of cfg.steps || []) {
    if (step.type) { let t = null; app.walk((n) => { if (n.tag === step.type && !t) t = n; }); if (t) t.value = step.value; continue; }
    let b = null; app.walk((n) => { if (n.tag === 'button' && n.textContent === step.click && !b) b = n; });
    if (b && b.listeners.click) b.listeners.click({});
    await tick(); await tick();
  }
  const texts = []; const attrs = []; const inputs = []; const buttons = [];
  app.walk((n) => { if (n.tag === '#text' || (n._text && n.tag !== 'main')) texts.push(n._text); Object.keys(n.attrs).forEach((k) => attrs.push([n.tag, k, n.attrs[k]])); if (['input', 'textarea', 'select'].includes(n.tag)) inputs.push(n.tag); if (n.tag === 'button') buttons.push(n.textContent); });
  process.stdout.write(JSON.stringify({ texts, calls, attrs, inputs, buttons, all: app.textContent }));
});
