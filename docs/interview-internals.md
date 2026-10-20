# Grid Interview internals

How Grid Interview works inside: components, data model, the main flows, the recording
pipeline, security controls, configuration and operations. For the overview and setup see
[interview.md](interview.md); for a Hinglish walkthrough see [interview-hinglish.md](interview-hinglish.md).

## Architecture

```
                 ┌──────────────── Frappe stack (erpnext + hrms + grid_interview image) ────────────────┐
 HR / reviewers  │  Desk: Interview Dashboard, Interview Scheduler, sidebar, forms of app DocTypes        │
 interviewers ──►│  api/*  (thin, whitelisted)  ──►  services/*  (authorization + business rules)         │
                 │     scheduling · notifications · portal · candidates · references · results           │
 candidates ────►│     meeting · consent · recording · storage · transcription · analysis · jobs         │
 (portal, room)  │     dashboard · retention                                                             │
                 │  integrations/hrms_adapter ──► HRMS: Interview, Interview Type, Interview Feedback,     │
                 │                                Job Applicant, Job Opening                              │
                 │  video/ (Mock, Jitsi)     RQ workers: Processing Jobs     Scheduler: */5 + daily      │
                 └────────┬──────────────────────────┬──────────────────────────────┬────────────────────┘
                          │ IFrame API + room JWT    │ HMAC-signed chunked upload   │ HTTPS (optional proxy)
                  ┌───────▼────────┐        ┌────────▼─────────┐        ┌───────────▼────────────────────┐
                  │ Jitsi (own host)│─rec──►│ Jibri (own host,  │        │ OpenAI-compatible transcription│
                  │ JWT auth, lobby │       │ snd-aloop)        │        │ and chat APIs (only if allowed)│
                  └─────────────────┘       └───────────────────┘        └────────────────────────────────┘
                  Storage: Local Private (server disk / EBS) now · S3 (private, SSE, presigned) later
```

Three layers, always in this direction:

```
Browser (desk page / portal / room JS)
   │  API call only
   ▼
api/*.py            take the request, call a service, return the answer (no rules here)
   ▼
services/*.py       permission checks, business rules, emails, status changes
   ▼
integrations/hrms_adapter.py   the only code that reads HRMS
video/*.py                     the only code that talks to a video provider
DocTypes                       data
```

## Data model

| DocType | Links to |
|---|---|
| Video Session (+ Participant Event) | Interview, Job Applicant (Dynamic Links) |
| Access Session | Video Session |
| Consent | Video Session, Interview |
| Recording | Video Session, Interview |
| Transcript (+ Segment) | Recording, Video Session, Interview |
| Analysis (+ Finding) | Interview, Transcript |
| Result | Job Applicant, Interview |
| Reference | candidate User, Job Applicant |
| Portal Profile | User |
| Notification Log, Processing Job, Audit Log | any record (Dynamic Link) |
| Settings (single) | — |

HRMS records are referenced through Dynamic Links, so the app installs without HRMS.

## Main flows

1. **Schedule:** HR → `scheduling.schedule_interview` → HRMS Interview (as the user) → Video
   Session → candidate account (invitation) → emails.
2. **Reconcile (every 5 minutes, `run_cycle`):**

   | Changed directly in HRMS | Grid Interview does |
   |---|---|
   | New Interview (draft, future, no session) | `manage_desk_interviews`: session, candidate account, invitations (setting `auto_manage_desk_interviews`) |
   | Interview time | updates the Video Session, revokes old join links, sends "rescheduled" |
   | Interview cancelled | Video Session Cancelled, access revoked, sends "cancelled" |
   | Interview deleted | Video Session Cancelled (links ignored on save), access revoked, audited; no email, the interview data is gone |
   | Interviewer added | invites the new interviewer |
   | Interviewer removed | they can no longer join (every join re-checks HRMS) |

   Then sends due reminders (only the latest one if the scheduler was down) and syncs Email Queue
   status, retrying failures with exponential backoff up to *Max Email Attempts*. If HR Settings ›
   *Send Interview Reminder* is on, Grid Interview sends no reminders.
3. **Join:** portal or join link → `meeting.join` (join window + participant check) → provider
   join config (Jitsi JWT) → attendance events and presence (Redis) → lifecycle.
4. **Record:** everyone consents → moderator starts → Jibri uploads (HMAC) → finalize job →
   storage → transcription job → analysis job → reviewers notified.
