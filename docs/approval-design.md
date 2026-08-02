# Sprint B — Approval-based task runner (DESIGN, not implementation)

> **Status**: Design only. User reviews this before any code lands.
> Built on top of v3.4 (dashboard user auth). The two are
> intentionally separate sprints so the login flow is shippable
> and stable before we add the approval surface.

---

## 1. Motivation

Today, every workflow step in the orchestrator runs without human
intervention. For some tasks — sending an email to a customer,
publishing a report to an internal portal, executing a trade — the
*agent* decides what to send/publish/execute and just does it.
That's the right default for back-office automation, but wrong for
anything where the *human* needs to sign off before the next step
runs.

This sprint adds a new step type, `approval`, that pauses a workflow
until a human (anyone with the link, or a logged-in dashboard user)
clicks Approve or Reject. The task stops at the approval step,
shows a card on the project page, and emails a magic link to the
human specified in the step config. When the human acts, the task
resumes and runs the next step.

**Concrete use case (from user spec, 2026-07-31):**
> Agent runs a tradingview backtest, builds a signal report, then
> wants to send it to the user for approval before posting to a
> Telegram channel. Today: agent either asks via chat (manual) or
> just sends. With approval: agent stops at the approval step,
> emails the user a link, user clicks Approve, then agent posts.

**Non-goals (deferred):**
- Multi-step approval chains (A approves → B reviews → C executes).
  One approval per step is enough for the first cut.
- Per-approver allowlists (e.g. "only alice@company.com can
  approve"). Configurable globally via the workflow step's
  `approver_emails` field, but we don't enforce which specific
  approver clicks the link.
- Approval timeout escalation. Default 72h then auto-fail; no
  email reminder (orchestrator doesn't know how to email without
  going through an agent anyway).

---

## 2. Data model

New table `approvals` (additive migration, no rewrite needed):

| col                  | type           | note |
|----------------------|----------------|------|
| id                   | TEXT PK        | `apr-{short_id}` |
| task_id              | TEXT FK NOT NULL | references tasks(id) ON DELETE CASCADE |
| step_id              | TEXT NOT NULL  | which step in the workflow |
| token                | TEXT UNIQUE NOT NULL | URL-safe 32-byte random, base64 |
| prompt               | TEXT NOT NULL  | what the user sees (the approval question) |
| context_json         | TEXT           | snapshot of step inputs / outputs so the user has context to decide |
| status               | TEXT NOT NULL DEFAULT 'pending' | `pending` / `approved` / `rejected` / `expired` |
| requested_at         | INTEGER NOT NULL | epoch seconds |
| expires_at           | INTEGER        | optional; null = no expiry |
| responded_at         | INTEGER        | null until acted on |
| responder_user_id    | TEXT           | nullable; set by the **dashboard** path |
| responder_label      | TEXT           | nullable; set by the **magic-link** path (e.g. email or "self") |
| response_note        | TEXT           | optional free-text from the human |

Indexes: `idx_approvals_task_id`, `idx_approvals_token UNIQUE`,
`idx_approvals_status` (for "show me all pending approvals" queries).

The `responder_user_id` / `responder_label` split lets us audit
*who* approved:
- Dashboard user → `responder_user_id = usr-...` (we know the
  username; can show it in the task history)
- Magic link → `responder_label = "alice@company.com"` (the email
  address from the step's `approver_emails` list; we don't know
  *which* person at that address clicked the link, just that the
  email was on the allowlist)

This is "good enough" for audit. The agent can also write a
summary of the approval back into the step's output so the next
step sees `{"approved": true, "by": "alice@company.com"}` etc.

---

## 3. Workflow step type

New step `type: "approval"` in the workflow JSON. The orchestrator's
state machine adds a new task status `awaiting_approval` between
`running` and the next `running`:

```json
{
  "id": "step-approve",
  "type": "approval",
  "config": {
    "prompt": "Send the report to legal@company.com?",
    "approver_emails": ["alice@company.com", "bob@company.com"],
    "expires_in_hours": 72
  }
}
```

Config schema (validated at workflow save time):
| field             | type     | required | default | notes |
|-------------------|----------|----------|---------|-------|
| `prompt`          | string   | yes      | —       | max 500 chars; the human's question |
| `approver_emails` | string[] | no       | `[]`    | informational; not enforced for magic-link clicks (see §6 security) |
| `expires_in_hours`| int      | no       | 72      | `0` or null = no expiry. After expiry, status → `expired` and task → `failed` |

Existing step types (`task`, `decision`, etc.) are unchanged.

**Task lifecycle:**

```
running  --[reaches approval step]-->  awaiting_approval
  |
  +-- [human approves] -->  running  (next step)
  +-- [human rejects]  -->  failed   (terminal)
  +-- [expires]        -->  failed   (terminal; status='expired')
```

The transition `running → awaiting_approval` is triggered the same
way as `running → failed`: the supervisor's tick loop sees the new
task status, persists `awaiting_approval`, creates the `approvals`
row, and emits a `task.awaiting_approval` audit event with the magic
link URL.

---

## 4. Two paths to approve

| Path | Auth | When | Audit |
|------|------|------|-------|
| **Magic link** in email | signed token, single-use, expires | External user (email). The agent emails the URL. | `responder_label` = email from `approver_emails` (or "anonymous" if not on list) |
| **Dashboard widget** | user session cookie (v3.4) | Internal user (logged into dashboard). | `responder_user_id` = the user row |

### 4.1 Magic link

URL: `https://orch.local/approval/<token>`

- Token = `secrets.token_urlsafe(32)` (URL-safe base64, 43 chars)
- URL is shown on the project page's "Awaiting approval" card and
  in the task's prompt to the agent (the agent emails it)
- The `/approval/<token>` page is a STANDALONE HTML page (no login
  required). Renders:
  - The approval prompt
  - A short context summary (from `approvals.context_json`)
  - "Approve" and "Reject" buttons
  - Optional "Add a note" text field
- Click Approve → POST `/api/approvals/by-token/{token}/respond` with
  `{decision: "approve" | "reject", note: "..."}`. Server marks the
  approval, transitions the task, returns a 200 page that says
  "Thanks, you approved. The workflow will resume."
- Token is **single-use**: a second click on the same link returns
  410 Gone with a clear "This approval has already been responded
  to" page (so email forwards / accidental double-clicks don't
  re-fire the action).

