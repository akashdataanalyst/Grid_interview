# Grid Interview: har DocType kahan use hota hai

Grid Interview ke **16 DocTypes** hain: 12 normal, 1 single (Settings) aur 3 child tables. Ye file
batati hai ki har DocType kya hai, kaun / kab banata hai, code mein kahan use hota hai, kaun dekh
sakta hai, aur use **hataya** jaaye to kya tootega.

Code se check kiya gaya (2026-10-18): har DocType kam se kam ek feature ka **akela** data store
hai, aur koi do DocTypes ek hi kaam nahi karte. Isliye koi bhi DocType hatane se ek feature band
hota hai; hataya kuch nahi gaya. Jo features abhi Settings mein band hain (passwordless join links,
recording, transcription, AI), unke DocTypes khaali padey rehte hain aur unka koi kharcha nahi.

HRMS ke DocTypes (Job Applicant, Interview, Interview Type, Interview Feedback, Job Opening) Grid
Interview ke nahi hain; wo sirf padhe jaate hain (`integrations/hrms_adapter.py`).

## Ek nazar mein

| # | DocType | Type | Feature | Abhi chalu? | Hata sakte hain? |
|---|---|---|---|---|---|
| 1 | Grid Interview Settings | single | sab | ✅ | ❌ app chalega hi nahi |
| 2 | Grid Interview Video Session | normal | scheduling, room, sab ka centre | ✅ | ❌ sab tootega |
| 3 | Grid Interview Participant Event | child (Video Session) | attendance | ✅ | ❌ No Show / In Progress status nahi banega |
| 4 | Grid Interview Notification Log | normal | emails | ✅ | ❌ emails dobara jaayengi, retry / status nahi |
| 5 | Grid Interview Portal Profile | normal | candidate portal | ✅ | ❌ portal profile / documents nahi |
| 6 | Grid Interview Result | normal | result publish | ✅ | ❌ candidate ko result nahi dikhega |
| 7 | Grid Interview Reference | normal | references | ✅ | ⚠ references feature poora jaayega |
| 8 | Grid Interview Audit Log | normal | security trail | ✅ | ⚠ kisne kya dekha / kiya ka record jaayega |
| 9 | Grid Interview Access Session | normal | passwordless join link | ⏸ band | ⚠ join link feature jaayega |
| 10 | Grid Interview Consent | normal | recording consent | ⏸ band | ⚠ recording legal nahi rahegi |
| 11 | Grid Interview Recording | normal | recording | ⏸ band | ⚠ recording jaayegi |
| 12 | Grid Interview Transcript | normal | transcription | ⏸ band | ⚠ transcript + AI jaayega |
| 13 | Grid Interview Transcript Segment | child (Transcript) | transcript ki lines | ⏸ band | ⚠ Transcript ke saath hi |
| 14 | Grid Interview Analysis | normal | AI draft report | ⏸ band | ⚠ AI report jaayegi |
| 15 | Grid Interview Analysis Finding | child (Analysis) | AI report ke points | ⏸ band | ⚠ Analysis ke saath hi |
| 16 | Grid Interview Processing Job | normal | background kaam | ⏸ band (recording ke saath) | ⚠ recording pipeline retry nahi karegi |

❌ = core, hata hi nahi sakte. ⚠ = hata sakte hain, par wo feature code se bhi hatana padega
(services, api, tests, docs), aur uska data chala jaayega.

Agar kabhi app chhoti karni ho, to sabse bada hissa **recording + AI** hai: Consent, Recording,
Transcript (+ Segment), Analysis (+ Finding), Processing Job = 7 DocTypes, plus
`services/consent.py`, `recording.py`, `storage.py`, `transcription.py`, `analysis.py`, `jobs.py`,
`api/recording.py`, room ki recording UI aur `tests/test_recording.py`. Iske bina scheduling,
emails, portal, room, result aur dashboard waise hi chalte rahenge.

## Rishte (kaun kisse juda hai)

```
HRMS Interview ──1:1── Video Session ──┬── Participant Event (child)      kaun kab aaya / gaya
   │                                   ├── Access Session               join links
   │                                   ├── Consent                      har vyakti ki permission
   │                                   └── Recording ── Transcript ── Analysis
   │                                                    └ Segment      └ Finding (child)
   │
Job Applicant ── Result (HR publish karta hai)
              ── Reference (candidate jodta hai)
User (candidate) ── Portal Profile (+ private documents)

Notification Log · Processing Job · Audit Log ── kisi bhi record se (Dynamic Link)
Settings (single) ── sab padhte hain
```

## Har DocType detail mein

### 1. Grid Interview Settings (single)

- **Kya:** app ki saari settings: portal, join window, reminders, video provider, recording,
  storage, transcription / AI, background jobs, retention. Secrets Password fields mein.
- **Kaun badalta hai:** Grid Interview Administrator / System Manager (Desk → Settings).
- **Code:** `utils/settings.py:get_settings` se har service padhti hai (meeting, notifications,
  portal, recording, storage, transcription, retention, `video/jitsi.py`). Save par
  `grid_interview_settings.py` values check karta hai (ranges, Jitsi secret ≥ 32, recording ke
  liye consent text, ffmpeg) aur Audit Log mein sirf badle fields ke naam likhta hai.

