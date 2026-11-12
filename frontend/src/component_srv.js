// ---- LOOPX server mode (added by scripts/build_frontend.py) ----
// Workspaces with a `sid` live on the LOOPX server; the five agents run there.
// Sample workspaces keep the design's built-in demo data.
srv() { return !!(typeof window !== 'undefined' && window.LX && window.LX.server); }

srvWs() { const w = this.ws(); return this.srv() && w && w.sid ? w : null; }

srvApi(method, path, body, raw) { return window.LX.api(method, path, body, raw); }

srvRec(w) {
const h = this.strHash(w.name);
const pbk = w.playbook && Object.keys(w.playbook).length ? Object.assign({ v: 1 }, w.playbook, { industry: w.playbook.industry || w.industry }) : null;
return { id: 's' + w.id, sid: w.id, company: w.name, industry: w.industry, pb: pbk ? 'custom' : '', seed: 11 + (h % 9000), k: 0, theme: w.theme || 'violet', sample: false, custom: pbk || undefined };
}

srvBoot() {
if (this._srvBooting) return;
this._srvBooting = true;
this.srvApi('GET', '/auth/me').then((r) => {
const u = r.user;
this.setState({ authed: true, profile: Object.assign({}, this.state.profile || {}, { name: u.name || u.email, email: u.email, role: u.role }), myRole: u.role, asRole: u.role });
return this.srvApi('GET', '/workspaces');
}).then((r) => {
this._srvBooting = false;
if (!r) return;
const st = this.state;
const samples = (st.workspaces || []).filter((w) => w.sample !== false && !w.sid);
const recs = r.workspaces.map((w) => this.srvRec(w));
const list = recs.concat(samples);
let cur = list.find((w) => w.id === st.wsId);
let picked = false;
try { picked = localStorage.getItem('loopx-srv-picked') === '1'; localStorage.setItem('loopx-srv-picked', '1'); } catch (e) {}
if (cur && !cur.sid && recs.length && !picked) cur = null;  // first visit: open a real client, not a sample
this.setState({ workspaces: list });
if (!cur && recs.length) this.later(40, () => this.switchWs(recs[0].id));
else if (!cur && list.length) this.later(40, () => this.switchWs(list[0].id));
else if (cur && cur.sid) this.srvLoadWs(cur);
}).catch((e) => {
this._srvBooting = false;
if (e && e.status === 401) this.setState({ authed: false, booting: false });
});
}

srvCfgKey(s) { return JSON.stringify([s.agentsCfg, s.opt, s.cap, !!s.agentsPaused]); }

srvLoadWs(rec) {
const id = rec.sid;
this._srvLoaded = null;
Promise.all([this.srvApi('GET', '/workspaces/' + id), this.srvApi('GET', '/workspaces/' + id + '/contacts'), this.srvApi('GET', '/workspaces/' + id + '/audit')]).then((res) => {
const cur = this.srvWs();
if (!cur || cur.sid !== id) return;
const W = res[0].workspace;
const base = this.baseState().agentsCfg;
const cfg = {};
Object.keys(base).forEach((k) => { cfg[k] = Object.assign({}, base[k], (W.agents || {})[k] || {}); });
const audit = (res[2].audit || []).map((x) => ({ id: 'a' + x.id, t: new Date(x.at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }), who: x.who, text: x.text }));
const patch = { contacts: res[1].contacts, agentsCfg: cfg, opt: Object.assign({}, this.state.opt, W.opt || {}), cap: W.cap || 2, agentsPaused: !!W.agents_paused, agAudit: audit, cver: (this.state.cver || 0) + 1, srvLive: null, srvDecs: [], launched: [] };
this._srvSynced = this.srvCfgKey(patch);
this._srvLoaded = id;
this._srvSeenInit = false;
this._srvSeen = {};
this._ck = null;
this.setState(patch);
this.srvPoll();
this.srvLoadReport();
}).catch((e) => this.notify('Could not load this client from the server: ' + e.message));
}

srvReloadAgents() {
// the Agents page saved settings on the server: take them into the console too
const w = this.srvWs();
if (!w) return;
this.srvApi('GET', '/workspaces/' + w.sid).then((r) => {
const W = r.workspace;
const base = this.baseState().agentsCfg;
const cfg = {};
Object.keys(base).forEach((k) => { cfg[k] = Object.assign({}, base[k], (W.agents || {})[k] || {}); });
const patch = { agentsCfg: cfg, opt: Object.assign({}, this.state.opt, W.opt || {}), cap: W.cap || 2, agentsPaused: !!W.agents_paused };
this._srvSynced = this.srvCfgKey(Object.assign({}, this.state, patch));
this.setState(patch);
this.srvPoll();
}).catch(() => {});
}