### 4.2 Dashboard widget

On the project page, when a task is `awaiting_approval`, render a
card per pending approval:

```
+----------------------------------------------------+
| Awaiting approval                                   |
|                                                     |
| step-approve: "Send the report to legal@company.com?" |
| Step output: ... (truncated context) ...             |
| [Approve]  [Reject]                                 |
+----------------------------------------------------+
```

Both buttons POST to `/api/approvals/{id}/respond` (cookie auth via
the existing v3.4 middleware). The responder is recorded as
`responder_user_id` = the logged-in user.

---

## 5. API surface

### New endpoints (auth: cookie OR magic-link-token in URL)

```
GET  /approval/{token}                          -- HTML page, public
POST /api/approvals/by-token/{token}/respond    -- public (token in path)
GET  /api/approvals/                            -- list pending (cookie auth)
GET  /api/approvals/{id}                        -- one approval (cookie auth)
POST /api/approvals/{id}/respond                -- dashboard path (cookie auth)
GET  /api/tasks/{id}/approval                   -- helper for the project page widget
```

### Pydantic models

```python
class ApprovalRespondIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=1000)
```

### Response shape (GET /api/approvals/)

```json
[
  {
    "id": "apr-a1b2c3",
    "task_id": "t-9f8e7d",
    "step_id": "step-approve",
    "prompt": "Send the report to legal@company.com?",
    "context": {"summary": "...", "url": "..."},
    "status": "pending",
    "requested_at": 1785486342,
    "expires_at": 1785745542,
    "approver_emails": ["alice@company.com"]
  }
]
```

The `context` field is what the dashboard widget shows. For
single-line summaries we cap at 200 chars; multi-line JSON is fine
for structured step outputs (e.g. a backtest metrics blob).

### State transitions (server-side)

```
POST /api/approvals/by-token/{token}/respond
  1. Look up approval by token. 404 if not found.
  2. If status != 'pending': 410 Gone (already responded / expired).
  3. If expires_at < now: mark 'expired', transition task to 'failed',
     return 410.
  4. Validate decision in body. 422 if invalid.
  5. Set status = 'approved' | 'rejected'. Set responded_at = now.
     Set responder_label = email-from-list (if any) | 'anonymous'.
  6. If approved: task.status = 'running', persist
     approval response into the next step's input
     (the supervisor picks it up on next tick).
  7. If rejected: task.status = 'failed', terminal.
  8. Audit log: `approval.responded` with task_id, decision, note.
  9. Return 200 with a small JSON (used by the standalone approval
     page to render the "Thanks!" state).
```

---

## 6. UI changes

### 6.1 Project page — new "Awaiting approval" section

Rendered only when at least one approval is `pending` for any task
on this project. Card per approval:

- Project_id, step_id, prompt (truncated to 80 chars with
  "..." if longer)
- A button linking to `/approval/{token}` (standalone page, full
  context) — labelled "Review in full →"
- Inline Approve / Reject buttons for the dashboard path

The card sits at the top of the page (right after the project
header), so users see approvals before the workflow canvas / task
list.

### 6.2 Workflow editor — new step type

