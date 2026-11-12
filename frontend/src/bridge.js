/* LOOPX server bridge.
 * Loaded before the console. When the page is served by the LOOPX server it:
 *  - exposes window.LX.api() for the console's server calls
 *  - replaces the design preview's window.claude with one that asks the server's Claude connection
 *  - adds the "Connections" panel (sender, reply inbox, store connectors, Events API, imports, test replies)
 * Opened as a plain file (no server), it does nothing and the console runs its sample data.
 */
(function () {
  var LX = (window.LX = { server: false, health: null, currentWs: null, comp: null });
  try {
    var x = new XMLHttpRequest();
    x.open('GET', '/api/health', false);
    x.send();
    if (x.status === 200) { LX.health = JSON.parse(x.responseText); LX.server = true; }
  } catch (e) { /* opened as a file */ }
  if (!LX.server) return;

  LX.api = function (method, path, body, raw) {
    var opt = { method: method, credentials: 'same-origin', headers: {} };
    if (body !== undefined) {
      if (raw) { opt.body = body; } else { opt.headers['content-type'] = 'application/json'; opt.body = JSON.stringify(body); }
    }
    return fetch('/api' + path, opt).then(function (r) {
      return r.text().then(function (t) {
        var j = null;
        try { j = t ? JSON.parse(t) : null; } catch (e) {}
        if (!r.ok) {
          var d = j && j.detail;
          var err = new Error(typeof d === 'string' ? d : (d && d.message) || 'Request failed (' + r.status + ')');
          err.status = r.status; err.code = d && d.code;
          throw err;
        }
        return j;
      });
    });
  };

  // The console asks window.claude.use('sample') for Claude. Route it to the server.
  window.claude = {
    use: function (name) {
      if (name !== 'sample' || !LX.health.ai) return Promise.resolve(null);
      var wrap = function (e) { throw { code: e.code || (e.status === 429 ? 'rate_limited' : 'unavailable'), message: e.message }; };
      var f = function (input, opts) { return LX.api('POST', '/ai/sample', { input: input, opts: opts || {} }).catch(wrap); };
      f.json = function (input, opts) {
        return LX.api('POST', '/ai/sample', { input: input, opts: opts || {}, json: true }).then(function (r) {
          if (r.json == null) throw { code: 'bad_json', message: 'Claude did not return JSON.' };
          return r.json;
        }, wrap);
      };
      return Promise.resolve(f);
    }
  };

  // ------------------------------------------------------------------ Connections panel
  var css = '' +
    '#lxp-btn{position:fixed;right:18px;bottom:18px;z-index:9998;background:#5B3DF5;color:#fff;border:0;border-radius:999px;padding:10px 16px;font:600 13px Figtree,Arial,sans-serif;box-shadow:0 6px 20px rgba(60,30,160,.35);cursor:pointer}' +
    '#lxp-wrap{position:fixed;inset:0;z-index:9999;background:rgba(20,14,40,.45);display:none;align-items:center;justify-content:center}' +
    '#lxp{width:min(880px,94vw);max-height:90vh;overflow:auto;background:#fff;border-radius:16px;box-shadow:0 20px 60px rgba(20,14,40,.35);font:14px/1.5 Figtree,Arial,sans-serif;color:#1E1638}' +
    '#lxp header{display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid #ECEAF4;position:sticky;top:0;background:#fff;z-index:1}' +
    '#lxp h2{font-size:18px;margin:0}#lxp h3{font-size:15px;margin:18px 0 8px}#lxp .tabs{display:flex;gap:6px;flex-wrap:wrap;padding:12px 22px 0}' +
    '#lxp .tab{border:1px solid #E4E1F0;background:#F7F6FB;border-radius:999px;padding:6px 12px;cursor:pointer;font:inherit;font-size:13px}#lxp .tab.on{background:#5B3DF5;color:#fff;border-color:#5B3DF5}' +
    '#lxp .body{padding:6px 22px 22px}#lxp label{display:block;font-size:12px;color:#5D5478;margin:10px 0 4px}' +
    '#lxp input,#lxp select,#lxp textarea{width:100%;box-sizing:border-box;border:1px solid #DCD8EA;border-radius:8px;padding:8px 10px;font:inherit}' +
    '#lxp .row{display:grid;grid-template-columns:1fr 1fr;gap:10px}#lxp .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}' +
    '#lxp .btn{background:#5B3DF5;color:#fff;border:0;border-radius:8px;padding:8px 14px;font:600 13px inherit;cursor:pointer;margin:12px 8px 0 0}' +
    '#lxp .btn.sec{background:#F1EEFD;color:#3B2A9A}#lxp .msg{margin-top:10px;font-size:13px;color:#0A7A55}#lxp .err{color:#B42318}' +
    '#lxp code,#lxp pre{background:#F7F6FB;border-radius:6px;padding:2px 6px;font:12px ui-monospace,Menlo,monospace}#lxp pre{padding:10px;white-space:pre-wrap;word-break:break-all}' +
    '#lxp table{width:100%;border-collapse:collapse;font-size:12.5px}#lxp td,#lxp th{border-bottom:1px solid #F0EEF6;padding:6px 4px;text-align:left;vertical-align:top}' +
    '#lxp .pill{display:inline-block;border-radius:999px;padding:1px 8px;font-size:11.5px;background:#F1EEFD;color:#3B2A9A}#lxp .ok{background:#E6F6EF;color:#0A7A55}#lxp .warn{background:#FFF3E0;color:#9A5B00}' +
    '#lxp .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}#lxp .card{border:1px solid #ECEAF4;border-radius:10px;padding:10px 12px}#lxp .card b{display:block;font-size:16px}';

  var tab = 'status';
  function el(h) { var d = document.createElement('div'); d.innerHTML = h; return d.firstElementChild; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function val(id) { var e = document.getElementById(id); return e ? e.value : ''; }
  function say(id, text, bad) { var e = document.getElementById(id); if (e) { e.textContent = text; e.className = 'msg' + (bad ? ' err' : ''); } }
  function wsPath(p) { return '/workspaces/' + LX.currentWs + (p || ''); }
  function refreshConsole() { if (LX.comp && LX.comp.srvRefreshAll) LX.comp.srvRefreshAll(); }

  function open() {
    if (!LX.currentWs) {
      var name = LX.sampleName || 'this sample client';
      if (LX.comp && LX.comp.srvAdopt && confirm(name + ' is a sample with demo data. Create a real ' + name + ' on the server, with the same playbook and no customers yet?')) LX.comp.srvAdopt();
      return;
    }
    document.getElementById('lxp-wrap').style.display = 'flex';
    render();
  }
  function close() { document.getElementById('lxp-wrap').style.display = 'none'; }

  function render() {
    var box = document.getElementById('lxp');
    var tabs = [['status', 'Status'], ['office', 'Office test'], ['plan', "Today's plan"], ['sender', 'Sender'], ['inbox', 'Reply inbox'], ['stores', 'Store connectors'], ['api', 'Events API'], ['import', 'Import file'], ['reply', 'Test a reply'], ['team', 'Team']];
    box.innerHTML = '<header><h2>Connections</h2><button class="btn sec" id="lxp-x" style="margin:0">Close</button></header>' +
      '<div class="tabs">' + tabs.map(function (t) { return '<button class="tab' + (t[0] === tab ? ' on' : '') + '" data-t="' + t[0] + '">' + t[1] + '</button>'; }).join('') + '</div>' +
      '<div class="body" id="lxp-body">Loading…</div>';
    box.querySelector('#lxp-x').onclick = close;
    Array.prototype.forEach.call(box.querySelectorAll('.tab'), function (b) { b.onclick = function () { tab = b.getAttribute('data-t'); render(); }; });
    ({ status: pStatus, office: pOffice, plan: pPlan, sender: pSender, inbox: pInbox, stores: pStores, api: pApi, import: pImport, reply: pReply, team: pTeam })[tab]();
  }
  function body(h) { document.getElementById('lxp-body').innerHTML = h; }

  function pStatus() {
    Promise.all([LX.api('GET', wsPath('/live')), LX.api('GET', '/health')]).then(function (r) {
      var L = r[0], H = r[1], rs = L.rs, p = L.plan;
      body('<div class="grid">' +
        [['Emails sent today', rs.sent], ['Opened', rs.opened], ['Replies', rs.replied], ['Returned', rs.returned]].map(function (x) { return '<div class="card">' + x[0] + '<b>' + x[1] + '</b></div>'; }).join('') + '</div>' +
        '<h3>Server</h3><table>' +
        '<tr><td>Sending</td><td><span class="pill ' + (H.mail === 'console' ? 'warn' : 'ok') + '">' + esc(H.mail) + '</span> ' + (H.mail === 'console' ? 'Emails are written to data/outbox/outbox.jsonl, not sent. Set MAIL_PROVIDER=brevo or smtp.' : '') + '</td></tr>' +
        '<tr><td>Claude (AI)</td><td><span class="pill ' + (H.ai ? 'ok' : 'warn') + '">' + (H.ai ? 'on' : 'off') + '</span> ' + (H.ai ? '' : 'Set ANTHROPIC_API_KEY. Without it the agents use rules and no AI text.') + '</td></tr>' +
        '<tr><td>Database</td><td>' + esc(H.db) + '</td></tr>' +
        '<tr><td>Reply inbox</td><td>' + (L.imap ? '<span class="pill ok">connected</span>' : '<span class="pill warn">not set up</span>') + (L.imapError ? ' <span class="err">' + esc(L.imapError) + '</span>' : '') + '</td></tr>' +
        '<tr><td>Daily sending limit left</td><td>' + L.quotaLeft + ' of ' + H.daily_limit + '</td></tr>' +
        '<tr><td>Agents last ran</td><td>' + (L.lastTick ? new Date(L.lastTick + 'Z').toLocaleString() : 'not yet') + (L.paused ? ' <span class="pill warn">all paused</span>' : '') + '</td></tr>' +
        '<tr><td>Today\'s plan</td><td>' + (p ? esc(p.status) + ' · ' + (p.summary.emails || 0) + ' emails, ' + (p.summary.holdout || 0) + ' holdout, ' + (p.summary.held || 0) + ' held' : 'not built yet') + '</td></tr>' +
        '</table><button class="btn" id="lxp-run">Run the agents now</button><button class="btn sec" id="lxp-build">Rebuild today\'s plan</button><div id="lxp-m"></div>');
      document.getElementById('lxp-run').onclick = function () {
        say('lxp-m', 'Running…');
        LX.api('POST', wsPath('/tick')).then(function (r) { say('lxp-m', 'Done: ' + (r.sent || 0) + ' emails sent, ' + (r.replies || 0) + ' replies read.'); refreshConsole(); setTimeout(pStatus, 600); }, function (e) { say('lxp-m', e.message, 1); });
      };
      document.getElementById('lxp-build').onclick = function () {
        LX.api('POST', wsPath('/plan/build')).then(function (r) { say('lxp-m', 'Plan rebuilt: ' + esc(r.plan.status) + ', ' + (r.plan.summary.emails || 0) + ' emails.'); refreshConsole(); }, function (e) { say('lxp-m', e.message, 1); });
      };
    }, function (e) { body('<p class="err">' + esc(e.message) + '</p>'); });
  }

  function pOffice() {
    LX.api('GET', '/health').then(function (H) {
      var live = H.mail !== 'console';
      body('<p>Add real people, such as colleagues who agreed to help, as test customers. Each one gets a purchase history that makes them due for a reminder <b>today</b>, so the Timing agent picks them, Writer fills their email, Guard sends a test wave, and Listener reads their replies. Test people are never put in the holdout group.</p>' +
        (live ? '<p><span class="pill ok">Real sending is on (' + esc(H.mail) + ')</span> These people will really get an email after you approve the plan.</p>'
              : '<p><span class="pill warn">Sending is off (console)</span> Emails are only saved to data/outbox. To really send, set MAIL_PROVIDER=smtp (or brevo) in .env and restart. See the steps from Claude.</p>') +
        '<label>Email addresses (one per line, or "Name &lt;email&gt;")</label><textarea id="o-e" rows="6" placeholder="Priya Nair <priya@yourcompany.com>\nrahul@yourcompany.com"></textarea>' +
        '<button class="btn" id="o-add">Add as test people</button><button class="btn sec" id="o-plan">Rebuild today\'s plan</button><div id="o-m"></div>' +
        '<h3>Then</h3><ol><li>Click <b>Rebuild today\'s plan</b> (or wait for the next agent run).</li><li>In LOOPX go to <b>Today\'s plan</b> and click <b>Approve the plan</b>.</li><li>The test wave goes first. After WAVE_WAIT_MINUTES Guard checks it and sends the rest.</li><li>Ask a colleague to reply "please remove me" to see the Listener unsubscribe them (needs the Reply inbox set up).</li></ol>' +
        '<p><small>Sample addresses at example.com are never emailed: Guard holds them.</small></p>');
      document.getElementById('o-add').onclick = function () {
        LX.api('POST', wsPath('/test-people'), { emails: val('o-e') }).then(function (r) {
          var names = { wk: 'weekly', bi: 'every 14 days', mo: 'monthly' };
          say('o-m', r.added.length ? 'Added ' + r.added.length + ': ' + r.added.map(function (a) { return a.email + ' (' + names[a.campaign] + ')'; }).join(', ') + '.' + (r.rejected.length ? ' Not added: ' + r.rejected.join(', ') + '.' : '') : 'Nobody added. ' + (r.rejected.length ? 'Not valid: ' + r.rejected.join(', ') : 'Paste at least one email address.'), !r.added.length);
          refreshConsole();
        }, function (e) { say('o-m', e.message, 1); });
      };
      document.getElementById('o-plan').onclick = function () {
        LX.api('POST', wsPath('/plan/build')).then(function (r) {
          var p = r.plan;
          say('o-m', p.status === 'draft' ? "Today's plan now has " + (p.summary.emails || 0) + " emails waiting for approval. Go to Today's plan and approve it." :
            p.status === 'done' ? 'No new people are due. Add test people first.' : "Today's plan is " + p.status + ". It can be rebuilt once it has finished sending.", p.status !== 'draft');
          refreshConsole();
        }, function (e) { say('o-m', e.message, 1); });
      };
    });
  }

  function pPlan() {
    LX.api('GET', wsPath('/plan')).then(function (r) {
      if (!r.plan) { body('<p>No plan yet today. The Timing agent builds it after ' + 'PLAN_HOUR, or click “Rebuild today\'s plan” on the Status tab.</p>'); return; }
      body('<p>Status: <b>' + esc(r.plan.status) + '</b>' + (r.plan.approvedBy ? ' · approved by ' + esc(r.plan.approvedBy) : '') + '. First 60 emails:</p><table><tr><th>To</th><th>Campaign</th><th>Subject</th><th>Status</th></tr>' +
        r.sends.map(function (s) { return '<tr><td>' + esc(s.who) + '<br><small>' + esc(s.to) + '</small></td><td>' + esc(s.campaign) + (s.wave ? ' <span class="pill">test wave</span>' : '') + '</td><td>' + esc(s.subject) + ' <small>(' + s.variant + ')</small></td><td><span class="pill">' + esc(s.status) + '</span>' + (s.hold ? '<br><small>' + esc(s.hold) + '</small>' : '') + '</td></tr>'; }).join('') + '</table>' +
        (r.sends[0] ? '<h3>Example email</h3><pre>' + esc(r.sends[0].subject + '\n\n' + r.sends[0].body) + '</pre>' : ''));
    });
  }

  function pSender() {
    LX.api('GET', wsPath()).then(function (r) {
      var s = r.workspace.settings || {};
      body('<p>How this client\'s reminders look in the inbox, and the dates the Timing agent respects.</p><div class="row"><div><label>From name</label><input id="s-fn" value="' + esc(s.from_name || r.workspace.name) + '"></div><div><label>From email (verified with your provider)</label><input id="s-fe" value="' + esc(s.from_email || '') + '" placeholder="reminders@yourdomain.com"></div></div>' +
        '<div class="row"><div><label>Reply-to (the inbox the Listener reads)</label><input id="s-rt" value="' + esc(s.reply_to || '') + '"></div><div><label>Website (the button in each email)</label><input id="s-web" value="' + esc(s.website || '') + '"></div></div>' +
        '<div class="row3"><div><label>Time zone</label><input id="s-tz" value="' + esc(s.timezone || 'Asia/Kolkata') + '"></div><div><label>Country for bank holidays (e.g. IN, US, GB)</label><input id="s-co" value="' + esc(s.country || '') + '"></div><div><label>Team email for complaints</label><input id="s-ow" value="' + esc(s.owner_email || '') + '"></div></div>' +
        '<button class="btn" id="s-save">Save</button><div id="s-m"></div>');
      document.getElementById('s-save').onclick = function () {
        LX.api('PATCH', wsPath(), { settings: { from_name: val('s-fn'), from_email: val('s-fe'), reply_to: val('s-rt'), website: val('s-web'), timezone: val('s-tz'), country: val('s-co').toUpperCase(), owner_email: val('s-ow') } })
          .then(function () { say('s-m', 'Saved.'); }, function (e) { say('s-m', e.message, 1); });
      };
    });
  }

  function pInbox() {
    LX.api('GET', wsPath()).then(function (r) {
      var i = (r.workspace.settings || {}).imap || {};
      body('<p>The Listener agent reads replies from this mailbox over IMAP. For Gmail: turn on 2-step verification, create an <b>app password</b>, host <code>imap.gmail.com</code>. Leave empty to use the server-wide IMAP_* settings.</p>' +
        '<div class="row"><div><label>IMAP host</label><input id="i-h" value="' + esc(i.host || '') + '" placeholder="imap.gmail.com"></div><div><label>Folder</label><input id="i-f" value="' + esc(i.folder || 'INBOX') + '"></div></div>' +
        '<div class="row"><div><label>User</label><input id="i-u" value="' + esc(i.user || '') + '"></div><div><label>App password</label><input id="i-p" type="password" value="' + esc(i.password || '') + '"></div></div>' +
        '<button class="btn" id="i-save">Save</button><div id="i-m"></div>');
      document.getElementById('i-save').onclick = function () {
        LX.api('PATCH', wsPath(), { settings: { imap: { host: val('i-h'), folder: val('i-f'), user: val('i-u'), password: val('i-p') } } })
          .then(function () { say('i-m', 'Saved. The Listener reads new mail on the next agent run.'); }, function (e) { say('i-m', e.message, 1); });
      };
    });
  }

  function pStores() {
    LX.api('GET', wsPath()).then(function (r) {
      var c = (r.workspace.settings || {}).connectors || {};
      var sh = c.shopify || {}, st = c.stripe || {}, wo = c.woocommerce || {};
      function last(x) { return x.last_sync ? '<small>Last sync ' + new Date(x.last_sync + 'Z').toLocaleString() + '</small>' : ''; }
      body('<p>Orders from these stores become purchase events. Timing uses them to predict return dates, and Analyst uses them to count returns. They sync every hour.</p>' +
        '<h3>Shopify</h3><div class="row"><div><label>Shop domain</label><input id="c-sh" value="' + esc(sh.shop || '') + '" placeholder="yourstore.myshopify.com"></div><div><label>Admin API access token (read_orders, read_customers)</label><input id="c-st" value="' + esc(sh.token || '') + '"></div></div>' + last(sh) +
        '<button class="btn" data-c="shopify">Save</button><button class="btn sec" data-s="shopify">Sync now</button>' +
        '<h3>Stripe</h3><label>Restricted key (read-only: Charges, Customers)</label><input id="c-sk" value="' + esc(st.key || '') + '">' + last(st) +
        '<button class="btn" data-c="stripe">Save</button><button class="btn sec" data-s="stripe">Sync now</button>' +
        '<h3>WooCommerce</h3><div class="row3"><div><label>Store URL</label><input id="c-wu" value="' + esc(wo.url || '') + '"></div><div><label>Consumer key</label><input id="c-wk" value="' + esc(wo.key || '') + '"></div><div><label>Consumer secret</label><input id="c-ws" value="' + esc(wo.secret || '') + '"></div></div>' + last(wo) +
        '<button class="btn" data-c="woocommerce">Save</button><button class="btn sec" data-s="woocommerce">Sync now</button><div id="c-m"></div>');
      var vals = { shopify: function () { return { shop: val('c-sh'), token: val('c-st') }; }, stripe: function () { return { key: val('c-sk') }; }, woocommerce: function () { return { url: val('c-wu'), key: val('c-wk'), secret: val('c-ws') }; } };
      Array.prototype.forEach.call(document.querySelectorAll('#lxp [data-c]'), function (b) {
        b.onclick = function () { var n = b.getAttribute('data-c'); LX.api('PUT', wsPath('/connectors/' + n), vals[n]()).then(function () { say('c-m', n + ' saved.'); }, function (e) { say('c-m', e.message, 1); }); };
      });
      Array.prototype.forEach.call(document.querySelectorAll('#lxp [data-s]'), function (b) {
        b.onclick = function () {
          var n = b.getAttribute('data-s'); say('c-m', 'Syncing ' + n + '…');
          LX.api('POST', wsPath('/connectors/' + n + '/sync')).then(function (r) { if (r.ok) { say('c-m', n + ': ' + r.fetched + ' orders read, ' + r.added + ' new.'); refreshConsole(); } else say('c-m', r.error, 1); }, function (e) { say('c-m', e.message, 1); });
        };
      });
    });
  }

  function pApi() {
    LX.api('GET', wsPath()).then(function (r) {
      var key = r.workspace.api_key, base = location.origin;
      body('<p>Your own system can send customers and purchases straight to this client. Each purchase updates the customer\'s pattern and counts as a return if it follows a reminder.</p>' +
        '<label>API key for ' + esc(r.workspace.name) + '</label><input readonly value="' + esc(key) + '">' +
        '<h3>Send a purchase</h3><pre>curl -X POST ' + base + '/api/v1/events \\\n  -H "X-API-Key: ' + esc(key) + '" -H "Content-Type: application/json" \\\n  -d \'{"events":[{"email":"asha@example.com","at":"2026-11-11T10:30:00Z","kind":"purchase","amount":499,"external_id":"order-1001","first":"Asha"}]}\'</pre>' +
        '<h3>Add or update customers</h3><pre>curl -X POST ' + base + '/api/v1/contacts \\\n  -H "X-API-Key: ' + esc(key) + '" -H "Content-Type: application/json" \\\n  -d \'{"contacts":[{"email":"asha@example.com","first":"Asha","company":"Asha Stores","subscribed":true}]}\'</pre>' +
        '<p><small>external_id makes calls safe to repeat: the same order is never counted twice.</small></p>');
    });
  }

  function pImport() {
    body('<p>Upload a CSV of <b>customers</b> (one row each: email, first name, first and last order date, number of orders) or of <b>orders</b> (one row per order: email, order date, amount, order id). LOOPX detects which one it is from the column names.</p>' +
      '<input type="file" id="f-file" accept=".csv,text/csv"><button class="btn" id="f-go">Import</button><div id="f-m"></div>' +
      '<p><small>The Customers screen\'s own importer also saves to the server.</small></p>');
    document.getElementById('f-go').onclick = function () {
      var f = document.getElementById('f-file').files[0];
      if (!f) { say('f-m', 'Choose a file first.', 1); return; }
      var fd = new FormData(); fd.append('file', f);
      say('f-m', 'Importing…');
      LX.api('POST', wsPath('/import'), fd, true).then(function (r) {
        say('f-m', 'Imported ' + r.kind + ': ' + (r.added || 0) + ' added' + (r.updated != null ? ', ' + r.updated + ' updated' : '') + (r.duplicates ? ', ' + r.duplicates + ' already known' : '') + ', ' + (r.skipped || 0) + ' skipped.');
        refreshConsole();
      }, function (e) { say('f-m', e.message, 1); });
    };
  }

  function pReply() {
    body('<p>Try the Listener without waiting for real mail: paste a reply as if a customer sent it.</p>' +
      '<div class="row"><div><label>From (a customer\'s email)</label><input id="r-f"></div><div><label>Subject</label><input id="r-s" value="Re: your reminder"></div></div>' +
      '<label>Reply</label><textarea id="r-b" rows="4" placeholder="Please take me off this list."></textarea><button class="btn" id="r-go">Give it to the Listener</button><div id="r-m"></div><h3>Recent replies</h3><div id="r-list">Loading…</div>');
    function list() {
      LX.api('GET', wsPath('/replies')).then(function (r) {
        document.getElementById('r-list').innerHTML = r.replies.length ? '<table>' + r.replies.map(function (x) { return '<tr><td>' + esc(x.from) + '</td><td><span class="pill">' + esc(x.category) + ' ' + x.conf + '%</span></td><td>' + esc(x.summary || x.body.slice(0, 120)) + '</td></tr>'; }).join('') + '</table>' : '<p>No replies yet.</p>';
      });
    }
    list();
    document.getElementById('r-go').onclick = function () {
      LX.api('POST', wsPath('/replies'), { from: val('r-f'), subject: val('r-s'), body: val('r-b') }).then(function (r) {
        say('r-m', r.reply ? 'Listener read it as “' + r.reply.category + '” (' + r.reply.conf + '% certain). See the Listener agent\'s decisions.' : 'Already read.');
        list(); refreshConsole();
      }, function (e) { say('r-m', e.message, 1); });
    };
  }

  function pTeam() {
    LX.api('GET', '/team').then(function (r) {
      body('<p>Everyone who can sign in. New sign-ups start as Viewer; an admin raises the role here.</p><table><tr><th>Name</th><th>Email</th><th>Role</th></tr>' +
        r.team.map(function (u) { return '<tr><td>' + esc(u.name) + '</td><td>' + esc(u.email) + '</td><td><select data-u="' + u.id + '">' + ['Admin', 'Marketing manager', 'Approver', 'Analyst', 'Viewer'].map(function (x) { return '<option' + (x === u.role ? ' selected' : '') + '>' + x + '</option>'; }).join('') + '</select></td></tr>'; }).join('') + '</table>' +
        '<h3>Add a person</h3><div class="row3"><div><label>Name</label><input id="t-n"></div><div><label>Email</label><input id="t-e"></div><div><label>Temporary password</label><input id="t-p"></div></div><label>Role</label><select id="t-r"><option>Viewer</option><option>Analyst</option><option>Approver</option><option>Marketing manager</option><option>Admin</option></select><button class="btn" id="t-add">Add</button><div id="t-m"></div>');
      Array.prototype.forEach.call(document.querySelectorAll('#lxp select[data-u]'), function (s) {
        s.onchange = function () { LX.api('PATCH', '/team/' + s.getAttribute('data-u'), { role: s.value }).then(function () { say('t-m', 'Role saved.'); }, function (e) { say('t-m', e.message, 1); }); };
      });
      document.getElementById('t-add').onclick = function () {
        LX.api('POST', '/team', { name: val('t-n'), email: val('t-e'), password: val('t-p'), role: val('t-r') }).then(function () { pTeam(); }, function (e) { say('t-m', e.message, 1); });
      };
    }, function (e) { body('<p class="err">' + esc(e.message) + '</p>'); });
  }

  function mount() {
    if (document.getElementById('lxp-btn')) return;
    var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
    var b = el('<button id="lxp-btn" title="Server settings for this client" style="display:none">Connections</button>');
    b.onclick = open;
    document.body.appendChild(b);
    var w = el('<div id="lxp-wrap"><div id="lxp"></div></div>');
    w.onclick = function (e) { if (e.target === w) close(); };
    document.body.appendChild(w);
  }
  LX.showButton = function (on, sampleName) {
    mount();
    var b = document.getElementById('lxp-btn');
    if (!b) return;
    LX.sampleName = sampleName || '';
    b.style.display = on ? 'block' : 'none';
    var label = sampleName ? 'Make this client real' : 'Connections';
    if (b.textContent !== label) b.textContent = label;
  };
})();
