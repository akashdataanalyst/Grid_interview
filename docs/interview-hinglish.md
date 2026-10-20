# Grid Interview: Hinglish guide

Ye file samjhati hai ki **Grid Interview** app **HRMS** (Frappe HR) ke saath kaise kaam karti hai:
ek interview ka poora safar, kaun kya kar sakta hai, aur kaunsi file kya karti hai. English detail
ke liye [interview.md](interview.md) aur [interview-internals.md](interview-internals.md) dekhein.

## 1. Ek line mein

> **HRMS interview ka "record" rakhta hai. Grid Interview uske upar scheduling, emails, candidate
> portal, video room, recording aur AI report ka kaam karta hai.**

Grid Interview HRMS ka code, DocTypes ya fields **nahi badalta**. Wo HRMS ke apne records (Job
Applicant, Interview, Interview Type, Interview Feedback) waise hi use karta hai, aur apna extra
data apne alag DocTypes mein rakhta hai.

```
 ┌──────────────────────── HRMS (pehle se tha) ────────────────────────┐
 │  Job Opening → Job Applicant → Interview (Interview Type ke saath)   │
 │                                   └→ Interview Feedback (interviewer)│
 └───────────────────────────────▲─────────────────────────────────────┘
                                 │ padhta hai / HR ke naam se banata hai
 ┌──────────────────────── Grid Interview (naya) ──────────────────────┐
 │  Video Session · Emails · Candidate Portal · Room · Recording        │
 │  Transcript · AI Analysis · Result · References · Audit Log          │
 └──────────────────────────────────────────────────────────────────────┘
```

## 2. Desk mein kahan kya hai (sidebar)

Desk par **Grid Interview** kholne par sidebar groups mein dikhta hai:

| Group | Kya milega |
|---|---|
| (upar) | **Dashboard**, **Interview Scheduler**, Interviews (HRMS), Job Applicants, Interview Feedback, Results |
| Video & Recording | Video Session, Consent, Recording, Transcript |
| AI Review | AI Analysis, Processing Job |
| Candidate Portal | Portal Profile, Reference, Join Link, Open Portal |
| Logs | Notification Log, Audit Log (band rehta hai, click karke kholein) |
| (neeche) | Settings |

Apps screen (`/apps`) par tile **Interviews** seedha Dashboard kholti hai.

## 3. Kaun kya hai (HRMS ↔ Grid Interview)

| HRMS ka record | Grid Interview mein use |
|---|---|
| **Job Applicant** | Candidate. Uska **email** hi portal account ki pehchaan hai. |
| **Interview Type** | Round (jaise "Technical Round"). Iski **Expected Skill Set** AI report ka rubric hai. |
| **Interview** | Asli interview: date, time, interviewers, status. Har Interview ka ek **Video Session**. |
| **Interview Detail** | Interviewers ki list. Sirf yahi log room join karte hain aur moderator hote hain. |
| **Interview Feedback** | Interviewer HRMS mein hi feedback deta hai. Dashboard dikhata hai kiska baaki hai. |
| **Job Opening** | Dashboard filters aur emails mein job title. |

| Grid Interview ka record | Kaam |
|---|---|
| **Video Session** | Ek HRMS Interview = ek Video Session: room ka naam, status, samay. |
| **Notification Log** | Har email ka record; ek email do baar nahi jaati. |
| **Portal Profile** | Candidate ki profile aur documents. |
| **Consent** | Recording aur AI processing ki permission. |
| **Recording / Transcript / Analysis** | Recording file, uska text, aur AI ki draft report. |
| **Result** | Jo result HR candidate ko portal par dikhata hai. |
| **Reference** | Candidate ke referees aur unke jawab. |
| **Audit Log** | Kisne kab kya dekha / kiya (badla nahi ja sakta). |

## 4. Poora flow, step-by-step

```
 ① HR schedule ──► ② Emails ──► ③ Candidate portal ──► ④ Room (video)
                                                            │
 ⑧ Candidate result dekhe ◄── ⑦ Result publish ◄── ⑥ HR decision ◄── ⑤ Feedback + AI report
```

