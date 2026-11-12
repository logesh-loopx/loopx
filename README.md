# LOOPX: email campaign console with five working AI agents

LOOPX sends **return reminders**: it works out when each customer usually comes back, emails them just before that date, and measures whether the email made a difference. Five agents do the daily work, and a person approves one plan a day.

The console is the LOOPX design (Claude Design export). It runs against a Python server where the agents are real.

| Agent | What it really does |
|---|---|
| **Timing** | Reads each customer's purchase history and works out their cycle, due date and certainty, using the same formula the console shows. Builds the day's plan, applies the certainty threshold, sends one reminder per cycle, and leaves out customers who come back without reminders. Learns each customer's send hour from their opens, respects weekends and bank holidays (moving reminders ahead of them), and keeps the holdout group out. |
| **Writer** | Fills the playbook emails with name, business and next date. Shortens long subjects, blocks emails with empty merge fields or words on the avoid list, and holds edited emails for review. Runs A/B(/C/D) subject tests and picks the winner after N opens. Each week it suggests a better subject for the weakest campaign (AI). |
| **Guard** | Never emails unsubscribed, bounced, complained or closed customers. Applies the weekly cap and the unopened-in-a-row stop. Sends a test wave first, checks unsubscribes and bounces, and pauses the send if a limit is passed, with a "Resume?" decision for a person. Enforces the daily sending quota. Every email carries one-click unsubscribe. |
| **Analyst** | Counts sends, opens, clicks, replies, returns, unsubscribes and bounces per day and per campaign. Compares emailed customers with the holdout group, so the report shows returns *caused* by email. Flags unusual days, writes the weekly report (AI, or plain figures without it), and suggests pausing weak campaigns. |
| **Listener** | Reads replies from your reply-to inbox (IMAP). The AI (or rules) sorts each one: unsubscribe, business closed, new address, complaint, out-of-office, bounce, question, positive. Unsubscribes are always honoured. Other actions follow the permission levels, and complaints are forwarded to your team. |

Every decision shows its reason, certainty and rule on the **Agents** screen. Each agent's permission level (off / suggest / ask / auto) is respected: *ask* items wait for the daily approval or a person, *suggest* items need a click, and *auto* items happen on their own. Most decisions can be undone. The settings you change in the console (levels, rules, permissions, holdout, weekly cap, pause all) are saved on the server and used on the next run.

**The AI only labels and writes; code decides the actions.** A reply cannot talk the Listener into anything outside its rules.

## AI: free providers

The agents work with any one of these. Paste a key in **Connections → AI** (admins only) and click **Test**:

| Provider | Cost | Notes |
|---|---|---|
| **Groq** | Free, no card | Fast. The first choice. Free plans are limited per minute and per day. |
| **Google Gemini** | Free AI Studio key | Good backup. Free requests per day are lower. |
| **OpenRouter** | Free `:free` models | About 50 requests a day without credits. |
| **Ollama** | Free, on your computer | No key and no limits; needs a reasonably strong PC. |
| **Claude** | Paid | Most capable. |

LOOPX uses them **in order**. It keeps each one under its per-minute and daily limits, and moves to the next when one runs out for the day. If none can answer, the agents fall back to their rules, so nothing stops. The Connections → AI tab shows each provider's use today.

What the AI does in each agent:

- **Timing:** reviews customers just below the certainty line (one call a day) and writes a short plan briefing.
- **Writer:** writes a personal subject and opening line for each email, from that customer's own history. Code then rejects any AI line that adds numbers, offers, links or words on the avoid list.
- **Guard:** reviews every new or edited campaign for misleading, pushy or spammy wording (a high risk holds it for a person) and spot-checks personalised emails.
- **Analyst:** writes a daily insight and the weekly report. With Tavily, it compares results with industry benchmarks once a month.
- **Listener:** sorts replies in any wording or language and drafts answers to questions. A person approves before an answer is sent.

**Tavily** (web search, free 1,000 credits a month) is optional. The Analyst uses it for benchmarks, and playbook drafting reads the client's website.

## Run it on your computer

**Option A: Docker** (Docker Desktop installed)
```bash
python scripts/init_env.py          # creates .env with fresh secrets (optional but recommended)
docker compose up --build
```

**Option B: plain Python 3.11+**
```bash
./run_local.sh          # macOS / Linux
run_local.bat           # Windows (double-click)
```

Open **http://localhost:8000** and click **Create account**. The first account becomes the Admin.

Locally you get **Demo Store (local)** with 400 sample customers and two years of purchases. Email goes to the **console** provider: nothing is sent, and every email is written to `data/outbox/outbox.jsonl`. Try this:

1. **Today's plan → Approve the plan.** The test wave goes first, Guard checks it, then the rest is sent in batches.
2. **Connections → Test a reply.** Paste "Please take me off this list", then open **Agents → Listener → Decisions**.
3. **Connections → Status → Run the agents now** runs all five immediately. Otherwise they run every 5 minutes.
4. **Agents**: change a level, a rule or a permission. It's saved on the server and applied on the next run.