Add a "Human approval" entry to the step-type picker in the visual
builder. Drop a step → config form has:
- Prompt (textarea, 500 chars max)
- Approver emails (comma-separated input, optional)
- Expiry hours (number input, default 72)

Render the step in the canvas with a distinct visual: yellow border
+ ⏸ pause icon, so it stands out from regular task steps.

### 6.3 Standalone approval page

`/approval/{token}`:
- No sidebar, no nav (same minimal layout as /login)
- Title: the prompt
- Body: the context (rendered as plain text or a small `<pre>` for
  JSON)
- Two big buttons: "Approve" and "Reject"
- Optional "Add a note" textarea
- On submit: replace the form with a "Thanks!" message showing the
  decision and timestamp
- After 1.5s, JS redirects to the orchestrator's project page
  (or shows "Done — you can close this tab")

---

## 7. Agent usage

The agent doesn't need any new tools. The approval step is just a
"task" with extra context. The orchestrator's prompt to the agent
will include the approval's `magic_link_url` after the approval is
created:

```
The current step is "approval" — you must email the human at
<approver_emails> with this URL and STOP. Do not proceed to the
next step. The human will click the URL, see the approval page, and
Approve or Reject. When they Approve, the next step will run
automatically.

Magic link URL: https://orch.local/approval/abc123def456...

Your job: compose the email (use `shell` to call sendmail, or
`web_search` for SMTP credentials, whatever you have). Once
the email is sent, return success. The workflow pauses here.
```

The agent then uses its existing `shell` or `web_extract` tool to
send the email. The orchestrator doesn't know or care how the email
is sent — it just hands the URL to the agent and waits.

---

## 8. Security

| Concern | Mitigation |
|---------|-----------|
| Anyone with the link can approve | Intentional — the email is the auth. If we want stricter control later, add an `approver_tokens` per-email table and only sign tokens to those addresses. Out of scope for v1. |
| Token re-use (email forward / double-click) | Token is single-use; second click returns 410. |
| Token interception in email | Token is signed (itsdangerous) and 32 bytes of random entropy. If TLS is on (we deploy internal LAN; TLS deferred to productize), this is reasonable. |
| Approval page exposes sensitive context | The `context_json` is whatever the step's output is. If the step's output contains secrets, the step's `secrets_redact` config (future) should strip them. For v1, we trust the step author. |
| Expired approval still reachable via magic link | Server checks `expires_at` and returns 410 if expired. |
| User disables themselves, but their cookie is still valid | Cookie has 7-day max-age. Disabling takes effect on next request because `current_user_id` re-checks the DB. Same model as v3.4. |
| CSRF on the dashboard Approve / Reject buttons | SameSite=Lax cookie + `X-Requested-With: XMLHttpRequest` header check (deferred to a follow-up; for v1 we rely on SameSite=Lax only) |

---

## 9. Out of scope (deliberately)

- **Per-approver allowlist enforcement**: the `approver_emails`
  field is informational only on the magic-link path. We can
  enforce it in a v2 by storing per-email signed tokens.
- **Multi-step approval chains**: one approval = one step. The
  workflow author can chain `approval` steps if they need
  sequential sign-offs.
- **Approval delegation** (e.g. "alice can approve on behalf of
  bob if bob is OOO"): not in scope.
- **Notification / reminders**: orchestrator doesn't email.
  Future: optional webhook URL per approval step that fires on
  creation and expiry-imminent.
- **Approval analytics / dashboards**: just show in
  /api/approvals/. No per-approver latency stats yet.
- **TLS / HTTPS**: deferred to productize. Internal LAN with
  HMAC agent auth is the existing security model; user cookies
  are also internal-only.

---

## 10. Open questions for sign-off

1. **Expiry default 72h** — agree? Or 24h / 7d?
2. **Magic link label** — when an email isn't on the
   `approver_emails` list, what do we record as `responder_label`?
   My proposal: `"anonymous"`. Or do we want to require the
   human to type their email in the form?
3. **Auto-redirect after approval** — show "Thanks!" page for 1.5s
   then redirect to project page? Or just "Thanks, you can close
   this tab"? My proposal: redirect (less work for the human).
4. **What context to show** — just the step's `output`, or also
   include the task's full history? My proposal: step output only,
   plus the prompt and the response note. Task history is
   already in the dashboard.
5. **Standalone approval page vs dashboard** — confirm the
   standalone /approval/{token} page is acceptable for the
   "internal user gets emailed too" case. Alternative: redirect
   to /login if the user has a session and a magic link otherwise.
   My proposal: just do the standalone page (simpler, no auth
   dance, works the same in all cases).

If you sign off on this, I'll implement as v3.5 (estimated ~10
commits over a few iterations: schema + API, magic link path,
dashboard path, project page section, workflow editor, agent
prompt template, tests, docs).