5. **Decide:** feedback in HRMS → reviewer handles the AI draft → HR records the decision on the
   HRMS Interview → HR publishes a Result → candidate sees it in the portal.

Status flow:

```
HRMS Interview:  Pending ──────────────────────────► Cleared / Rejected / Under Review  (HR decision)
                    └─► Cancelled

Video Session:   Scheduled ──► In Progress ──► Completed
                    ├──► No Show / Expired   (nobody came / window passed)
                    └──► Cancelled
```

## Scheduling details

- The HRMS Interview is created and changed with the HR user's own permissions. HRMS makes the
  time fields set-only-once, so reschedule writes them with `db_set` after a write-permission
  check (as HRMS's own Reschedule does) and adds a timeline comment.
- Roles allowed to schedule: `services/scheduling.py › SCHEDULER_ROLES` (Grid Interview
  Administrator / HR User, HR Manager, HR User, System Manager).
- Every email is a Notification Log row with a unique idempotency key; re-runs never send
  duplicates.
- Known limitation: HRMS's own *Reschedule* button also emails, so participants get two
  messages; reschedule from Grid Interview.

## Video room

- `/interview/room?session=<id>`: device check, the embedded meeting, a details panel, Leave, and
  End interview for moderators. Interviewers also see the candidate, the round's expected skills
  and links to the Job Applicant / HRMS Interview.
- **Providers** (`video/`): `Mock` (own camera only, no media sent; development and tests) and
  `Jitsi` (official IFrame API; per-participant, room-scoped HS256 JWT; moderator flag decided on
  the server; never valid after the join window; the secret never reaches the browser). A new
  provider is one class plus its entry in `video/__init__.py`.
- **Who may join:** the candidate whose applicant email matches, assigned interviewers
  (moderators) and Grid Interview HR / Administrators (moderators), only inside the join window
  of a non-terminal session. Every POST needs a CSRF token; passwordless link users need a
  custom header.
- **Lifecycle:** *In Progress* only when a candidate and an interviewer have joined; *Completed*
  when a moderator ends a meeting both sides joined, or it goes quiet after the window; an
  interviewer alone after the window is *No Show*, otherwise *Expired*.
- **Passwordless join links** (off by default): single-person, expiring, hash-only. Opening the
  link does nothing until the candidate presses *Continue* (mail scanners cannot use it up). The
  first use binds it to that browser with an HttpOnly cookie; repeated failures revoke it, and so
  do reschedule, cancel and end.

## Recording pipeline

**Consent.** Each change is a new Consent row with the policy version; an older version does not
count. Recording starts only when every participant currently in the meeting (the candidate must
be present) has current consent. Someone without consent joining stops it; withdrawal stops it
and, per *On Consent Withdrawal*, suspends or deletes the recording.

**Control.** Only a moderator starts or stops recording, and only when the server agrees:
recording enabled, session eligible and In Progress, consents complete, concurrency and free-disk
limits respected. Jitsi's own record button is hidden; everyone sees a server-driven indicator.

**Jibri → server.** The Jibri host uploads in HMAC-signed chunks to `api/recording.ingest_*`
(timestamp ±5 minutes, replay protection, per-chunk and whole-file SHA-256). Uploads for rooms
without an authorised recording are refused. The Jibri-side uploader (`gi_finalize.py`) and the
Jitsi / Jibri host notes were in `deploy/` and were removed from the repository in commit
`ff19aa5`; restore them from git history (`git show ff19aa5^:deploy/jibri/gi_finalize.py`) before
enabling recording.

**Jobs.** Each step is a Processing Job with a unique idempotency key and a row lock:

```
Recording Finalize ──► Transcription ──► AI Analysis ──► email to interviewers and reviewers
(reassemble, size,     (ffmpeg → mono     (schema, enums,
 SHA-256, ffprobe,      16 kHz, 10-min     timestamps, protected-
 max duration, store)   parts, provider)   trait check)
```

Failures roll back the step and retry with exponential backoff up to *Max Attempts per Job*, then
become Failed (administrators can *Retry Now*). Jobs orphaned by a killed worker are recovered by
the scheduler.

**Transcription.** OpenAI-compatible `/audio/transcriptions`, or *Mock*. Speakers are not
identified, so every segment is marked *speaker uncertain*.

**AI draft.** Input: transcript, job title, round and expected skills only. The prompt forbids
protected or non-job-related traits; the server validates schema, enums and timestamps, checks
protected-trait phrases, downgrades "Transcript Fact" evidence not found verbatim to
"Interpretation", and forces "Insufficient Evidence" for very short transcripts. The AI never
writes HRMS feedback, never changes the Interview and never creates a candidate-facing Result.

**Storage.** *Local Private*: default `<site>/private/grid_interview_recordings`, outside
`public/` and `private/files/`, file mode 640, played through `api/recording.stream` (Range
requests) after a permission check, every access audited. *S3*: private bucket, SSE-AES256, size
verification, presigned URLs; switching is a setting plus copying existing files.

## Candidate portal details

- Candidates are Website Users with *Grid Interview Candidate* (no Desk). Scheduling creates the
  account automatically; existing System Users are never given the candidate role automatically.
- Documents are private Files on the Portal Profile; downloads are ownership-checked and audited.
- References: *Send Verification Email* locks the reference; the referee sees only the
  candidate's name, the position and the stated relationship. *Request Clarification* unlocks it.
  The email body with the link is redacted from Email Queue after sending.
- Results: a published Result is locked until unpublished; the email only says that an update is
  available, the text is shown after sign-in.

## Security controls

| Area | Control |
|---|---|
| Identity | caller from `frappe.session.user` or a hashed, browser-bound token; no user / role parameters |
| Row level | `permission_query_conditions` + `has_permission` limit interviewers to their interviews |
| Tokens | 256-bit random, SHA-256 stored, expiring, revocable, GET does not redeem |
| CSRF | portal creates the token for website users; link users need a custom header; never set `allow_cors` to `*` |
| Rate limits | registration, referee responses, link redemption, join / heartbeat / consent, uploads, ingest |
| Enumeration | registration and password reset answer the same for every email |
| Uploads | extension allowlist, magic bytes, size / count limits, safe names, content inspection, optional malware hook (fails closed) |
| Recordings | outside web folders, permission-checked streaming, audited plays, S3 SSE + short presigned URLs |
| Ingest | HMAC-SHA256 over timestamp, method and parameters; ±5 minutes; replay cache; SHA-256 per chunk and file |
| Secrets | Password fields, never returned or logged; Jitsi / webhook secrets ≥ 32 characters |
| Output | user text HTML-escaped in emails and pages |
| SQL | dashboard filters are bound parameters; no other raw SQL takes user input |
| Audit | append-only rows for access, consent, recording, playback, publication, decisions, deletions, settings changes (field names only), refusals |

Guest endpoints (reviewed allowlist in `tests/test_api_security.py`): `portal.register`,
`portal.submit_reference_response`, `video.room_info / join / heartbeat / leave / redeem_link`,
`recording.consent_status / set_consent`, `recording.ingest_start / ingest_chunk /
ingest_complete`.

Privacy: consent per participant and purpose with policy versions; external AI only with the
settings opt-in **and** the candidate's External Processing consent; retention per data type with
audited deletions; suspended recordings kept for legal hold. HR / legal should approve the consent
text, privacy notice, retention periods and the external provider before production use.

Residual risks: join and reference tokens appear in URLs (and access logs) until redeemed; emails
with one-time links sit in Email Queue until sent; block-level erase is not guaranteed on EBS /
SSD (use encrypted volumes); Frappe's missing CSRF token for website users also affects other
website-user pages on the same site.

## Configuration and secrets

Everything is in **Grid Interview Settings**. Secrets are Password fields encrypted with the
site's `encryption_key`; nothing secret belongs in the image, git or container environment.

| Secret | Field | Shared with | Generate |
|---|---|---|---|
| Jitsi JWT secret | Jitsi JWT App Secret | Jitsi `JWT_APP_SECRET` | `openssl rand -hex 32` |
| Recording webhook secret | Recording Webhook Secret | Jibri host `GI_WEBHOOK_SECRET` | `openssl rand -hex 32` |
| S3 secret key (optional) | S3 Secret Access Key | AWS IAM | prefer an EC2 instance role |
| Transcription API key | Transcription API Key | provider | provider console |
| AI API key | AI API Key | provider | provider console |

Defaults: join window 15 minutes before to 30 after; reminders 1440, 60, 15 minutes; emails 3
attempts; jobs 5 attempts, backoff from 60 s; recording 120 minutes, 2048 MB, 1 concurrent;
retention: access sessions 90 days, recordings 180, transcripts and AI drafts 365, audit logs
1095, notification logs 180.

### Enabling features safely

Always on a test site first.

| Step | Settings | Prerequisites |
|---|---|---|
| Portal | Public Portal Base URL, support contacts, privacy notice | HTTPS public address |
| Self-registration | Allow Candidate Self-Registration | approved privacy notice |
| Real video | Video Provider = Jitsi, domain, app id, secret, audience | own Jitsi host; two-browser test passed |
| Join links | Allow Passwordless Join Links | Jitsi working |
| Recording | Enable Recording, consent text and version, webhook secret, limits | Jibri host with `snd-aloop` and the uploader; one full recording tested |
| S3 | Storage Backend = S3, bucket, region (IAM role) | private bucket, Block Public Access, SSE, no versioning or a lifecycle rule; copy existing files |
| Transcription / AI | Allow External Providers + provider fields | data processing terms; approved consent wording |

Minimum IAM policy for S3 (`HeadObject` is covered by `s3:GetObject`):

```json
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
  "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
  "Resource": "arn:aws:s3:::<bucket>/<prefix>*"}]}
```

## Retention

A daily job (`services/retention.run`) applies the periods in settings (0 = keep): recordings
are deleted from storage and the row kept as *Deleted* (with their transcripts and AI drafts);
expired transcripts and drafts are erased; old access sessions, notification logs and audit logs
are deleted; abandoned upload folders are removed after 48 hours; recordings stuck in Finalizing
for over 24 hours are marked Failed. Suspended recordings are never auto-deleted. Each run is
audited with counts only.

## Operations

| Where | Healthy | Act when |
|---|---|---|
| Interview Dashboard tiles | failures = 0 | transcription / AI failures > 0: open the Processing Job, fix, *Retry Now* |
| Processing Job | Succeeded | Failed, or Retry Scheduled repeatedly |
| Notification Log | Sent | Failed: check Email Account / Email Queue errors |
| Recording | Stored | Failed or Finalizing for over 24 h: check Jibri logs |
| Error Log | no `grid_interview` entries | any entry |
| Disk (Local Private) | above *Minimum Free Disk* | new recordings are refused below it |

| Symptom | Cause / fix |
|---|---|
| 502 after restarting the backend | nginx cached the old backend IP: restart the frontend too |
| `KeyError: 'ContainerConfig'` on `docker-compose up` | compose v1 with newer images: `docker rm -f` the app containers first |
| Install stops with MariaDB deadlock 1213 | workers ran during install: stop scheduler / queue containers, `install-app grid_interview --force` |
| Candidate sees "not a candidate account" | the email belongs to a System User; give the role manually or use a personal email |
| Candidate sees no interviews | account email differs from the Job Applicant email |
| Join refused "You can join from …" | outside the join window |
| Embedded room stays blank | Jitsi not reachable over HTTPS, or a proxy sends X-Frame-Options / frame-ancestors |
| "Waiting for recording consent from …" | someone in the meeting has not consented (policy version must match) |
| Jibri upload `Signature expired` | clock skew over 5 minutes: fix NTP |
| No reminders sent | HR Settings › *Send Interview Reminder* is on, or the offsets are blank |
| Two reschedule emails | HRMS's own Reschedule button was used |

**Backups:** `bench backup --with-files` covers all records and portal documents. Recordings are
not included: back up the Local Private folder separately (for example EBS snapshots) or use S3.

**Capacity (measure on the real host):** grid_interview adds little to the ERPNext host (a laptop
test peaked at about 1.9 vCPU / 260 MB for the backend at 20–30 concurrent portal / room requests;
room heartbeats use only Redis). Jitsi: 2–4 vCPU, 4–8 GB RAM for a few concurrent 1:1 interviews,
UDP 10000 open. Jibri: about 2 vCPU and 2–4 GB RAM per concurrent recording. Disk: about 0.5–1 GB
per recorded hour at 720p, times the retention period.

## Known limitations

- Speaker labels are not available from Whisper-style APIs ("speaker uncertain").
- Rescheduling changes date and time only; change interviewers on the HRMS Interview (the next
  cycle invites new ones).
- One Video Session per HRMS Interview; panel rounds use several interviewers in one session.
- Jitsi and Jibri are verified by configuration and unit tests only; a live two-browser call and a
  real Jibri recording must be tested on the target infrastructure.
- Without HRMS the app installs, but scheduling and the portal's interview features are unavailable.