### 2. Grid Interview Video Session

- **Kya:** ek HRMS Interview = ek Video Session. Room ka random naam, start / end, status
  (Scheduled → In Progress → Completed / No Show / Expired / Cancelled), candidate, attendance.
- **Kaun banata hai:** Interview Scheduler (`services/scheduling.py:schedule_interview`,
  `create_session`) ya HRMS mein bane Interview ko attach karne par (`attach_existing`).
- **Code:** sabse zyada use hone wala record:
  - `services/scheduling.py`: reschedule, cancel, har 5 minute HRMS se milan (`reconcile_sessions`);
  - `services/meeting.py`: join, heartbeat, leave, end, status (`finalize_sessions`), join links;
  - `services/notifications.py`: email ka context, reminders;
  - `services/portal.py`: candidate ke apne interviews; `services/dashboard.py`: list, decision;
  - recording, consent, transcription, analysis: kis session ka hai;
  - `permissions.py:video_session_query`: interviewer ko sirf apne sessions;
  - `www/interview/room.py`, `public/js/interview_room.js`: room page.
- **Kaun dekhta hai:** Administrator, HR User, Reviewer, Interviewer (sirf apne).

### 3. Grid Interview Participant Event (child of Video Session, field `participant_events`)

- **Kya:** room mein kaun kab join / reconnect / leave / disconnect hua.
- **Kaun banata hai:** `services/meeting.py` (`_register_join`, `leave`, heartbeat timeout).
- **Kyun zaroori:** isi se pata chalta hai ki dono taraf ke log aaye the ya nahi, yaani In
  Progress, Completed ya No Show.

### 4. Grid Interview Notification Log

- **Kya:** har email ki ek row: type (scheduled, reminder, rescheduled, cancelled, access link,
  result, reference …), kisko, status (Queued / Sent / Failed / Skipped), attempts, Email Queue link.
- **Kaun banata hai:** `services/notifications.py:notify` (har email se pehle). Unique
  *idempotency key* ki wajah se ek email do baar nahi banti.
- **Code:** `notifications.py` (bhejna, retry, `sync_delivery_status`), `api/scheduling.py`
  (Scheduler page par emails ki list), `services/portal.py:notification_history` (candidate ke
  Account page par "emails sent"), `retention.py` (purani rows hatana).
- **Frappe ki Email Queue kaafi kyun nahi:** Email Queue ek email ka status rakhti hai, par "ye
  reminder is interview ke liye pehle hi gaya hai kya?" iska jawab nahi deti. Wo yahan se aata hai.
- **Delete:** *Grid Interview Administrator* rows delete kar sakta hai (test data saaf karna). Row
  hatne par wahi email (same idempotency key) dobara ban sakti hai, isliye asli interviews ki rows
  na hatayein; purani rows retention job khud hatata hai.
- **Audit Log** ke ulat: Audit Log delete nahi hota (controller rokta hai), sirf retention job.

### 5. Grid Interview Portal Profile

- **Kya:** candidate ke portal account (Website User) ki profile: naam, phone, headline,
  location, LinkedIn, experience (saal), qualification, resume, privacy notice kab accept kiya;
  aur unke private documents isi record par attached.
- **Kaun banata hai:** `services/candidates.py:ensure_profile` (schedule karte waqt) ya
  `register` (self-registration chalu ho to).
- **Code:** `services/portal.py` (profile dikhana / badalna, documents upload / download /
  delete), `candidates.py:require_candidate` (har portal request par check).
- **Kaun dekhta hai:** candidate sirf apna (portal se); HR / Administrator Desk mein.

### 6. Grid Interview Result

- **Kya:** HR ka likha hua result jo candidate ko dikhta hai. Candidate sirf yahi outcome dekh
  sakta hai, feedback / AI kabhi nahi.
- **Kaun banata hai:** HR (Desk → Results), *Publish to Candidate* (`services/results.py:publish`).
- **Code:** `results.py` (publish / unpublish, publish ke baad lock), `services/portal.py`
  (`published_results`, `candidate_status`), `portal_emails.py` ("update available" email),
  `dashboard.py:timeline`.

### 7. Grid Interview Reference

- **Kya:** candidate ke referees (naam, email, rishta, company) aur referee ka jawab.
- **Kaun banata hai:** candidate portal ke References page se (`services/references.py:add_reference`).
- **Code:** `references.py` (add / edit / delete, *Send Verification* → ek-baar ka link,
  `submit_response`, *Request Clarification*), `portal_emails.py` (referee email),
  `www/interview_reference.py` (referee ka form), `dashboard.py:timeline`.
- **Hatane par:** references ka poora feature (portal page, referee form, HR buttons) jaayega.

### 8. Grid Interview Audit Log

- **Kya:** append-only record: kisne kab kya dekha / kiya (recording play, result publish,
  decision, consent, settings change, mana kiya gaya access …). Secrets, tokens, transcript text
  kabhi nahi.
