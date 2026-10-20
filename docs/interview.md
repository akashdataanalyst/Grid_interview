# Grid Interview

A recruitment interview platform for Frappe / ERPNext v16 on top of Frappe HR (HRMS): HR schedules
interviews, candidates use their own portal, everyone meets in an embedded video room, and
recordings become transcripts and AI draft reports that a human reviews. HRMS stays the record of
the interview; Grid Interview adds everything around it.

`grid_interview` is a separate app (not a module of `grid_erp`). It does not change Frappe,
ERPNext, HRMS or `grid_erp`, and adds no Custom Fields, Property Setters or `doc_events` to
standard DocTypes. HRMS is optional: without it the app installs, but scheduling and the portal's
interview features are not available.

## Code layout

```
grid_interview/                    App package
├── hooks.py                       apps-screen tile, permissions, scheduler jobs, portal menu
├── permissions.py                 row-level rules (interviewers see only their interviews)
├── api/                           thin whitelisted endpoints, no business rules
│   ├── scheduling.py              schedule / reschedule / cancel / attach, interviewer search
│   ├── dashboard.py               dashboard counts and lists, timeline, decision, recording delete
│   ├── hr.py                      publish results, reference verification / clarification
│   ├── portal.py                  candidate portal: register, profile, documents, references
│   ├── video.py                   room: info, join, heartbeat, leave, end, join links
│   └── recording.py               consent, recording start / stop, Jibri ingest, AI review, retry
├── services/                      all authorization and business rules
│   ├── scheduling.py              HRMS Interview as the user, Video Session, 5-minute run_cycle
│   ├── notifications.py           invitation / reminder / reschedule / cancel emails, idempotent
│   ├── portal.py, candidates.py   what a candidate sees; candidate accounts, CSRF
│   ├── portal_emails.py           result-published and reference emails
│   ├── references.py, results.py  referees; publish / unpublish results
│   ├── meeting.py                 join window, room lifecycle, passwordless join links
│   ├── consent.py, recording.py   consent per person and purpose; recording control and ingest
│   ├── storage.py                 Local Private (now) / S3 (later) behind one interface
│   ├── transcription.py           ffmpeg + OpenAI-compatible transcription (or Mock)
│   ├── analysis.py                AI draft report, server-side validation, human review
│   ├── jobs.py                    Processing Jobs: finalize → transcribe → analyse, with retries
│   ├── dashboard.py               dashboard data, candidate timeline, HR decision
│   └── retention.py               daily clean-up by retention period
├── integrations/hrms_adapter.py   the only place that reads HRMS records
├── video/                         provider abstraction: base, mock, jitsi
├── utils/                         settings, security (tokens, hashes), audit
├── setup/install.py               creates the app's four roles (after install / migrate)
├── grid_interview/                Module "Grid Interview"
│   ├── doctype/                   16 DocTypes (13 + 3 child tables; table below)
│   ├── page/                      grid_interview_dashboard, interview_scheduler
│   └── sidebar/grid_interview/    desk sidebar of the module (grouped menu)
├── www/                           interview-portal/*, interview/room, interview/join,
│                                  interview-reference (referee form)
├── templates/                     emails/*.html, includes/ (portal header)
├── public/                        js (desk buttons, portal, room), css, images/grid_interview.svg
└── tests/                         security policy, scheduling, portal, meeting, recording, dashboard
```

Repository root: `docs/` (this file, [internals](interview-internals.md),
[Hinglish guide](interview-hinglish.md)), `.github/workflows/build.yml` (image build),
`pyproject.toml`, `README.md`.

## How it works

```
HR ─► Interview Scheduler (/desk/interview-scheduler)
        │  grid_interview.api.scheduling.schedule_interview
        ▼
   services/scheduling ── creates the HRMS Interview *as the logged-in user* (HRMS rules apply)
        │                 + Grid Interview Video Session (random room) + candidate portal account
        ▼
   Emails (Notification Log, one per idempotency key) ── invitation, reminders, reschedule, cancel
        │
        ▼
Candidate ─► /interview-portal ─► Join (only inside the join window) ─► /interview/room
Interviewer ─────────────────────────────────────────────────────────►  (Mock or Jitsi)
        │
        ▼  (only when recording is enabled and every participant consented)
   Jibri upload (HMAC) ─► Processing Jobs: finalize ─► transcribe ─► AI draft
        │
        ▼
Interviewer: HRMS Interview Feedback · Reviewer: approves / corrects the AI draft
HR: records the decision on the HRMS Interview ─► publishes a Grid Interview Result ─► portal
```