srvRefreshAll() {
const w = this.srvWs();
if (!w) return;
this.srvReloadContacts();
this.srvPoll();
this.srvLoadReport();
this.srvLoadAudit();
}

srvSyncCheck() {
if (typeof window !== 'undefined' && window.LX) {
const w0 = this.srvWs();
window.LX.currentWs = w0 ? w0.sid : null;
window.LX.comp = this;
if (window.LX.showButton) window.LX.showButton(!!(this.state.authed && !this.state.booting), w0 ? '' : this.ws().company);
if (window.LX.agentsPage) window.LX.agentsPage.sync(this);
}
const w = this.srvWs();
if (!w || this._srvLoaded !== w.sid) return;
if (this.srvCfgKey(this.state) === this._srvSynced) return;
clearTimeout(this._srvST);
this._srvST = setTimeout(() => {
const st = this.state;
const w2 = this.srvWs();
if (!w2 || w2.sid !== w.sid) return;
const k2 = this.srvCfgKey(st);
if (k2 === this._srvSynced) return;
const agents = {};
Object.keys(st.agentsCfg || {}).forEach((k) => { const c = st.agentsCfg[k]; agents[k] = { on: c.on, level: c.level, s: c.s, rules: c.rules || {}, perms: c.perms || {} }; });
this._srvSynced = k2;
this.srvApi('PATCH', '/workspaces/' + w.sid, { agents: agents, opt: st.opt, cap: st.cap, agents_paused: !!st.agentsPaused })
.catch((e) => { this._srvSynced = null; this.notify('Saving the agent settings failed: ' + e.message); });
}, 600);
}

srvPoll() {
const w = this.srvWs();
if (!w || this._srvPolling) return;
this._srvPolling = true;
Promise.all([this.srvApi('GET', '/workspaces/' + w.sid + '/live'), this.srvApi('GET', '/workspaces/' + w.sid + '/decisions?days=2')]).then((res) => {
this._srvPolling = false;
const cur = this.srvWs();
if (!cur || cur.sid !== w.sid) return;
const L = res[0];
const st = this.state;
const plan = L.plan;
const approved = !!plan && ['wave', 'sending', 'paused', 'done'].indexOf(plan.status) !== -1;
const tm = (iso) => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const patch = { srvLive: L, srvDecs: res[1].decisions || [], approved: approved, tick: L.tick, rt: approved ? 1 : 0, liveOn: !(plan && plan.status === 'paused') };
if (approved && plan.approvedAt) patch.approvedAt = tm(plan.approvedAt);
patch.events = (L.decisions || []).filter((d) => d.status !== 'info').slice(0, 7).map((d) => ({ id: 'd' + d.id, t: tm(d.at), text: d.agentName.replace(' agent', '') + ': ' + d.title, tone: d.status === 'pending' || d.status === 'suggested' ? 'warn' : 'ok', camp: true }));
if (this.srvCfgKey(st) === this._srvSynced && !!st.agentsPaused !== !!L.paused) {
patch.agentsPaused = !!L.paused;
this._srvSynced = this.srvCfgKey(Object.assign({}, st, patch));
}
const seen = this._srvSeen || (this._srvSeen = {});
const fresh = (L.notifications || []).filter((n) => !seen[n.id]);
fresh.forEach((n) => { seen[n.id] = true; });
const first = !this._srvSeenInit;
this._srvSeenInit = true;
this._rc = {};
this._segs = null;
this.setState(patch);
const show = first ? fresh.filter((n) => !n.read).slice(0, 4) : fresh;
show.slice().reverse().forEach((n) => this.notify(n.text));
}).catch((e) => {
this._srvPolling = false;
if (e && e.status === 401) this.setState({ authed: false });
});
}

srvLoadReport() {
const w = this.srvWs();
if (!w) return;
this.srvApi('GET', '/workspaces/' + w.sid + '/report?days=90').then((R) => {
const byDay = {};
(R.days || []).forEach((d) => { byDay[d.day] = d; });
this._rc = {};
this._ds = {};
this.setState({ srvReport: { byDay: byDay, lift: R.lift } });
}).catch(() => {});
}

srvLoadAudit() {
const w = this.srvWs();
if (!w) return;
this.srvApi('GET', '/workspaces/' + w.sid + '/audit').then((a) => {
this.setState({ agAudit: (a.audit || []).map((x) => ({ id: 'a' + x.id, t: new Date(x.at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }), who: x.who, text: x.text })) });
}).catch(() => {});
}