The four sample clients (Avaniral, TaxZerone, SecurePayStubs, Payticks) stay as design demos. On any of them, click **Make this client real** to create a real copy on the server with the same playbook. You can also create a new client with the setup wizard.

## Getting customers in

- **CSV:** the Customers screen's importer, or **Connections → Import file**. Accepts a customer list (first/last order date, number of orders) or an order list (one row per order).
- **Events API:** `POST /api/v1/events` with the client's `X-API-Key` (shown under Connections → Events API). Send each purchase; `external_id` makes calls safe to repeat.
- **Shopify / Stripe / WooCommerce:** add the keys under **Connections → Store connectors**. Orders sync every hour.

A purchase within `ATTRIBUTION_DAYS` (default 7) after an email counts as a return. Holdout customers are tracked the same way, so you can compare.

## Host it for free

Free web hosts block SMTP ports (Render since Sept 2025; DigitalOcean and Railway too), so the free setup sends through **Brevo's HTTPS API**.

| Piece | Free service | What to do |
|---|---|---|
| Database | **Neon** Postgres (1 GB free) | Create a project and copy the connection string → `DATABASE_URL` |
| Sending | **Brevo** (300 emails/day free) | Verify your sender (domain or address), create an API key → `BREVO_API_KEY`, `MAIL_FROM` |
| Server | **Render** free web service | New → Blueprint → this repo (uses `render.yaml`). Fill in the variables. Set `PUBLIC_URL` to the Render URL. |
| Wake-up + agents | **GitHub Actions** (or cron-job.org) | Repo secrets `LOOPX_URL` and `CRON_SECRET`, plus repo variable `LOOPX_CRON_ENABLED=true`. `.github/workflows/agents-cron.yml` calls `/cron/tick` every 10 minutes. |
| Replies | Your reply-to mailbox | `IMAP_HOST/USER/PASSWORD` (Gmail: app password), or per client under Connections → Reply inbox |
| Bounces & spam | Brevo webhook | Brevo → Transactional → Settings → Webhooks → `https://YOUR-URL/webhooks/brevo?secret=CRON_SECRET` (hard bounce, spam, unsubscribed) |
| AI | **Groq** (free) with **Gemini** (free) as backup | Paste the keys in **Connections → AI**, or set `GROQ_API_KEY` / `GEMINI_API_KEY`. Claude is optional (paid). |
| Web research | **Tavily** (1,000 free credits a month) | `TAVILY_API_KEY`, or paste it in Connections → AI |

Free-tier limits: the Render free service sleeps when idle (the cron wakes it), and Brevo free allows 300 emails a day across all clients (`DAILY_SEND_LIMIT`; Guard holds the rest for the next day). Upgrade either one when you outgrow it. No code changes are needed.

## Configuration

All settings are environment variables; see **`.env.example`** for every one with an explanation. Per-client settings (sender name, reply-to, time zone, holiday country, IMAP, store keys) are under **Connections** in the console.

## Project layout

```
backend/app/
  agents/      timing.py writer.py guard.py analyst.py listener.py orchestrator.py spec.py
  services/    mailer (console/SMTP/Brevo) · inbox (IMAP) · connectors (Shopify/Stripe/Woo)
               ingest (CSV, events) · tracking · actions (approve/undo) · ai (Groq, Gemini, OpenRouter, Ollama, Claude) · research (Tavily)
  routes/      api.py (console API) · public.py (tracking, unsubscribe, Events API, cron, webhooks)
frontend/      index.html (built) · src/bridge.js · src/component_srv.js
design/        the original Claude Design export
scripts/       build_frontend.py (rebuilds the console from the export) · init_env.py
```

The console is built from `design/loopx-console-export.html` by `scripts/build_frontend.py`, which applies about 40 checked patches. If you export a new design version, replace the file and run the script; any patch that no longer matches stops the build with a message.

## Still from the design (not wired to the server yet)

These still use the design's demo behaviour on real clients, or are switched off there:

- **New campaign → Launch** (one-off broadcasts) is switched off on real clients. Reminders go out through Today's plan.
- **Website tracking snippet** (site visits) and **Rehearse the plan** are still the design's demo.
- The per-customer live activity timeline and the per-campaign split inside Reports use the design's estimates. Totals, rates, the holdout comparison and daily numbers are real.
- **Ask LOOPX** answers from what the console has loaded, and from the AI when a key is set.
- Settings → Team in the console is the design's version. The real team list and roles are under **Connections → Team**.

## Tests

```bash
pip install pytest && cd backend && python -m pytest -q tests
```

The tests cover sign-in, roles, Events API, CSV import, plan build, approval, the test wave, Guard pausing a bad wave, sending, open/click/unsubscribe tracking, return attribution, all Listener actions, approve/undo, the store connectors (mocked), cron and Brevo webhooks. CI runs them on every push, and it also checks that the console patches still apply.