**① Schedule** — Sidebar → *Interview Scheduler*. HR chunta hai: Job Applicant, Interview Type,
date, time, interviewers. System check karta hai (permission, time sahi, interviewer ke paas
`Interviewer` role, overlap nahi), phir **HRMS Interview HR ke naam se** banta hai (status Pending),
uska **Video Session**, candidate ka **portal account** (Website User, Desk nahi), aur emails.
HRMS ke standard **Interview** form se bana interview (draft, aage ka time) **5 minute ke andar
apne aap** jud jaata hai: Video Session, portal account aur emails, bilkul Scheduler jaisa
(Settings → *Auto-manage Interviews Created in HRMS*; band karne par "Manage Desk Interviews" se
haath se jodein). Interview kam se kam 5 minute aur zyada se zyada 8 ghante ka; HRMS form mein
isse chhota / bada time ho to interview **nahi judta** aur Error Log mein wajah aati hai.
Candidate ka email kisi **System User** (employee / admin) ka na ho: aise email ka candidate
account nahi banta, portal "not a candidate account" dikhata hai (Audit Log:
`candidate.account_skipped`).

**② Emails** — Invitation (candidate ko portal + password set karne ka link), reminders **24
ghante, 1 ghanta, 15 minute** pehle (Settings mein badlein), reschedule / cancel ki email. Har
email Notification Log mein, aur ek hi baar. HR Settings mein HRMS ka *Send Interview Reminder*
**band** rakhein.

**③ Candidate portal** — `/interview-portal`: apne interviews aur **Join** button, profile aur
documents, references, aur sirf **published** results. Candidate ko sirf apna data dikhta hai
(login email = Job Applicant email).

**④ Room** — `/interview/room?session=<video session>`. **15 minute pehle** se **end ke 30 minute
baad** tak khulta hai. Sirf assigned interviewers aur wo candidate join kar sakte hain. Video
abhi **Mock** hai (asli video ke liye apna Jitsi server). Recording band hai; chalu karne par
room mein **har vyakti ki consent** ke baad hi shuru hoti hai.

**⑤ Feedback + AI** — Interviewer HRMS ka **Interview Feedback** bharta hai. Recording +
transcription + AI chalu ho to: recording → transcript → AI **draft** report. Reviewer use approve
/ correct karta hai. **AI kabhi faisla nahi karta**; umar, dharm, jaati jaisi baatein report mein
mana hain.

**⑥ Decision** — Dashboard → row ka *Actions* → **Record decision**: Cleared / Rejected / Under
Review. Ye HRMS Interview ke status mein save hota hai. Feedback baaki ho to chetavni aati hai.
**Job Applicant ka status apne aap nahi badalta.**

**⑦ Result publish** — HR **Result** banata hai aur **Publish** karta hai. Publish ke baad text
lock; candidate ko email + portal par dikhta hai.

**⑧ Candidate** portal ke Results mein published result dekhta hai.

## 5. HRMS mein seedha badlav kiya to?

Har **5 minute** ek job HRMS aur Grid Interview ko milata hai:

| HRMS mein kiya | Grid Interview kya karta hai |
|---|---|
| Naya Interview banaya (form se) | Video Session, portal account, invitation emails (setting chalu ho to) |
| Interview ka time badla | Video Session update, purane join links band, "rescheduled" email |
| Interview Cancelled | Video Session Cancelled, access band, "cancelled" email |
| Interview delete | Video Session Cancelled, access band; **email nahi** (interview ki jaankari hi nahi bachi). Candidate ko batana ho to pehle Cancel karein |
| Naya interviewer joda | Naye interviewer ko invitation |
| Interviewer hataya | Wo room join nahi kar paayega |

> **Reschedule Grid Interview se hi karein.** HRMS ka apna Reschedule button alag email bhejta
> hai, to do emails jaayengi.

## 6. Status ka safar

```
HRMS Interview:  Pending ─────────────────────► Cleared / Rejected / Under Review  (⑥)
                    └─► Cancelled

Video Session:   Scheduled ──► In Progress ──► Completed
                    ├──► No Show / Expired   (koi nahi aaya / window nikal gayi)
                    └──► Cancelled
```

## 7. Kaun kya kar sakta hai (roles)

| Role | Kya kar sakta hai |
|---|---|
| **Grid Interview Administrator** | Settings, sab kuch, recordings delete, jobs retry |
| **Grid Interview HR User** + HRMS **HR User** | Schedule, reschedule, cancel, decision, result publish, references |
| **Grid Interview Reviewer** | AI draft review, recording / transcript dekhna |
| HRMS **Interviewer** | Sirf apne interviews: join, moderate, feedback |
| **Grid Interview Candidate** | Sirf apne interviews, profile, documents, references, results |

> HR staff ko HRMS ka **HR User** role bhi chahiye (Interview unke naam se banta hai). Jis user
> par **Role Profile** laga hai, uske roles profile se reset hote hain; role profile mein jodein.

## 8. Code ka naksha

**Teen layer ka niyam:** browser sirf `api/` ko call karta hai; `api/` patli hai aur kaam
`services/` ko deti hai; asli faisle (permission, rules, emails, status) `services/` mein hote
hain; HRMS sirf `integrations/hrms_adapter.py` se padha jaata hai.

