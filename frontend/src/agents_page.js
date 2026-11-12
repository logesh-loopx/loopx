/* LOOPX Agents page (server clients).
 * A clean grid of the five agents. Tap one for: Overview, Settings, Permissions, History.
 * Rendered into #lx-agents-root, which the console shows instead of its own Agents view.
 */
(function () {
  var LX = window.LX;
  if (!LX || !LX.server) return;

  var ICON = {
    timing: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    writer: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    guard: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    analyst: '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
    listener: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/><path d="M8.5 11h7M8.5 14h4"/>'
  };
  var LEVEL = { suggest: 'Suggests only', approve: 'Acts after your daily approval', auto: 'Acts on its own' };
  var LEVEL_HELP = {
    suggest: 'It prepares everything, and a person does each step.',
    approve: 'It prepares the day, you approve once, then it carries on.',
    auto: 'It acts without asking. Locked safety rules still apply.'
  };
  var ACT = { off: 'Off', suggest: 'Suggest', ask: 'Ask first', auto: 'Do it' };
  var STATUS = {
    done: ['Done', 'ok'], approved: ['Approved', 'ok'], pending: ['Needs you', 'warn'], suggested: ['Suggested', 'warn'],
    declined: ['Declined', 'mute'], undone: ['Undone', 'mute'], info: ['Note', 'mute'], blocked: ['Not allowed', 'mute']
  };

  var P = (LX.agentsPage = { ws: null, data: null, sel: null, tab: 'overview', hist: { filter: 'all', days: 7, items: null }, toast: '' });

  var css = '' +
    '#lx-agents-root{font-family:Figtree,Arial,sans-serif;color:var(--ink);padding-bottom:40px}' +
    '.lxa-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:4px 0 22px}' +
    '.lxa-h1{margin:0;font-family:Sora,sans-serif;font-weight:700;font-size:28px;line-height:1.2}' +
    '.lxa-sub{margin:6px 0 0;color:var(--mut);font-size:14.5px;max-width:620px}' +
    '.lxa-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap}' +
    '.lxa-glass{background:var(--glass);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border:1px solid var(--glassLine);border-radius:20px;box-shadow:0 10px 30px rgba(76,40,180,.10)}' +
    '.lxa-btn{border:0;border-radius:999px;padding:9px 16px;font-weight:600;font-size:13.5px;background:var(--grad);color:#fff;cursor:pointer;box-shadow:0 6px 16px rgba(76,40,180,.22)}' +
    '.lxa-btn.sec{background:var(--glass2);color:var(--tintInk);box-shadow:none;border:1px solid var(--line2)}' +
    '.lxa-btn.warn{background:var(--warnBg);color:var(--warnInk);box-shadow:none}' +
    '.lxa-btn:disabled{opacity:.5;cursor:default}' +
    '.lxa-pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 10px;font-size:12px;font-weight:600;background:var(--muteBg);color:var(--mut2);white-space:nowrap}' +
    '.lxa-pill.ok{background:var(--okBg);color:var(--okInk)}.lxa-pill.warn{background:var(--warnBg);color:var(--warnInk)}.lxa-pill.acc{background:var(--tint);color:var(--tintInk)}' +
    '.lxa-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:16px}' +
    '.lxa-card{padding:20px;display:flex;flex-direction:column;gap:14px;cursor:pointer;text-align:left;transition:transform .15s,box-shadow .15s;font:inherit;color:inherit}' +
    '.lxa-card:hover{transform:translateY(-2px);box-shadow:0 16px 36px rgba(76,40,180,.16)}' +
    '.lxa-card.off{opacity:.62}' +
    '.lxa-top{display:flex;align-items:center;gap:12px}' +
    '.lxa-ico{width:46px;height:46px;border-radius:14px;display:flex;align-items:center;justify-content:center;flex:none}' +
    '.lxa-ico svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}' +
    '.lxa-ico.big{width:60px;height:60px;border-radius:18px}.lxa-ico.big svg{width:30px;height:30px}' +
    '.lxa-name{font-family:Sora,sans-serif;font-weight:700;font-size:17px;line-height:1.25}' +
    '.lxa-role{font-size:13.5px;color:var(--mut);line-height:1.45}' +
    '.lxa-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}' +
    '.lxa-stat{background:var(--glass2);border-radius:12px;padding:8px 10px;min-width:0}' +
    '.lxa-stat b{display:block;font-family:Sora,sans-serif;font-size:17px;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.lxa-stat span{display:block;font-size:11.5px;color:var(--mut);line-height:1.3;margin-top:2px}' +
    '.lxa-foot{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12.5px;color:var(--mut);border-top:1px solid var(--line2);padding-top:12px}' +
    '.lxa-last{font-size:12.5px;color:var(--mut2);line-height:1.4;min-height:35px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}' +
    '.lxa-back{background:none;border:0;color:var(--acc2);font-weight:600;font-size:14px;padding:6px 0;cursor:pointer;margin-bottom:10px}' +
    '.lxa-dhead{padding:22px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:16px}' +
    '.lxa-dhead .grow{flex:1;min-width:220px}' +
    '.lxa-switch{position:relative;width:52px;height:30px;border-radius:999px;border:0;background:var(--line);cursor:pointer;flex:none;transition:background .15s}' +
    '.lxa-switch::after{content:"";position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;background:#fff;box-shadow:0 2px 6px rgba(0,0,0,.2);transition:left .15s}' +
    '.lxa-switch.on{background:var(--acc)}.lxa-switch.on::after{left:25px}' +
    '.lxa-seg{display:inline-flex;background:var(--muteBg);border-radius:999px;padding:3px;gap:2px;flex-wrap:wrap}' +
    '.lxa-seg button{border:0;background:none;border-radius:999px;padding:7px 12px;font-size:12.5px;font-weight:600;color:var(--mut2);cursor:pointer}' +
    '.lxa-seg button.on{background:var(--chipOn);color:var(--chipOnInk);box-shadow:0 2px 8px rgba(76,40,180,.15)}' +
    '.lxa-tabs{display:flex;gap:6px;margin:0 0 14px;flex-wrap:wrap}' +
    '.lxa-tab{border:1px solid var(--line2);background:var(--glass2);border-radius:999px;padding:8px 16px;font-weight:600;font-size:13.5px;color:var(--mut2);cursor:pointer}' +
    '.lxa-tab.on{background:var(--grad);color:#fff;border-color:transparent}' +
    '.lxa-tab .n{display:inline-block;margin-left:6px;background:var(--warnBg);color:var(--warnInk);border-radius:999px;padding:0 7px;font-size:11.5px}' +
    '.lxa-panel{padding:22px}' +
    '.lxa-two{display:grid;grid-template-columns:1.2fr 1fr;gap:16px}@media(max-width:900px){.lxa-two{grid-template-columns:1fr}}' +
    '.lxa-h3{font-family:Sora,sans-serif;font-weight:700;font-size:15px;margin:0 0 10px}' +
    '.lxa-list{margin:0;padding-left:18px;color:var(--mut2);font-size:14px;line-height:1.7}' +
    '.lxa-ai{margin-top:14px;padding:12px 14px;border-radius:14px;background:var(--tint);color:var(--tintInk);font-size:13.5px;line-height:1.5}' +
    '.lxa-field{display:grid;grid-template-columns:1fr 240px;gap:6px 18px;align-items:center;padding:14px 0;border-bottom:1px solid var(--line2)}' +
    '.lxa-field:last-child{border-bottom:0}@media(max-width:700px){.lxa-field{grid-template-columns:1fr}}' +
    '.lxa-field label{font-weight:600;font-size:14px}.lxa-field small{grid-column:1/-1;color:var(--mut);font-size:12.5px;margin-top:-2px}' +
    '.lxa-field input[type=text],.lxa-field input[type=number],.lxa-field input[type=time],.lxa-field select{width:100%;box-sizing:border-box;border:1px solid var(--line);background:var(--glass2);border-radius:10px;padding:8px 10px;font-size:14px}' +
    '.lxa-range{display:flex;align-items:center;gap:10px}.lxa-range input{flex:1;accent-color:var(--acc)}.lxa-range b{min-width:56px;text-align:right;font-family:Sora,sans-serif}' +
    '.lxa-lock{color:var(--mut);font-size:13px;display:flex;align-items:center;gap:6px}' +
    '.lxa-save{position:sticky;bottom:16px;display:flex;justify-content:flex-start;margin-top:14px;pointer-events:none}' +
    '.lxa-save>div{display:flex;gap:10px;padding:8px;border-radius:999px;background:var(--glass2);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 8px 24px rgba(76,40,180,.18);pointer-events:auto}' +
    '.lxa-perm{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 0;border-bottom:1px solid var(--line2);flex-wrap:wrap}' +
    '.lxa-perm:last-child{border-bottom:0}.lxa-perm .t{font-weight:600;font-size:14px}.lxa-perm small{display:block;color:var(--mut);font-size:12.5px;font-weight:400;margin-top:2px}' +
    '.lxa-hbar{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:12px}' +
    '.lxa-day{font-family:Sora,sans-serif;font-weight:700;font-size:13px;color:var(--mut);margin:16px 0 6px;text-transform:uppercase;letter-spacing:.04em}' +
    '.lxa-dec{padding:14px 16px;border-radius:14px;background:var(--glass2);margin-bottom:8px}' +
    '.lxa-dec .r1{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}' +
    '.lxa-dec .tt{font-weight:600;font-size:14px;line-height:1.4}' +
    '.lxa-dec .meta{font-size:12px;color:var(--mut);margin-top:3px}' +
    '.lxa-dec .why{font-size:13px;color:var(--mut2);margin-top:8px;line-height:1.5;white-space:pre-wrap}' +
    '.lxa-dec .acts{display:flex;gap:8px;margin-top:10px}' +
    '.lxa-dec .acts .lxa-btn{padding:6px 14px;font-size:12.5px}' +
    '.lxa-empty{color:var(--mut);font-size:14px;padding:20px 4px}' +
    '.lxa-toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:var(--inv);color:#fff;padding:10px 18px;border-radius:999px;font-size:13.5px;z-index:9997;box-shadow:0 8px 24px rgba(0,0,0,.25)}' +
    '.lxa-note{font-size:13px;color:var(--mut);margin:10px 0 0}';

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function icon(key, big) {
    return '<div class="lxa-ico' + (big ? ' big' : '') + '" style="color:var(--' + colorOf(key) + ');background:color-mix(in srgb,var(--' + colorOf(key) + ') 15%,transparent)"><svg viewBox="0 0 24 24">' + ICON[key] + '</svg></div>';
  }
  function colorOf(key) { var a = agent(key); return a ? a.color : 'n1'; }
  function agent(key) { return P.data ? P.data.agents.filter(function (a) { return a.key === key; })[0] : null; }
  function ago(iso) {
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }
  function root() { return document.getElementById('lx-agents-root'); }
  function api(m, p, b) { return LX.api(m, '/workspaces/' + P.ws + p, b); }
  function toast(t) {
    var el = document.getElementById('lxa-toast');
    if (!el) { el = document.createElement('div'); el.id = 'lxa-toast'; el.className = 'lxa-toast'; document.body.appendChild(el); }
    el.textContent = t; el.style.display = 'block';
    clearTimeout(P._tt); P._tt = setTimeout(function () { el.style.display = 'none'; }, 2600);
  }
  function syncConsole() { if (P.comp && P.comp.srvReloadAgents) P.comp.srvReloadAgents(); }

  // ------------------------------------------------------------------ data
  P.load = function (quiet) {
    if (!P.ws) return;
    var ws = P.ws;
    return api('GET', '/agents').then(function (d) {
      if (ws !== P.ws) return;
      P.data = d;
      if (!quiet || !P.editing) P.render();
    }, function (e) { var r = root(); if (r && !P.data) r.innerHTML = '<p class="lxa-empty">' + esc(e.message) + '</p>'; });
  };
  P.loadHistory = function () {  // show "Loading" and fetch again (the History tab starts the fetch)
    P.hist.items = null; P.hist.loading = false; P.render();
  };
  P.fetchHistory = function () {
    if (!P.sel) { P.hist.loading = false; return; }
    var key = P.sel, ws = P.ws, days = P.hist.days;
    LX.api('GET', '/workspaces/' + ws + '/decisions?agent=' + key + '&days=' + days).then(function (r) {
      P.hist.loading = false;
      if (key !== P.sel || ws !== P.ws || days !== P.hist.days) return;
      P.hist.items = r.decisions; P.render();
    }, function (e) { P.hist.loading = false; P.hist.items = []; toast(e.message); P.render(); });
  };

  // ------------------------------------------------------------------ views
  P.render = function () {
    var r = root();
    if (!r) return;
    if (!document.getElementById('lxa-css')) { var s = document.createElement('style'); s.id = 'lxa-css'; s.textContent = css; document.head.appendChild(s); }
    if (!r.__lxa) { r.__lxa = true; r.addEventListener('click', onClick); r.addEventListener('input', onInput); r.addEventListener('change', onInput); }
    if (!P.data) { r.innerHTML = '<p class="lxa-empty">Loading the agents…</p>'; return; }
    r.innerHTML = P.sel ? detail(agent(P.sel)) : grid();
  };

  function header() {
    var d = P.data;
    var NAMES = { groq: 'Groq', gemini: 'Gemini', openrouter: 'OpenRouter', ollama: 'Ollama', anthropic: 'Claude' };
    var ai = d.ai.on ? '<span class="lxa-pill acc" title="AI providers, tried in this order">AI: ' + esc(d.ai.chain.map(function (n) { return NAMES[n] || n; }).join(' → ')) + '</span>'
      : '<span class="lxa-pill warn" title="Add a free key under Connections, AI">AI off: rules only</span>';
    return '<div class="lxa-head"><div><h1 class="lxa-h1">Agents</h1><p class="lxa-sub">Five agents run your reminders. Tap one to see what it does, change its settings and read its history.</p></div>' +
      '<div class="lxa-tools">' + ai +
      (d.lastTick ? '<span class="lxa-pill" title="Last time the agents ran">Ran ' + esc(ago(d.lastTick + 'Z')) + '</span>' : '') +
      '<button class="lxa-btn sec" data-a="run"' + (d.canRun ? '' : ' disabled') + '>Run now</button>' +
      '<button class="lxa-btn ' + (d.paused ? '' : 'warn') + '" data-a="pauseall"' + (d.canRun ? '' : ' disabled') + '>' + (d.paused ? 'Resume all agents' : 'Pause all agents') + '</button></div></div>' +
      (d.paused ? '<div class="lxa-glass lxa-panel" style="margin-bottom:16px;background:var(--warnBg);color:var(--warnInk)"><b>All agents are paused.</b> Nothing is planned or sent until you resume them. Locked safety rules (unsubscribes) still apply.</div>' : '');
  }

  function grid() {
    return header() + '<div class="lxa-grid">' + P.data.agents.map(function (a) {
      var state = !a.on || P.data.paused ? '<span class="lxa-pill">Off</span>' : '<span class="lxa-pill ok">On</span>';
      return '<button class="lxa-glass lxa-card' + (!a.on || P.data.paused ? ' off' : '') + '" data-open="' + a.key + '" aria-label="Open ' + esc(a.name) + '">' +
        '<div class="lxa-top">' + icon(a.key) + '<div style="flex:1;min-width:0"><div class="lxa-name">' + esc(a.name.replace(' agent', '')) + '</div>' +
        '<div style="font-size:12.5px;color:var(--mut)">' + esc(LEVEL[a.level] || '') + '</div></div>' + state + '</div>' +
        '<div class="lxa-role">' + esc(a.role) + '</div>' +
        '<div class="lxa-stats">' + a.stats.map(function (s) { return '<div class="lxa-stat"><b>' + esc(s.value) + '</b><span>' + esc(s.label) + '</span></div>'; }).join('') + '</div>' +
        '<div class="lxa-last">' + (a.last ? esc(a.last.title) + ' · ' + esc(ago(a.last.at)) : 'Nothing yet today.') + '</div>' +
        '<div class="lxa-foot"><span>' + a.today + ' decision' + (a.today === 1 ? '' : 's') + ' in 24 h</span>' +
        (a.pending ? '<span class="lxa-pill warn">' + a.pending + ' need' + (a.pending === 1 ? 's' : '') + ' you</span>' : '<span>Open →</span>') + '</div></button>';
    }).join('') + '</div>' +
      '<p class="lxa-note">Want more control over sending, replies or AI keys? Use <b>Connections</b> (bottom right).</p>';
  }

  function detail(a) {
    if (!a) { P.sel = null; return grid(); }
    var d = P.data, edit = d.canEdit;
    var tabs = [['overview', 'Overview'], ['settings', 'Settings'], ['perms', 'Permissions'], ['history', 'History']];
    var head = '<button class="lxa-back" data-a="back">← All agents</button>' +
      '<div class="lxa-glass lxa-dhead">' + icon(a.key, true) +
      '<div class="grow"><div class="lxa-name" style="font-size:22px">' + esc(a.name) + '</div><div class="lxa-role">' + esc(a.role) + '</div></div>' +
      '<div style="display:flex;flex-direction:column;gap:8px;align-items:flex-end">' +
      '<div style="display:flex;align-items:center;gap:10px"><span style="font-size:13px;color:var(--mut)">' + (a.on ? 'On' : 'Off') + '</span>' +
      '<button class="lxa-switch' + (a.on ? ' on' : '') + '" role="switch" aria-checked="' + a.on + '" aria-label="Turn ' + esc(a.name) + ' on or off" data-a="toggle"' + (d.canRun ? '' : ' disabled') + '></button></div>' +
      '<div class="lxa-seg" role="group" aria-label="How much it acts on its own">' + ['suggest', 'approve', 'auto'].map(function (l) {
        return '<button data-level="' + l + '" class="' + (a.level === l ? 'on' : '') + '"' + (edit ? '' : ' disabled') + ' title="' + esc(LEVEL_HELP[l]) + '">' + esc(LEVEL[l]) + '</button>';
      }).join('') + '</div></div></div>' +
      '<div class="lxa-tabs">' + tabs.map(function (t) {
        return '<button class="lxa-tab' + (P.tab === t[0] ? ' on' : '') + '" data-tab="' + t[0] + '">' + t[1] + (t[0] === 'history' && a.pending ? '<span class="n">' + a.pending + '</span>' : '') + '</button>';
      }).join('') + '</div>';
    var body = { overview: overview, settings: settingsTab, perms: permsTab, history: historyTab }[P.tab](a);
    return head + body;
  }

  function overview(a) {
    return '<div class="lxa-two"><div class="lxa-glass lxa-panel"><h3 class="lxa-h3">What it does</h3><ul class="lxa-list">' +
      a.does.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' +
      '<div class="lxa-ai"><b>With AI:</b> ' + esc(a.ai) + (P.data.ai.on ? '' : ' <i>AI is off, so it uses rules. Add a free key under Connections, AI.</i>') + '</div>' +
      '<p class="lxa-note">' + esc(LEVEL[a.level]) + ': ' + esc(LEVEL_HELP[a.level]) + '</p></div>' +
      '<div class="lxa-glass lxa-panel"><h3 class="lxa-h3">Today</h3><div class="lxa-stats" style="grid-template-columns:1fr">' +
      a.stats.map(function (s) { return '<div class="lxa-stat" style="display:flex;justify-content:space-between;align-items:center"><span style="font-size:13px;margin:0">' + esc(s.label) + '</span><b>' + esc(s.value) + '</b></div>'; }).join('') +
      '</div><h3 class="lxa-h3" style="margin-top:18px">Latest</h3>' +
      (a.last ? '<div class="lxa-last" style="min-height:0">' + esc(a.last.title) + ' · ' + esc(ago(a.last.at)) + '</div>' : '<div class="lxa-empty" style="padding:4px 0">Nothing yet today.</div>') +
      '<button class="lxa-btn sec" style="margin-top:14px" data-tab="history">See the full history</button></div></div>';
  }

  function field(r, edit) {
    var id = 'lxa-r-' + r.k, v = r.value, dis = edit ? '' : ' disabled', ctl;
    if (r.type === 'locked') {
      return '<div class="lxa-field"><label>' + esc(r.label) + '</label><span class="lxa-lock">🔒 Always on</span></div>';
    }
    if (r.type === 'info') {
      return '<div class="lxa-field"><label>' + esc(r.label) + '</label><span style="font-size:14px">' + esc(r.value) + '</span>' + (r.help ? '<small>' + esc(r.help) + '</small>' : '') + '</div>';
    }
    var attr = (r.extra ? 'data-x' : 'data-r') + '="' + esc(r.k) + '"';
    if (r.type === 'range') ctl = '<div class="lxa-range"><input type="range" id="' + id + '" ' + attr + ' min="' + r.min + '" max="' + r.max + '" step="' + r.step + '" value="' + esc(v) + '"' + dis + '><b data-show="' + esc(r.k) + '">' + esc(v) + esc(r.unit || '') + '</b></div>';
    else if (r.type === 'number') ctl = '<div class="lxa-range"><input type="number" id="' + id + '" ' + attr + ' min="' + r.min + '" max="' + r.max + '" step="' + r.step + '" value="' + esc(v) + '"' + dis + '><span style="font-size:12.5px;color:var(--mut);min-width:60px">' + esc(r.unit || '') + '</span></div>';
    else if (r.type === 'toggle') ctl = '<button class="lxa-switch' + (v ? ' on' : '') + '" role="switch" aria-checked="' + !!v + '" ' + attr + ' data-tog="1"' + dis + ' aria-label="' + esc(r.label) + '"></button>';
    else if (r.type === 'choice') ctl = '<select id="' + id + '" ' + attr + dis + '>' + r.options.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (String(v) === String(o[0]) ? ' selected' : '') + '>' + esc(o[1]) + '</option>'; }).join('') + '</select>';
    else if (r.type === 'time') ctl = '<input type="time" id="' + id + '" ' + attr + ' value="' + esc(v) + '"' + dis + '>';
    else ctl = '<input type="text" id="' + id + '" ' + attr + ' value="' + esc(v) + '"' + dis + '>';
    return '<div class="lxa-field"><label for="' + id + '">' + esc(r.label) + '</label>' + ctl + (r.help ? '<small>' + esc(r.help) + '</small>' : '') + '</div>';
  }

  function settingsTab(a) {
    var edit = P.data.canEdit;
    var extras = a.extras.map(function (x) { return Object.assign({ extra: x.type !== 'info' }, x); });
    return '<div class="lxa-glass lxa-panel"><h3 class="lxa-h3">Rules</h3>' + a.rules.map(function (r) { return field(r, edit); }).join('') +
      (extras.length ? '<h3 class="lxa-h3" style="margin-top:22px">Also used by this agent</h3>' + extras.map(function (r) { return field(r, edit); }).join('') : '') +
      (edit ? '<div class="lxa-save"><div><button class="lxa-btn" data-a="save">Save settings</button><button class="lxa-btn sec" data-a="reset">Discard changes</button></div></div>'
        : '<p class="lxa-note">Your role can view these settings. An admin or marketing manager can change them.</p>') + '</div>';
  }

  function permsTab(a) {
    var edit = P.data.canEdit;
    return '<div class="lxa-glass lxa-panel"><h3 class="lxa-h3">What it may do on its own</h3>' +
      '<p class="lxa-note" style="margin:0 0 6px">The level above sets these. Change one here to override it. <b>Ask first</b> waits for a person (for the daily plan, the plan approval covers it).</p>' +
      a.actions.map(function (x) {
        if (x.locked) return '<div class="lxa-perm"><div class="t">' + esc(x.label) + '<small>Locked for safety: always ' + esc(ACT[x.level] || x.level) + '</small></div><span class="lxa-lock">🔒 ' + esc(ACT[x.level]) + '</span></div>';
        return '<div class="lxa-perm"><div class="t">' + esc(x.label) + '<small>' + (x.override ? 'Changed by you (level default would be different). <a href="#" data-permreset="' + x.k + '">Reset</a>' : 'Follows the level') + '</small></div>' +
          '<div class="lxa-seg">' + ['off', 'suggest', 'ask', 'auto'].map(function (l) {
            return '<button data-perm="' + x.k + ':' + l + '" class="' + (x.level === l ? 'on' : '') + '"' + (edit && a.on && !P.data.paused ? '' : ' disabled') + '>' + ACT[l] + '</button>';
          }).join('') + '</div></div>';
      }).join('') + (a.on ? '' : '<p class="lxa-note">The agent is off, so every action is off.</p>') + '</div>';
  }

  function historyTab(a) {
    var H = P.hist;
    if (H.items === null && !H.loading) { H.loading = true; setTimeout(P.fetchHistory, 0); }
    var filters = [['all', 'All'], ['needs', 'Needs you'], ['done', 'Done'], ['other', 'Declined or undone'], ['notes', 'Notes']];
    var items = (H.items || []).filter(function (d) {
      if (H.filter === 'needs') return d.status === 'pending' || d.status === 'suggested';
      if (H.filter === 'done') return d.status === 'done' || d.status === 'approved';
      if (H.filter === 'other') return d.status === 'declined' || d.status === 'undone';
      if (H.filter === 'notes') return d.status === 'info';
      return true;
    });
    var bar = '<div class="lxa-hbar"><div class="lxa-seg">' + filters.map(function (f) { return '<button data-hf="' + f[0] + '" class="' + (H.filter === f[0] ? 'on' : '') + '">' + f[1] + '</button>'; }).join('') + '</div>' +
      '<div class="lxa-seg">' + [[1, 'Today'], [7, '7 days'], [30, '30 days'], [90, '90 days']].map(function (x) { return '<button data-hd="' + x[0] + '" class="' + (H.days === x[0] ? 'on' : '') + '">' + x[1] + '</button>'; }).join('') + '</div></div>';
    if (H.items === null) return '<div class="lxa-glass lxa-panel">' + bar + '<p class="lxa-empty">Loading…</p></div>';
    if (!items.length) return '<div class="lxa-glass lxa-panel">' + bar + '<p class="lxa-empty">Nothing here for this period.</p></div>';
    var out = '', day = '';
    items.forEach(function (d) {
      var dt = new Date(d.at), label = dt.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
      if (label !== day) { day = label; out += '<div class="lxa-day">' + esc(label) + '</div>'; }
      var st = STATUS[d.status] || [d.status, 'mute'];
      var acts = '';
      if (P.data.canRun && (d.status === 'pending' || d.status === 'suggested')) acts = '<button class="lxa-btn" data-v="' + d.id + ':approve">Approve</button><button class="lxa-btn sec" data-v="' + d.id + ':decline">Decline</button>';
      else if (P.data.canRun && d.undoable) acts = '<button class="lxa-btn sec" data-v="' + d.id + ':undo">Undo</button>';
      out += '<div class="lxa-dec"><div class="r1"><div><div class="tt">' + esc(d.title) + '</div><div class="meta">' + esc(dt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })) +
        (d.who ? ' · ' + esc(d.who) : '') + (d.rule ? ' · ' + esc(d.rule) : '') + (d.conf < 100 ? ' · ' + d.conf + '% certain' : '') + (d.decidedBy ? ' · by ' + esc(d.decidedBy) : '') + '</div></div>' +
        '<span class="lxa-pill ' + st[1] + '">' + st[0] + '</span></div>' + (d.reason ? '<div class="why">' + esc(d.reason) + '</div>' : '') + (acts ? '<div class="acts">' + acts + '</div>' : '') + '</div>';
    });
    return '<div class="lxa-glass lxa-panel">' + bar + out + '</div>';
  }

  // ------------------------------------------------------------------ events
  function collect() {
    var r = root(), rules = {}, extras = {}, a = agent(P.sel);
    Array.prototype.forEach.call(r.querySelectorAll('[data-r],[data-x]'), function (el) {
      var k = el.getAttribute('data-r') || el.getAttribute('data-x');
      var v = el.getAttribute('data-tog') ? el.classList.contains('on') : el.value;
      var orig = el.hasAttribute('data-r') ? (a.rules.filter(function (x) { return x.k === k; })[0] || {}).value : (a.extras.filter(function (x) { return x.k === k; })[0] || {}).value;
      if (String(v) !== String(orig)) (el.hasAttribute('data-r') ? rules : extras)[k] = v;
    });
    return { rules: rules, extras: extras };
  }

  function save(body, msg) {
    return api('PATCH', '/agents/' + P.sel, body).then(function (d) {
      P.data = d; P.editing = false; P.render(); toast(msg || 'Saved.'); syncConsole();
    }, function (e) { toast(e.message); });
  }

  function onInput(e) {
    var t = e.target;
    if (t.type === 'range') {
      var b = root().querySelector('[data-show="' + t.getAttribute('data-r') + '"]');
      var r = (agent(P.sel).rules.filter(function (x) { return x.k === t.getAttribute('data-r'); })[0] || {});
      if (b) b.textContent = t.value + (r.unit || '');
    }
    if (t.hasAttribute('data-r') || t.hasAttribute('data-x')) P.editing = true;
  }

  function onClick(e) {
    var el = e.target.closest('button,a');
    if (!el || el.disabled) return;
    var A = el.getAttribute('data-a');
    if (el.hasAttribute('data-open')) { P.sel = el.getAttribute('data-open'); P.tab = 'overview'; P.hist = { filter: 'all', days: 7, items: null, loading: false }; P.render(); window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
    if (A === 'back') { P.sel = null; P.editing = false; P.render(); return; }
    if (el.hasAttribute('data-tab')) {
      if (P.editing && !confirm('Leave without saving your changes?')) return;
      P.editing = false; P.tab = el.getAttribute('data-tab');
      if (P.tab === 'history') { P.hist.items = null; P.hist.loading = false; }
      P.render(); return;
    }
    if (A === 'run') {
      el.disabled = true; el.textContent = 'Running…';
      LX.api('POST', '/workspaces/' + P.ws + '/tick').then(function (r) {
        toast('The agents ran: ' + (r.sent || 0) + ' emails sent, ' + (r.replies || 0) + ' replies read.'); P.load(); if (P.comp && P.comp.srvRefreshAll) P.comp.srvRefreshAll();
      }, function (er) { toast(er.message); P.render(); });
      return;
    }
    if (A === 'pauseall') {
      var to = !P.data.paused;
      if (to && !confirm('Pause all five agents? Nothing will be planned or sent until you resume.')) return;
      LX.api('PATCH', '/workspaces/' + P.ws, { agents_paused: to }).then(function () { toast(to ? 'All agents paused.' : 'All agents resumed.'); P.load(); syncConsole(); }, function (er) { toast(er.message); });
      return;
    }
    if (A === 'toggle') { var on = !agent(P.sel).on; save({ on: on }, agent(P.sel).name + (on ? ' is on.' : ' is off.')); return; }
    if (el.hasAttribute('data-level')) {
      var lv = el.getAttribute('data-level');
      if (lv === agent(P.sel).level) return;
      if (!confirm('Change to “' + LEVEL[lv] + '”? ' + LEVEL_HELP[lv] + ' Any permissions you changed by hand go back to the level default.')) return;
      save({ level: lv }, 'Now: ' + LEVEL[lv] + '.'); return;
    }
    if (el.getAttribute('data-tog')) { el.classList.toggle('on'); el.setAttribute('aria-checked', el.classList.contains('on')); P.editing = true; return; }
    if (A === 'save') {
      var c = collect();
      if (!Object.keys(c.rules).length && !Object.keys(c.extras).length) { toast('Nothing changed.'); return; }
      save(c, 'Settings saved. The agent uses them on its next run.'); return;
    }
    if (A === 'reset') { P.editing = false; P.render(); return; }
    if (el.hasAttribute('data-perm')) {
      var pp = el.getAttribute('data-perm').split(':'), body = { perms: {} };
      body.perms[pp[0]] = pp[1];
      save(body, 'Permission saved.'); return;
    }
    if (el.hasAttribute('data-permreset')) { e.preventDefault(); var b2 = { perms: {} }; b2.perms[el.getAttribute('data-permreset')] = null; save(b2, 'Back to the level default.'); return; }
    if (el.hasAttribute('data-hf')) { P.hist.filter = el.getAttribute('data-hf'); P.render(); return; }
    if (el.hasAttribute('data-hd')) { P.hist.days = +el.getAttribute('data-hd'); P.loadHistory(); return; }
    if (el.hasAttribute('data-v')) {
      var v = el.getAttribute('data-v').split(':');
      el.disabled = true;
      LX.api('POST', '/workspaces/' + P.ws + '/decisions/' + v[0] + '/' + v[1]).then(function () {
        toast({ approve: 'Approved.', decline: 'Declined.', undo: 'Undone.' }[v[1]]); P.hist.items = null; P.hist.loading = false; P.load();
        if (P.comp && P.comp.srvRefreshAll) P.comp.srvRefreshAll();
      }, function (er) { toast(er.message); el.disabled = false; });
    }
  }

  // ------------------------------------------------------------------ called by the console on every update
  P.sync = function (comp) {
    P.comp = comp;
    var r = root();
    if (!r) { if (P._poll) { clearInterval(P._poll); P._poll = null; } return; }
    var ws = LX.currentWs;
    if (!ws) return;
    if (r.getAttribute('data-ws') !== String(ws)) {
      r.setAttribute('data-ws', String(ws));
      P.ws = ws; P.sel = null; P.data = null; P.editing = false; P.render(); P.load();
    } else if (!r.firstChild) { P.render(); }
    if (!P._poll) P._poll = setInterval(function () { if (root() && !P.editing && document.visibilityState === 'visible') P.load(true); }, 30000);
  };
})();