srvReloadContacts() {
const w = this.srvWs();
if (!w) return;
this.srvApi('GET', '/workspaces/' + w.sid + '/contacts').then((c) => {
const cur = this.srvWs();
if (cur && cur.sid === w.sid) { this._ck = null; this.setState({ contacts: c.contacts, cver: (this.state.cver || 0) + 1 }); }
}).catch(() => {});
}

srvPushContacts(list) {
const w = this.srvWs();
if (!w) return;
const rows = (list || []).map((c) => ({ e: c.e, f: c.f, l: c.l, co: c.co, fa: c.fa, la: c.la, n: c.n, sp: c.sp, sub: c.sub, tg: c.tg }));
this.srvApi('POST', '/workspaces/' + w.sid + '/contacts', { contacts: rows, source: 'file' })
.then(() => { this.srvReloadContacts(); this.srvLoadAudit(); })
.catch((e) => this.notify('Saving customers to the server failed: ' + e.message));
}

srvAudit(text) {
const w = this.srvWs();
if (w) this.srvApi('POST', '/workspaces/' + w.sid + '/audit', { text: text }).catch(() => {});
}

srvApprove() {
const st = this.state;
const w = this.srvWs();
if (st.approved || !this.can('run') || st.agentsPaused) return;
const skip = Object.keys(st.held || {}).filter((k) => st.held[k]);
this.srvApi('POST', '/workspaces/' + w.sid + '/plan/approve', { skip: skip }).then((r) => {
const at = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
this.setState({ approved: true, approvedAt: at, liveOn: true });
this.notify('Today’s plan was approved at ' + at + '. ' + this.f(((r.plan || {}).summary || {}).emails || 0) + ' emails are scheduled. The test wave goes first.');
this.srvPoll();
}).catch((e) => this.notify('The plan was not approved: ' + e.message));
}

srvVerdict(id, verdict) {
const w = this.srvWs();
if (!w) return;
this.srvApi('POST', '/workspaces/' + w.sid + '/decisions/' + id + '/' + verdict).then(() => {
this.notify({ approve: 'Approved.', decline: 'Declined.', undo: 'Undone.' }[verdict] || 'Saved.');
this.srvRefreshAll();
}).catch((e) => this.notify(e.message));
}

srvDecisions(key) {
const tm = (iso) => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
return (this.state.srvDecs || []).filter((d) => d.agent === key).map((d) => ({ id: 'srv-' + d.id, sid: d.id, srv: d, t: tm(d.at), action: d.status === 'info' ? 'info' : d.action, title: d.title, r: null, who: d.who, reason: d.reason, conf: d.conf, rule: d.rule }));
}

srvStatus(d) {
const s = d.srv.status;
const ok = 'background: var(--okBg); color: var(--okInk)';
const mute = 'background: var(--muteBg); color: var(--mut2)';
const warn = 'background: var(--warnBg); color: var(--warnInk)';
const spec = this.agentSpec()[d.srv.agent];
const a = spec ? spec.actions.find((x) => x[0] === d.srv.action) : null;
if (s === 'info') return { k: 'info', label: 'No action taken', css: mute };
if (s === 'pending') return { k: 'wait', label: 'Needs your approval', css: warn, a: a };
if (s === 'suggested') return { k: 'wait', label: 'Suggested', css: warn, a: a };
if (s === 'approved') return { k: d.srv.undoable ? 'done' : 'approved', label: 'Approved', css: ok, a: a };
if (s === 'declined') return { k: 'declined', label: 'Declined', css: mute };
if (s === 'undone') return { k: 'undone', label: 'Undone', css: mute };
if (s === 'blocked') return { k: 'off', label: 'Not allowed', css: mute };
return { k: d.srv.undoable ? 'done' : 'fixed', label: 'Done', css: ok, a: a };
}

srvLog() {
return (this.state.srvDecs || []).filter((d) => d.status !== 'info').slice(0, 12).map((d) => {
const undone = d.status === 'undone';
const can = d.undoable && !undone;
return { who: d.agentName.replace(' agent', ''), text: d.title + (d.status === 'pending' ? ' (needs your approval)' : d.status === 'suggested' ? ' (suggested)' : ''),
undone: !can, undoLabel: undone ? 'Undone' : can ? 'Undo' : 'Done', undoAria: 'Undo: ' + d.title,
textCss: undone ? 'text-decoration: line-through; color: var(--mut)' : '', undo: () => { if (can) this.srvVerdict(d.id, 'undo'); } };
});
}

srvLast(agent) {
const d = (this.state.srvDecs || []).find((x) => x.agent === agent && x.status !== 'info');
return d ? d.title + '.' : 'Nothing yet today.';
}