- **Kaun banata hai:** sirf `utils/audit.py:log`, jo poori app se call hota hai.
- **Code:** `dashboard.py:timeline` (candidate timeline mein dikhata hai), `retention.py`
  (purane rows).
- **Frappe ka Version / Access Log kaafi kyun nahi:** Version sirf document badalne par banta
  hai; "recording kisne dekhi" ya "join mana kiya gaya" jaisi baatein usmein nahi aati.

### 9. Grid Interview Access Session

- **Kya:** passwordless join link: sirf token ka hash, expiry, kis browser se bandha hai, kitni
  galat koshishein.
- **Kaun banata hai:** HR *Send Join Link* (`services/meeting.py:create_link_token`).
- **Code:** `meeting.py` (`redeem_link`, `access_from_cookie`, `join`, `_revoke_links`),
  `scheduling.py:revoke_access` (reschedule / cancel par band), `retention.py`.
- **Abhi:** Settings → *Allow Passwordless Join Links* band hai, isliye table khaali hai.
  Candidates portal login se join karte hain.

### 10. Grid Interview Consent

- **Kya:** har participant ki har purpose (recording, external processing) ki consent, policy
  version ke saath. Har badlav ek nayi row.
- **Kaun banata hai:** room mein consent tick / untick (`services/consent.py:record`).
- **Code:** `consent.py:current`, `recording.py:start` (sabki consent ke bina recording nahi),
  `meeting.py` (bina consent wala join kare to recording band).

### 11. Grid Interview Recording

- **Kya:** ek recording file ki jaankari: storage key (path / S3 key), size, SHA-256, duration,
  status (Requested, Recording, Uploading, Finalizing, Stored, Failed, Suspended, Deleted), retention.
  File khud database mein nahi hoti.
- **Kaun banata hai:** moderator *Start recording* (`services/recording.py:start`); Jibri upload
  `ingest_start / ingest_chunk / ingest_complete` se file aati hai.
- **Code:** `recording.py` (start, stop, ingest, `finalize`, `stream` = play / download),
  `transcription.py` (agla step), `consent.py` / `meeting.py` (consent hatne par stop),
  `retention.py` (purani files hatana, atki hui Failed karna), `dashboard.py:timeline`.

### 12. Grid Interview Transcript

- **Kya:** recording ka text: provider, model, language, status.
- **Kaun banata hai:** Processing Job "Transcription" (`services/transcription.py:transcribe`).
- **Code:** `analysis.py` (AI ka input yahi hai), `dashboard.py` (failures tile, timeline),
  `retention.py`, `permissions.py` (interviewer sirf apne interview ka).

### 13. Grid Interview Transcript Segment (child of Transcript, field `segments`)

- **Kya:** transcript ki ek line: start / end second, text, "speaker uncertain", confidence.
- **Kaun banata hai:** `transcription.py:transcribe`. AI report ke timestamps inhi se check hote hain.

### 14. Grid Interview Analysis

- **Kya:** AI ki **draft** report (summary, provisional recommendation, uncertainty notes,
  findings), review status (Pending Review / Approved / Corrected / Rejected), reviewer ke notes,
  provider, model, prompt aur report version.
- **Kaun banata hai:** Processing Job "AI Analysis" (`services/analysis.py:analyze`); reviewer
  Desk form se review karta hai (`review`, `regenerate`).
- **Code:** `analysis.py` (prompt, server validation, `notify_reviewers`), `dashboard.py`
  ("AI drafts to review" tile, row ka *AI draft* button), `portal_emails.py` (reviewers ko email),
  `retention.py`, `permissions.py`.

### 15. Grid Interview Analysis Finding (child of Analysis, field `findings`)

- **Kya:** report ka ek point: type, skill, basis (Transcript Fact / Interpretation), claim,
  evidence (transcript ka hissa) aur uska start / end second.
- **Kaun banata hai:** `analysis.py:analyze` (validated AI output se).

### 16. Grid Interview Processing Job

- **Kya:** ek background step (abhi Recording Finalize, Transcription, Analysis use hote hain):
  status (Queued / Running / Succeeded / Retry Scheduled / Failed / Cancelled), attempts, next
  retry, error.
- **Kaun banata hai:** `services/jobs.py:submit` (recording ke baad ek ke baad ek).
- **Code:** `jobs.py` (`run`, retry backoff ke saath, `retry_due` scheduler se, `admin_retry` =
  *Retry Now*). Unique key ki wajah se ek step do baar nahi chalta.
- **Frappe RQ job kaafi kyun nahi:** RQ job worker restart par kho jaata hai aur retry / status
  Desk mein nahi dikhata; ye DocType dono deta hai.

## Kahan dikhte hain (Desk sidebar)

| Sidebar group | DocTypes |
|---|---|
| (upar) | Results |
| Video & Recording | Video Session, Consent, Recording, Transcript |
| AI Review | Analysis (AI Analysis), Processing Job |
| Candidate Portal | Portal Profile, Reference, Access Session (Join Link) |
| Logs | Notification Log, Audit Log |
| (neeche) | Settings |

Child tables (Participant Event, Transcript Segment, Analysis Finding) apne parent form ke andar
dikhte hain, sidebar mein nahi.
