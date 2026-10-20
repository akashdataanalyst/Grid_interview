# Grid Interview

Recruitment interview platform for Frappe / ERPNext v16 on top of Frappe HR (HRMS): interview
scheduling with emails and reminders, a candidate portal, an embedded video room, consent-based
recording, transcription and human-reviewed AI interview reports.

`grid_interview` is a standalone app. It reuses the standard HRMS records (Job Applicant,
Interview, Interview Type, Interview Feedback) and does not modify Frappe, ERPNext, HRMS or
`grid_erp`: no Custom Fields, Property Setters or `doc_events` on standard DocTypes.

## Requirements

| | Version |
|---|---|
| Frappe | 16 (tested on 16.52.0) |
| ERPNext | 16 (tested on 16.51.1) |
| Frappe HR (hrms) | `version-16` (tested on 16.51.0); optional, but needed for scheduling and the portal |
| ffmpeg / ffprobe | in the Frappe image, only needed when recording is enabled |

## Install

```bash
bench get-app https://github.com/akashdataanalyst/Grid_interview
bench --site <site> install-app grid_interview
```

Install it with the scheduler and queue workers stopped, or in its own step after
`new-site ... --install-app erpnext --install-app hrms`. Then open **Grid Interview Settings**;
see [docs/interview.md › Setup](docs/interview.md#setup).

## Documentation

| Document | Contents |
|---|---|
| [docs/interview.md](docs/interview.md) | Overview: code layout, how it works, DocTypes, roles, desk menu, setup, portal, dashboard, deployment, security summary, tests |
| [docs/interview-internals.md](docs/interview-internals.md) | Architecture, data model, flows, video room, recording pipeline, security controls, configuration and secrets, retention, operations, known limitations |
| [docs/interview-doctypes.md](docs/interview-doctypes.md) | (Hinglish) har DocType: kya hai, kaun banata hai, code mein kahan use, hatane par kya tootega |
| [docs/interview-hinglish.md](docs/interview-hinglish.md) | (Hinglish) poora flow HRMS ke saath, sidebar, roles, code ka naksha, kahan kya badlein |

## Tests

On a test site only (never production):

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app grid_interview
```

## License

MIT