srvAuth(up, finishAuth) {
const st = this.state;
const body = { email: String(st.aemail).trim(), password: st.apass, name: String(st.aname || '').trim(), keep: !!st.akeep };
this.setState({ aerr: '', anote: up ? 'Creating your account…' : 'Signing in…' });
this.srvApi('POST', up ? '/auth/signup' : '/auth/login', body).then((r) => {
const u = r.user;
this.setState({ anote: '' });
finishAuth('');
this.later(60, () => {
this.setState({ profile: Object.assign({}, this.state.profile || {}, { name: u.name || u.email, email: u.email, role: u.role }), myRole: u.role, asRole: u.role });
this._srvBooting = false;
this.srvBoot();
});
}).catch((e) => this.setState({ aerr: e.message, anote: '' }));
}

srvCreateWs(name, industry, custom) {
const h = this.strHash(name);
const theme = ['violet', 'ocean', 'sunset', 'midnight'][h % 4];
let tz = 'UTC';
try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (e) {}
const settings = { timezone: tz };
if (custom && custom.url) settings.website = custom.url;
this.setState({ setup: this.freshSetup() });
this.srvApi('POST', '/workspaces', { name: name, industry: industry, theme: theme, playbook: custom || {}, settings: settings }).then((r) => {
const rec = this.srvRec(r.workspace);
this.setState({ workspaces: [rec].concat(this.state.workspaces || []) });
this.later(60, () => this.switchWs(rec.id));
this.later(1500, () => this.notify('The ' + name + ' workspace is ready. Add its customers next, or connect a store under Connections.'));
}).catch((e) => this.notify('Could not create the client: ' + e.message));
}

srvAdopt() {
const w = this.ws();
if (!w || w.sid) return;
const pbk = this.pbFor(w);
let copy = null;
try { copy = pbk ? JSON.parse(JSON.stringify(pbk)) : null; } catch (e) { copy = null; }
this.srvCreateWs(w.company, w.industry, copy);
}

srvPlayNote() {
const L = this.state.srvLive;
const p = L && L.plan;
if (!p) return 'Waiting for today\u2019s plan';
const wave = (p.summary || {}).wave || 0;
if (p.status === 'paused') return 'The send is paused. Resume it, or approve Guard\u2019s resume decision.';
if (p.status === 'wave') return L.rs.sent < wave ? 'Sending the test wave of ' + this.f(wave) : 'Test wave sent. Guard checks unsubscribes and bounces before the full send';
if (p.status === 'sending') return 'Test wave passed. Sending the rest by each customer\u2019s hour';
if (p.status === 'done') return L.rs.returned ? 'All emails sent, customers are returning' : 'All emails sent';
return 'Waiting for approval';
}

srvSegOpen() {
const L = this.state.srvLive;
const out = {};
const segs = (L && L.rs && L.rs.segs) || {};
Object.keys(segs).forEach((k) => { if (segs[k].sent && segs[k].opened) out[k] = segs[k].opened / segs[k].sent; });
return out;
}

srvToggleSend() {
const w = this.srvWs();
const p = this.state.srvLive && this.state.srvLive.plan;
if (!w || !p) return;
const act = p.status === 'paused' ? 'resume' : 'pause';
this.srvApi('POST', '/workspaces/' + w.sid + '/plan/' + act).then(() => {
this.notify(act === 'pause' ? 'Today\u2019s send was paused.' : 'Today\u2019s send was resumed.');
this.srvPoll();
}).catch((e) => this.notify(e.message));
}

srvSavePlaybook(recId, custom, name, industry) {
const rec = (this.state.workspaces || []).find((w) => w.id === recId);
if (rec && rec.sid) this.srvApi('PATCH', '/workspaces/' + rec.sid, { playbook: custom, name: name, industry: industry }).catch((e) => this.notify('Saving the playbook failed: ' + e.message));
}

srvDayStat(o) {
const R = this.state.srvReport;
const d = new Date();
d.setDate(d.getDate() - o);
const m = R && R.byDay ? R.byDay[this.isoOf(d)] : null;
return { con: m ? m.contacted : 0, ret: m ? m.returned : 0, buy: m ? m.returned : 0 };
}

srvLift() {
const L = (this.state.srvReport || {}).lift;
const holdout = (this.state.opt || {}).holdout;
if (!L || !L.contacted || L.without_rate == null) return { none: true, withRate: L ? Math.round(L.with_rate) : 0, without: 0, extra: 0, holdout: holdout };
return { withRate: Math.round(L.with_rate), without: Math.round(L.without_rate), extra: Math.max(0, L.extra_returns || 0), holdout: holdout };
}
// ---- end LOOPX server mode ----