```
grid_interview/                       ← repo
├── README.md                         ← app ka parichay aur docs ki list
├── docs/                             ← interview.md, interview-internals.md, ye file
├── .github/workflows/build.yml       ← image build (ERPNext + HRMS + Grid Interview)
├── pyproject.toml
└── grid_interview/                   ← app ka code
    ├── hooks.py                      ← apps tile, permissions, scheduler (5 min + roz), portal menu
    ├── permissions.py                ← interviewer ko sirf apne interviews
    ├── api/                          ← browser yahin call karta hai
    ├── services/                     ← asli dimaag
    ├── integrations/hrms_adapter.py  ← HRMS se baat sirf yahin
    ├── video/                        ← Mock / Jitsi provider
    ├── utils/                        ← settings, tokens, audit
    ├── setup/install.py              ← install / migrate par 4 roles
    ├── grid_interview/               ← Frappe module
    │   ├── doctype/                  ← saare DocTypes
    │   ├── page/                     ← Dashboard, Interview Scheduler
    │   └── sidebar/grid_interview/   ← Desk ka grouped sidebar
    ├── www/                          ← portal, room, join link, referee form
    ├── templates/                    ← emails, portal header
    ├── public/                       ← JS, CSS, icon
    └── tests/
```

| `api/` file | Kiske liye |
|---|---|
| `scheduling.py` | Scheduler page: schedule, reschedule, cancel, attach, interviewer search |
| `dashboard.py` | Dashboard: counts, list, timeline, decision, recording delete |
| `hr.py` | Result publish / unpublish, reference verification / clarification |
| `portal.py` | Candidate portal: register, profile, documents, references |
| `video.py` | Room: info, join, heartbeat, leave, end, join link |
| `recording.py` | Consent, recording start / stop, Jibri upload, AI review, job retry |

| `services/` file | Kaam |
|---|---|
| `scheduling.py` | HRMS Interview banana / badalna (HR ke naam se), Video Session, 5-minute milan (`run_cycle`) |
| `notifications.py` | Saari emails; ek email ek hi baar; delivery status |
| `portal_emails.py` | Result published, reference emails |
| `candidates.py` | Candidate account, self-registration, CSRF |
| `portal.py` | Candidate ko kya dikhe |
| `references.py`, `results.py` | Referees; result publish / unpublish |
| `meeting.py` | Kaun, kab join kare; room ka status; join links |
| `consent.py`, `recording.py` | Consent; recording start / stop, Jibri upload, file verify |
| `storage.py` | File kahan: Local disk (abhi) ya S3 (baad mein) |
| `transcription.py`, `analysis.py` | Text banana; AI draft + check + human review |
| `jobs.py` | Background queue (retry ke saath): finalize → transcribe → analyse |
| `dashboard.py` | Dashboard data, timeline, decision |
| `retention.py` | Roz purana data hatana |

## 9. Kuch badalna hai to kahan

| Badlav | Kahan |
|---|---|
| Email ka text | Desk → Email Template "Grid Interview - <type> - <Candidate/Interviewer>", ya `templates/emails/` |
| Reminder, join window, retention, limits | Desk → Grid Interview Settings (code nahi) |
| Kaun schedule kar sakta hai | `services/scheduling.py` → `SCHEDULER_ROLES` |
| Candidate ko kya dikhe | `services/portal.py` |
| Room join ke niyam | `services/meeting.py` |
| Sidebar ke menu | `grid_interview/sidebar/grid_interview/grid_interview.json` (phir `bench migrate`) |
| Naya video provider | `video/` mein nayi class + `video/__init__.py` |
| HRMS ka koi field | sirf `integrations/hrms_adapter.py` |

## 10. Code badalne ke baad

1. Code badlein → **test site** par tests: `bench --site <test-site> run-tests --app grid_interview`
   (production site par kabhi nahi).
2. Commit karein → nayi image banayein (ERPNext + HRMS + Grid Interview).
3. Test site par image check karein.
4. Production: backup → nayi image → `bench --site <site> migrate`.

## 11. Links (localhost:8080 par)

| Kya | URL |
|---|---|
| Interview Dashboard | `/desk/grid-interview-dashboard` |
| Interview Scheduler | `/desk/interview-scheduler` |
| Settings | `/desk/grid-interview-settings` |
| HRMS Interviews | `/desk/interview` |
| Job Applicants | `/desk/job-applicant` |
| Candidate portal | `/interview-portal` |
| Interview room | `/interview/room?session=<video session>` |