- **HRMS is the record.** Interviews, rounds (Interview Type), interviewers and feedback stay in
  HRMS. Grid Interview reads them only through `integrations/hrms_adapter.py`.
- **The server decides.** Every endpoint takes the caller from the session (or a hashed, bound
  join token) and checks access in `services/`. The browser never decides moderator rights,
  recording permission or join timing.
- **Nothing runs twice.** Emails and background jobs carry unique idempotency keys, so scheduler
  re-runs and retries cannot duplicate them.
- **A human decides.** AI output is a draft; nothing reaches the candidate until HR publishes a
  Result.

A scheduler job runs every 5 minutes (`services.scheduling.run_cycle`): it manages Interviews
created in the standard HRMS form, reconciles changes made directly in HRMS Desk, sends due
reminders and syncs Email Queue status. A daily job applies
retention (`services.retention.run`).

## DocTypes

| DocType | Purpose |
|---|---|
| Grid Interview Settings (single) | Portal, join window, reminders, video provider, recording and consent, storage, transcription / AI providers, background jobs, retention. |
| Grid Interview Video Session (+ Participant Event) | One per HRMS Interview: random room id, times, status (Scheduled → In Progress → Completed / No Show / Expired / Cancelled), attendance events. |
| Grid Interview Access Session | Passwordless join links: hashed, expiring, revocable, bound to one browser. |
| Grid Interview Portal Profile | Candidate account ↔ applicant email; profile fields and private documents. |
| Grid Interview Reference | Candidate's referees and the single-use verification. |
| Grid Interview Result | The only outcome a candidate can see; written and published by HR. |
| Grid Interview Consent | Consent per participant and purpose (recording, external processing) with policy version. |
| Grid Interview Recording | Private storage key, checksum, lifecycle, retention. |
| Grid Interview Transcript (+ Segment) | Timestamped text segments; speaker marked "uncertain". |
| Grid Interview Analysis (+ Finding) | Schema-validated AI draft report; human review required. |
| Grid Interview Processing Job | Idempotent, retryable background step (finalize / transcribe / analyse). |
| Grid Interview Notification Log | One row per email: idempotency key, status, attempts. |
| Grid Interview Audit Log | Append-only record of sensitive actions (no secrets, tokens or transcript text). |

Where each DocType is created and used, and what breaks if it is removed:
[interview-doctypes.md](interview-doctypes.md) (Hinglish).

Reused from HRMS (never modified): Job Opening, Job Applicant, Interview Type (the round and its
expected skills), Interview, Interview Detail (interviewers), Interview Feedback, role
`Interviewer`.

## Roles

| Role | Can |
|---|---|
| Grid Interview Administrator | Settings, everything below, delete recordings, retry jobs |
| Grid Interview HR User (+ HRMS HR User) | Schedule / reschedule / cancel, decisions, publish results, references |
| Grid Interview Reviewer | Review AI drafts, view recordings and transcripts |
| HRMS Interviewer | Join and moderate assigned interviews only, give HRMS feedback |
| Grid Interview Candidate (Website User, no Desk) | Own interviews, profile, documents, references, published results |

Scheduling creates the HRMS Interview as the HR user, so HR staff also need an HRMS role (HR User
or HR Manager). A user with a Role Profile gets roles reset from it: add the roles to the profile.

## Desk menu

The module sidebar (`grid_interview/sidebar/grid_interview/grid_interview.json`) groups the app:

| Group | Entries |
|---|---|
| (top) | Dashboard, Interview Scheduler, Interviews, Job Applicants, Interview Feedback, Results |
| Video & Recording | Video Session, Consent, Recording, Transcript |
| AI Review | AI Analysis, Processing Job |
| Candidate Portal | Portal Profile, Reference, Join Link, Open Portal |
| Logs | Notification Log, Audit Log |
| (bottom) | Settings |

