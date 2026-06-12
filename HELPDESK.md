# DigiRocket Helpdesk — System Documentation

Internal helpdesk / ticketing + team chat built on top of the existing
Reporting App (Flask + Supabase). This document records everything that was
built so the whole system is saved in one place.

> Code lives in the normal app files (`routes.py`, `db.py`, `roles.py`,
> `auth.py`, `templates/`). It is deployed on Render (`reporting-app`,
> Blueprint-managed via `render.yaml`) and uses the shared Supabase project.

---

## 1. Database setup — run these SQL files ONCE in Supabase → SQL Editor

Run in this order (each is idempotent — safe to re-run):

| # | File | Creates |
|---|------|---------|
| 1 | `supabase_schema.sql` | clients, report_logs, activities, report_queries, query_messages, service_credentials (pre-existing) |
| 2 | `tickets_schema.sql` | `staff`, `tickets`, `ticket_messages`, `ticket_events` (+ seeds the 2 founders) |
| 3 | `ticket_assignees_schema.sql` | `ticket_assignees` (multi-assign) + `ticket_messages.mentions` |
| 4 | `staff_chat_schema.sql` | `staff_messages` (1:1 chat) + `reply_to_id` |
| 5 | `staff_groups_schema.sql` | `staff_groups`, `staff_group_members` (+ `pinned`), `staff_group_messages` |
| 6 | `avatars_schema.sql` | `staff.avatar_url` + `staff_groups.avatar_url` (profile/group photos) |

Staff are seeded/managed via `seed_staff.py` (re-runnable) **or** the `/staff` page.

---

## 2. Roles (roles.py)

Internal key → display label:

| Key | Label | Who | Access |
|-----|-------|-----|--------|
| `admin` | Admin | `analytics@digirocketads.com` (agency) | Everything |
| `supervisor` | **Founder** | Shubhranshu, Sunny | See all, read-only (monitor) |
| `triage` | **PMO** | Nikhar, Sidharth, Shweta | Review/approve + assign + manage staff + see all |
| `dispatcher` | **Assigner** | Jasleen, Apoorv | Assign approved tickets to employees |
| `employee` | Employee | team members | Only their own assigned tickets; resolve |

Teams: SEO, Content, Performance Marketing, Graphic Design, Developer, Sales, Physio.

**PMO emails** (`nikhar.makkar`, `sidharth.anant`, `shweta.singh`) are also in
`auth.DEFAULT_PMO_ADMINS` → full PMO portal + dashboard access.

---

## 3. Login

Everyone signs in at `/login`:
- **Email OTP** (works for Outlook/Microsoft 365 **and** Google) — enter email →
  6-digit code by email → sign in. Default path (company runs on Outlook).
- **Sign in with Google** at `/login/google`.

Only registered staff or clients can receive a code. OTP sessions set
`auth_provider='otp'` + a sentinel token. The **agency GA4/GSC token** is pinned
to `analytics@digirocketads.com` (`auth.is_agency_account`) so other logins don't
clobber it; that account must still use Google login to supply the agency token.

---

## 4. Ticket workflow (Amazon/Flipkart-style roadmap)

`Raised → Reviewed → Assigned → In progress → Resolved → Closed`

1. **Client** raises a ticket from `/portal/tickets` (rich-text editor, attachments),
   or PMO converts a client chat to a ticket (`/pmo/queries` → "Convert to ticket",
   copies the full transcript).
2. **PMO (triage)** reviews → Approve (route to a team) or Reject.
3. **Assigner (dispatch)** assigns to one OR MORE employees (multi-assign checkboxes).
4. **Employee** → Start work → Resolve (posts solution `Re: #N`, attach proof).
5. **PMO/assignee** closes; founders/PMO track everything.

- **@mention** in a ticket reply tags someone → they become an assignee + notified.
- Each ticket has a **History** trail (who advanced each stage, when).
- Ticket detail has **हिंदी translate** + **🔊 Listen (voice)** for the description.
- Only PMO/admin (and clients on the portal) can create tickets — employees cannot.

Key routes: `/tickets` (board), `/tickets/<id>` (detail), `/tickets/<id>/review|assign|start|resolve|close|message`, `/staff` (staff admin), `/api/translate`.

---

## 5. Employee Query — team chat

`/staff/messages`

- **1:1 chat** between any staff: read receipts (blue tick), reply-to a message,
  attachments + inline images, search people, unread-first.
- **Groups** (WhatsApp/Teams-style): create (`+ New`), per-group **3-dot menu**
  → Rename / Pin (per-user, floats to top) / Delete (creator or admin only).
- **Founders + PMO + admins auto-join every group** and can view all groups.
- **Group info panel** (click the group header): member list with photos, role,
  "Group admin" badge, Add members, change group photo.
- **Profile photos (DP)**: each user sets their own (topbar → My profile);
  group photos too. Shown everywhere (an `avatar()` macro renders photo-or-initial).

Routes: `/staff/messages`, `/staff/messages/send`, `/staff/groups/new`,
`/staff/groups/<id>/message|add|rename|pin|delete|photo`, `/staff/profile/photo`,
`/api/staff/unread`.

---

## 6. Performance & deploy notes

- **`roles.current_staff()` is cached per-request** on `flask.g` — permission
  helpers funnel through it and templates call them 10-20×/page; this turned
  10-20 Supabase calls per page into 1. Biggest page-load speedup.
- `ticket_detail` fetches its independent reads concurrently (ThreadPoolExecutor).
- **Render start command** (in `render.yaml`, Blueprint-managed):
  `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 1 --worker-class gthread --threads 4 --max-requests 150 --max-requests-jitter 30`
  The `gthread` worker keeps answering health checks while a thread generates a
  PDF (a plain sync worker blocks → Render restarts → 502); `--max-requests`
  recycles the worker to avoid OOM on the 512 MB free tier.
- Local dev: `venv\Scripts\Activate.ps1` then `python main.py` → http://127.0.0.1:5000
  (use the `venv` folder, not `.venv`). It's a Python/Flask app — `npm` does not apply.

---

## 7. Attachments / storage

All uploads (chat files, images, avatars, ticket proof) go to the Supabase
Storage bucket **`query-attachments`** (auto-created, public) under folders like
`avatars/`, `staff-chat/`, `<ticket_id>/`. Folder names must be storage-safe
(no `@`), which is why staff-chat uploads use a fixed `staff-chat` folder.
