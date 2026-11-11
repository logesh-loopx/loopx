"""Build frontend/index.html from the Claude Design export.

The export (design/loopx-console-export.html) is a self-unpacking bundle: a gzip'd asset
manifest plus the page template as a JSON string. This script:
  1. reads the template,
  2. injects the server bridge (frontend/src/bridge.js) and the server-mode methods
     (frontend/src/component_srv.js),
  3. applies small, checked patches so real workspaces use the LOOPX server and its agents,
  4. writes the bundle back with the patched template.

Every patch must match exactly once, so a changed export fails loudly instead of half-working.
Run:  python scripts/build_frontend.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "design" / "loopx-console-export.html"
OUT = ROOT / "frontend" / "index.html"
BRIDGE = (ROOT / "frontend" / "src" / "bridge.js").read_text()
METHODS = (ROOT / "frontend" / "src" / "component_srv.js").read_text()

SRV = "this.srvWs()"

PATCHES: list[tuple[str, str, str]] = [
    # --- bridge + methods ---
    ("load the bridge before the console runtime",
     '<script src="94446bd6-b07e-490d-860b-193b530493ed"></script>',
     "<script>" + BRIDGE.replace("</", "<\\/") + "</script>\n" + '<script src="94446bd6-b07e-490d-860b-193b530493ed"></script>'),
    ("add server-mode methods to the console",
     "baseState() {\nreturn {",
     METHODS.replace("</", "<\\/") + "baseState() {\nreturn {"),
    # --- lifecycle ---
    ("boot from the server on load",
     "Promise.resolve(c.use('downloads')).then((d) => { this._dl = d && typeof d.save === 'function' ? d : null; }, () => {});\n}\n} catch (e) {}\n}",
     "Promise.resolve(c.use('downloads')).then((d) => { this._dl = d && typeof d.save === 'function' ? d : null; }, () => {});\n}\n} catch (e) {}\nif (this.srv() && this.state.authed) this.srvBoot();\n}"),
    ("sync agent settings to the server",
     "componentDidUpdate() {\nclearTimeout(this._pT);",
     "componentDidUpdate() {\nif (this.srv()) this.srvSyncCheck();\nclearTimeout(this._pT);"),
    ("poll the server instead of simulating sends",
     "liveStep() {\nconst st = this.state;\nif (!st || !st.authed || st.booting || st.wsBusy) return;",
     "liveStep() {\nconst st = this.state;\nif (!st || !st.authed || st.booting || st.wsBusy) return;\n"
     f"if ({SRV}) {{ this._srvN = (this._srvN || 0) + 1; if (this._srvN % 4 === 0) this.srvPoll(); if (this._srvN % 48 === 0) this.srvLoadReport(); return; }}"),
    ("real send numbers",
     "runStats(rt, t) {",
     f"runStats(rt, t) {{\nif ({SRV} && this.state.srvLive) {{ const L = this.state.srvLive; return {{ sent: L.rs.sent, opened: L.rs.opened, clicked: L.rs.clicked, visited: L.rs.clicked, returned: L.rs.returned, tick: L.tick, done: L.tick >= 4 }}; }}"),
    ("real plan size",
     "const returned = act.reduce((a, s) => a + Math.round(s.n * s.rate), 0);\nreturn { act: act, emails: emails, returned: returned };",
     "const returned = act.reduce((a, s) => a + Math.round(s.n * s.rate), 0);\n"
     f"const spl = {SRV} && this.state.srvLive && this.state.srvLive.plan;\n"
     "return { act: act, emails: spl ? ((spl.summary || {}).emails || 0) : emails, returned: returned };"),
    ("approval goes to the server",
     "approve() {\nconst st = this.state;",
     f"approve() {{\nif ({SRV}) {{ this.srvApprove(); return; }}\nconst st = this.state;"),
    ("agent decisions come from the server",
     "agentDecisions(key) {\nconst st = this.state;",
     f"agentDecisions(key) {{\nif ({SRV}) return this.srvDecisions(key);\nconst st = this.state;"),
    ("audit log is saved on the server",
     "this.setState({ agAudit: [{ id: Date.now() + Math.floor(Math.random() * 1000), t: t, who: (st.profile || {}).name || 'You', text: text }].concat(st.agAudit || []).slice(0, 60) });",
     "this.setState({ agAudit: [{ id: Date.now() + Math.floor(Math.random() * 1000), t: t, who: (st.profile || {}).name || 'You', text: text }].concat(st.agAudit || []).slice(0, 60) });\n"
     f"if ({SRV}) this.srvAudit(text);"),
    ("reports use real daily numbers",
     "dayStat(o) {\nconst wk0 = this.ck();",
     f"dayStat(o) {{\nif ({SRV}) return this.srvDayStat(o);\nconst wk0 = this.ck();"),
    ("holdout comparison is real",
     "liftInfo() {\nconst r = this.report(30, 1);",
     f"liftInfo() {{\nif ({SRV}) return this.srvLift();\nconst r = this.report(30, 1);"),
    ("one-off campaigns are not faked",
     "launchCampaign() {\nconst st = this.state;",
     f"launchCampaign() {{\nif ({SRV}) {{ this.notify('One-off campaigns are not sent from this screen yet. Reminders go out through Today\\u2019s plan, run by the five agents.'); return; }}\nconst st = this.state;"),
    ("server data is never wiped by reset",
     "resetWs() {\nconst rec = this.ws();",
     f"resetWs() {{\nif ({SRV}) {{ this.notify('Clients on the server keep their data. Remove customers or the client on the server instead.'); return; }}\nconst rec = this.ws();"),
    # --- workspaces ---
    ("load a server workspace after switching",
     "this._assets = this._assetsBy[id] || {};\nthis._caret = undefined;\nthis.setState(next);",
     "this._assets = this._assetsBy[id] || {};\nthis._caret = undefined;\nthis.setState(next);\nif (rec.sid) this.srvLoadWs(rec);"),
    ("new clients are created on the server (setup wizard)",
     "const h = this.strHash(name);\nconst rec = { id: 'w' + Date.now(), company: name, industry: S.industry, pb: 'custom',",
     "if (this.srv()) { this.srvCreateWs(name, S.industry, custom); return; }\nconst h = this.strHash(name);\nconst rec = { id: 'w' + Date.now(), company: name, industry: S.industry, pb: 'custom',"),
    ("playbook edits are saved on the server",
     "this.later(60, () => this.notify('The ' + name + ' playbook was updated and approved by ' + st.profile.name + '.'));\nreturn;",
     "this.later(60, () => this.notify('The ' + name + ' playbook was updated and approved by ' + st.profile.name + '.'));\nif (this.srv()) this.srvSavePlaybook(S.editId, custom, name, S.industry);\nreturn;"),
    ("quick-add clients are created on the server",
     "let h = 7;\nfor (let i = 0; i < name.length; i++) h = (Math.imul(h, 31) + name.charCodeAt(i)) >>> 0;",
     "if (this.srv()) { this.srvCreateWs(name, st.wsDraft.industry || 'store', null); this.setState({ wsDraft: { name: '', industry: 'store' }, wsErr: '' }); return; }\n"
     "let h = 7;\nfor (let i = 0; i < name.length; i++) h = (Math.imul(h, 31) + name.charCodeAt(i)) >>> 0;"),
    # --- customers ---
    ("imports are saved on the server",
     "this.later(60, () => this.notify(this.f(b.rec.total) + ' customers saved to ' + this.terms().company + ' from ' + b.rec.name + '.'));",
     "this.later(60, () => this.notify(this.f(b.rec.total) + ' customers saved to ' + this.terms().company + ' from ' + b.rec.name + '.'));\n"
     f"if ({SRV}) this.srvPushContacts(b.contacts);"),
    ("customers added by hand are saved on the server",
     "this.setState({ contacts: b.contacts, cimports: recs, cver: (st.cver || 0) + 1, impFlow: Object.assign({}, fl, { one: this.freshFlow('one').one, err: '', okMsg: msg, okCid: c.id }) });",
     "this.setState({ contacts: b.contacts, cimports: recs, cver: (st.cver || 0) + 1, impFlow: Object.assign({}, fl, { one: this.freshFlow('one').one, err: '', okMsg: msg, okCid: c.id }) });\n"
     f"if ({SRV}) this.srvPushContacts(b.contacts.filter((x) => x.e === e));"),
    # --- agents screen ---
    ("decision status comes from the server",
     "const decRows = dec.map((d) => {\nconst s = statusOf(d);",
     "const decRows = dec.map((d) => {\nconst s = d.srv ? this.srvStatus(d) : statusOf(d);"),
    ("approve / decline / undo go to the server",
     "const setDec = (id, v, text) => {",
     "const setDec = (id, v, text) => {\nif (String(id).indexOf('srv-') === 0) { this.srvVerdict(Number(String(id).slice(4)), { undone: 'undo', approved: 'approve', declined: 'decline' }[v]); return; }"),
    ("show who a server decision is about",
     "hasWho: !!d.r, reason: d.reason,",
     "hasWho: !!d.r || !!(d.srv && d.who), reason: d.reason,"),
    ("agents activity log is real",
     "log: [['g1', 'Guard', 'Removed '",
     f"log: {SRV} ? this.srvLog() : [['g1', 'Guard', 'Removed '"),
    ("Guard card shows the real test-wave result",
     "st.tick >= 2 ? 'Test wave passed: unsubscribes 0.1%, bounces 0.3%.'",
     f"st.tick >= 2 ? ({SRV} ? this.srvLast('guard') : 'Test wave passed: unsubscribes 0.1%, bounces 0.3%.')"),
    ("Listener card counts real replies",
     "Math.floor(rs.opened / 2400) + ' replies read so far today.'",
     f"({SRV} && this.state.srvLive ? this.f(this.state.srvLive.rs.replied) : Math.floor(rs.opened / 2400)) + ' replies read so far today.'"),
    ("campaign counts follow the server's plan",
     "list.forEach((s) => { s.imp = B[s.id].length; s.n = s.syn + s.imp; });",
     "list.forEach((s) => { s.imp = B[s.id].length; s.n = s.syn + s.imp; });\n"
     f"const spx = {SRV} && this.state.srvLive && this.state.srvLive.plan;\n"
     "if (spx && spx.summary && spx.summary.segs) list.forEach((s) => { if (s.id !== 'alone') { const q = spx.summary.segs[s.id]; s.n = Math.min(q ? q.send : 0, B[s.id].length); } });"),
    ("plan screen: real test wave size",
     "sub: canRun ? 'We send a test wave of 2,000 first. This is the only approval needed today.'",
     f"sub: canRun ? 'We send a test wave of ' + ({SRV} && st.srvLive && st.srvLive.plan ? f(((st.srvLive.plan.summary || {{}}).wave) || 0) : '2,000') + ' first. This is the only approval needed today.'"),
    ("plan screen: real progress wording",
     "playNote: !st.liveOn ? 'The send is paused' :",
     f"playNote: {SRV} ? this.srvPlayNote() : !st.liveOn ? 'The send is paused' :"),
    ("plan screen: no simulation note",
     "Simulated: the next two days play out in a few seconds. Approved at {{ run.at }}.",
     "Approved at {{ run.at }}. The agents send in small batches every few minutes and Guard checks the test wave first."),
    ("pause / resume the real send",
     "toggleLive: () => { this.setState({ liveOn: !st.liveOn });",
     f"toggleLive: () => {{ if ({SRV}) {{ this.srvToggleSend(); return; }} this.setState({{ liveOn: !st.liveOn }});"),
    ("dashboard: real open rate per campaign",
     "const segOpen = { bi: 0.64, wk: 0.66, mo: 0.6, re: 0.41, mp: 0.47, cb: 0.45, ra: 0.29 };",
     f"const segOpen = {SRV} ? this.srvSegOpen() : {{ bi: 0.64, wk: 0.66, mo: 0.6, re: 0.41, mp: 0.47, cb: 0.45, ra: 0.29 }};"),
    ("dashboard: no made-up rate when a campaign has no data",
     "rate: frac > 0 && rs.opened > 0 ? Math.round((segOpen[dc0.id] || 0.5) * 100) + '%' : 'None yet'",
     "rate: frac > 0 && rs.opened > 0 && segOpen[dc0.id] != null ? Math.round(segOpen[dc0.id] * 100) + '%' : 'None yet'"),
    # --- static text that only made sense in the design preview ---
    ("login footer",
     "Sample data only. Nothing is sent from this preview.",
     "Your clients, their customers and every agent decision live on your LOOPX server."),
    ("login help",
     "Preview sign-in: any work email, a password of 8 or more characters, and any 6-digit code.",
     "The first account created on this server becomes the admin. Others join as viewers until an admin changes their role."),
    ("sidebar footer",
     '<div style="font-size: 12px; color: var(--mut); padding: 0 6px">Sample data only.</div>',
     '<div style="font-size: 12px; color: var(--mut); padding: 0 6px">Sample clients show demo data.</div>'),
    # --- sign-in ---
    ("sign in and sign up against the server",
     "if (st.amode === 'up') { finishAuth(''); return; }\nthis.setState({ astep: 'code', acode: '', aerr: '', anote: '' });",
     "if (this.srv()) { this.srvAuth(st.amode === 'up', finishAuth); return; }\n"
     "if (st.amode === 'up') { finishAuth(''); return; }\nthis.setState({ astep: 'code', acode: '', aerr: '', anote: '' });"),
    ("password reset needs an admin on the server",
     "const sendReset = () => {\nif (!authOk(st.aemail)) { this.setState({ aerr: 'Enter a valid work email address.', anote: '' }); return; }",
     "const sendReset = () => {\nif (this.srv()) { this.setState({ aerr: '', anote: 'Ask an admin to set a new password for you under Connections, Team.' }); return; }\n"
     "if (!authOk(st.aemail)) { this.setState({ aerr: 'Enter a valid work email address.', anote: '' }); return; }"),
    ("single sign-on is not set up on the server",
     "ssoGoogle: () => finishAuth('Google'), ssoMicrosoft: () => finishAuth('Microsoft'), ssoCompany: () => finishAuth('company SSO'),",
     "ssoGoogle: () => (this.srv() ? this.setState({ aerr: 'Single sign-on is not set up on this server. Use email and password.' }) : finishAuth('Google')), "
     "ssoMicrosoft: () => (this.srv() ? this.setState({ aerr: 'Single sign-on is not set up on this server. Use email and password.' }) : finishAuth('Microsoft')), "
     "ssoCompany: () => (this.srv() ? this.setState({ aerr: 'Single sign-on is not set up on this server. Use email and password.' }) : finishAuth('company SSO')),"),
    ("sign out on the server too",
     "signOut: () => { this.stopVoice();",
     "signOut: () => { if (this.srv()) { window.LX.api('POST', '/auth/logout').catch(() => {}); if (window.LX.showButton) window.LX.showButton(false); } this.stopVoice();"),
]


def main() -> int:
    lines = SRC.read_text(encoding="utf-8").split("\n")
    idx = next(i for i, l in enumerate(lines) if l.startswith('"<!DOCTYPE html>'))
    tpl = json.loads(lines[idx])
    for name, old, new in PATCHES:
        n = tpl.count(old)
        if n != 1:
            print(f"PATCH FAILED ({n} matches): {name}\n  looking for: {old[:140]!r}", file=sys.stderr)
            return 1
        tpl = tpl.replace(old, new)
    enc = json.dumps(tpl, ensure_ascii=False).replace("</", "<\\u002F")
    assert json.loads(enc) == tpl
    lines[idx] = enc
    out = "\n".join(lines)
    out = out.replace("<title>LOOPX - Email Campaign Console</title>", "<title>LOOPX</title>")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(out, encoding="utf-8")
    print(f"Built {OUT.relative_to(ROOT)} with {len(PATCHES)} patches ({len(out) // 1024} KB).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