The apps screen shows the tile **Interviews** (`/desk/grid-interview-dashboard`) to users with a
Grid Interview role, HR Manager / HR User or Interviewer.

## Setup

1. **Grid Interview Settings → Candidate Portal:** Public Portal Base URL (the HTTPS address
   candidates use), support email / phone, privacy notice version.
2. **Scheduling and Notifications:** join window (default 15 minutes before to 30 after),
   reminder offsets (default `1440,60,15`). In HR Settings, turn **off** HRMS's own *Send
   Interview Reminder*, or Grid Interview sends no reminders (to avoid double reminders).
3. **Users:** HR staff get *Grid Interview HR User* + *HR User*; interviewers get *Interviewer*;
   reviewers get *Grid Interview Reviewer*.
4. **Video:** *Mock* works out of the box (own camera only, no media sent). For real calls set
   Video Provider = Jitsi with domain, JWT app id, secret (`openssl rand -hex 32`, at least 32
   characters) and audience, and run a two-browser test.
5. **Recording / transcription / AI** stay off until Jitsi, a Jibri host and provider keys exist;
   see [internals › Enabling features](interview-internals.md#enabling-features-safely).
6. Open **Interview Scheduler**, pick a Job Applicant, Interview Type, time and interviewers,
   and schedule.

## Scheduling and emails

- **Schedule** creates a draft HRMS Interview (status Pending) as the logged-in user, a Video
  Session, the candidate's portal account (no password; the email links to set-password) and
  the invitation emails.
- Checks: end after start, start in the future, at least 5 minutes and at most 8 hours, interviewers are active users
  with `Interviewer`, no second active interview for the same applicant and round, no overlap for
  the candidate or an interviewer (overridable after confirmation).
- **Reschedule** changes the same HRMS Interview; **Cancel** sets it to Cancelled and revokes join
  links. A draft, future Interview created in the standard HRMS form is managed automatically
  within 5 minutes (session, portal account, invitations), unless Settings › *Auto-manage
  Interviews Created in HRMS* is off; **Manage Desk Interviews** attaches one by hand.
- Emails come from `templates/emails/*.html` and can be overridden with an Email Template named
  `Grid Interview - <type> - <Candidate|Interviewer>`. "Sent" means accepted by the mail server.
- Use Grid Interview's Reschedule, not HRMS's: the HRMS button sends its own email as well.

## Candidate portal

| Route | Who | What |
|---|---|---|
| `/interview-portal` | candidate | upcoming / past interviews, application status, published results, Join |
| `/interview-portal/profile` | candidate | profile fields, private document upload / download |
| `/interview-portal/references` | candidate | add / edit references until HR starts verification |
| `/interview-portal/account` | candidate | change password, sign out other devices, emails sent |
| `/interview-portal/password` | guest | set / reset password (Frappe's own, rate limited) |
| `/interview-portal/register` | guest | self-registration, only if enabled in settings |
| `/interview-reference?t=…` | referee | single-use, expiring verification form |
| `/interview/room?session=…` | candidate, interviewers, HR | the video room |
| `/interview/join?t=…` | candidate | passwordless join link (off by default) |

A candidate sees only Job Applicants whose email equals their account email, and never sees
feedback, AI analysis, transcripts or internal notes. HRMS outcomes show as *Under Review* until
HR publishes a Result.

## Dashboard and decisions

**Interview Dashboard** (`/desk/grid-interview-dashboard`) shows counts and lists in two groups:

- *Interviews:* today, upcoming, in progress, completed, feedback pending, awaiting decision;
- *Recording & AI:* recordings processing, AI drafts to review, transcription failures, AI failures.

Filters: date range, job opening, department, round, interviewer. Interviewers see only their
own interviews. *Actions* on a row: open the session or HRMS Interview, give feedback, open the
AI draft, **Record decision** (sets the HRMS Interview to Cleared / Rejected / Under Review,
warns about missing feedback, optionally submits) and the **Candidate timeline** (one audited,
chronological view of everything about a candidate).

## Recording, transcription and AI

Off by default. When enabled: every participant in the meeting must consent (current policy
version) before a moderator can start recording; withdrawing consent stops it. Jibri uploads the
file in HMAC-signed chunks; the pipeline then runs as Processing Jobs: **finalize** (verify size,
SHA-256, duration, store) → **transcribe** → **AI draft** → email to reviewers. External providers
are used only if the setting allows it **and** the candidate gave *External Processing* consent.
Reviewers *Approve*, *Correct*, *Reject* or *Regenerate* the draft; the AI never writes HRMS
feedback or decisions. Details: [internals › Recording pipeline](interview-internals.md#recording-pipeline).

Recordings are stored as *Local Private* (default `<site>/private/grid_interview_recordings`,
outside every web-served folder) and are **not** part of Frappe backups. S3 is implemented behind
the same interface.

## Deployment

The image is the official `frappe/erpnext` v16 image plus Frappe HR (`version-16`) and this app.
`.github/workflows/build.yml` builds it with frappe_docker's layered Containerfile from:

```json
[
  {"url": "https://github.com/frappe/erpnext", "branch": "version-16"},
  {"url": "https://github.com/frappe/hrms", "branch": "version-16"},
  {"url": "https://github.com/akashdataanalyst/Grid_interview", "branch": "main"}
]
```

frappe_docker's layered Containerfile does not install ffmpeg. Recording (finalize uses
`ffprobe`, transcription uses `ffmpeg`) needs an image that adds it, for example a layer with
`apt-get install -y --no-install-recommends ffmpeg`; Settings refuse *Enable Recording* without it.

New site:

```bash
bench new-site <site> --install-app erpnext --install-app hrms
bench --site <site> install-app grid_interview
```

Install `grid_interview` with the scheduler and queue workers stopped (or in a separate step):
installing it in the same `new-site` call while workers run can hit a MariaDB deadlock (1213) and
leave it half installed; `install-app grid_interview --force` repairs that.

Update: deploy the new image, then `bench --site <site> migrate` (syncs DocTypes, pages and the
sidebar; `after_migrate` re-creates missing roles). Do not set `allow_cors` to `*` on the site:
join-link users are protected against CSRF by a custom header that relies on it.

## Security summary

- The caller comes only from the session (or a hashed, browser-bound join token); APIs accept no
  user or role parameters.
- Twelve guest endpoints, each in a reviewed allowlist with its reason; every state-changing
  endpoint is POST-only and every read GET-only (enforced by tests).
- Tokens (join links, reference links) are 256-bit random, stored only as SHA-256, expiring and
  revocable; opening a link does not redeem it, so mail scanners cannot use it up.
- Candidate CSRF tokens are created by the portal (Frappe does not create them for website
  users); state-changing candidate requests without one are rejected.
- Uploads: extension allowlist, magic-byte check, size and count limits, Frappe's content
  inspection, optional malware-scan hook that fails closed.
- Recordings: outside web folders (mode 640), streamed only after a permission check, every play
  audited; S3 objects private, SSE, presigned URLs of 5 minutes or less.
- Secrets are Password fields, never returned to the browser or logged; Jitsi and webhook secrets
  need at least 32 characters.
- AI input is the transcript, job title, round and expected skills only; the prompt and a server
  check exclude protected traits. Full list: [internals › Security](interview-internals.md#security-controls).

## Tests

`grid_interview/tests` (run on a test site, never on production; they need `allow_tests`):

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app grid_interview
bench --site <test-site> run-tests --module grid_interview.tests.test_api_security
```

| File | Checks |
|---|---|
| `test_api_security.py` | guest allowlist, GET / POST rules, no `ignore_permissions` or raw SQL in `api/` |
| `test_install.py` | roles, no custom fields / property setters / hooks on standard DocTypes |
| `test_hrms_adapter.py` | HRMS reads, exact email matching |
| `test_scheduling.py` | schedule, reschedule, cancel, reconcile, reminders, emails |
| `test_portal.py` | candidate accounts, data isolation, documents, references, results |
| `test_meeting.py` | join window, room lifecycle, join links |
| `test_recording.py` | consent, recording control, ingest, transcription, AI validation |
| `test_dashboard.py` | dashboard counts, decisions, timeline, retention |
| `test_security_utils.py` | tokens, hashing, audit redaction |
