"""Idempotent, retryable background jobs (Grid Interview Processing Job + Frappe RQ).

* One Processing Job per idempotency key (unique), e.g. "transcription:<recording>": submitting
  the same work twice returns the existing job instead of creating a duplicate.
* A job row is locked (SELECT ... FOR UPDATE) while it runs, so two workers never run it together.
* Failures roll back the job's partial work, then retry with exponential backoff up to
  Max Attempts per Job; after that the job is Failed and an administrator can retry it.
* Jobs left "Running" by a killed worker are recovered by the scheduler.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from grid_interview.utils import audit
from grid_interview.utils.settings import get_settings

JOB = "Grid Interview Processing Job"
ACTIVE = ("Queued", "Running", "Retry Scheduled", "Succeeded")
STALE_RUNNING = timedelta(hours=2)

# job type -> function(reference_name) that does the work
HANDLERS = {
	"Recording Finalize": "grid_interview.services.recording.finalize",
	"Transcription": "grid_interview.services.transcription.transcribe",
	"Analysis": "grid_interview.services.analysis.analyze",
}

# job type -> function(reference_name, error) run after the final failed attempt
FAILURE_HANDLERS = {
	"Recording Finalize": "grid_interview.services.recording.on_job_failed",
	"Transcription": "grid_interview.services.transcription.on_job_failed",
	"Analysis": "grid_interview.services.analysis.on_job_failed",
}


def submit(job_type: str, reference_doctype: str, reference_name: str, key: str | None = None) -> str:
	key = key or f"{job_type}:{reference_name}"
	existing = frappe.db.get_value(JOB, {"idempotency_key": key}, ["name", "status"], as_dict=True)
	if existing and existing.status in ACTIVE:
		return existing.name
	settings = get_settings()
	if existing:  # Failed / Cancelled: a new submission restarts it
		frappe.db.set_value(JOB, existing.name, {"status": "Queued", "attempts": 0, "error_summary": None,
												 "next_retry_on": None})
		name = existing.name
	else:
		try:
			name = frappe.get_doc(
				{
					"doctype": JOB,
					"job_type": job_type,
					"reference_doctype": reference_doctype,
					"reference_name": reference_name,
					"idempotency_key": key,
					"queue": settings.processing_queue or "long",
					"status": "Queued",
					"attempts": 0,
					"max_attempts": cint(settings.max_job_attempts) or 5,
				}
			).insert(ignore_permissions=True).name
		except frappe.DuplicateEntryError:
			return frappe.db.get_value(JOB, {"idempotency_key": key}, "name")
	_enqueue(name)
	return name


def _enqueue(name: str):
	queue = frappe.db.get_value(JOB, name, "queue") or "long"
	frappe.enqueue(
		"grid_interview.services.jobs.run",
		queue=queue,
		timeout=3600,
		job_id=f"grid_interview:{name}",
		deduplicate=True,
		enqueue_after_commit=True,
		job=name,
	)


def run(job: str):
	"""Worker entry point."""
	row = frappe.db.get_value(JOB, job, ["name", "status", "job_type", "reference_name", "attempts", "max_attempts"],
							  as_dict=True, for_update=True)
	if not row or row.status not in ("Queued", "Retry Scheduled"):
		return  # already done, running elsewhere, or cancelled
	attempts = cint(row.attempts) + 1
	frappe.db.set_value(JOB, job, {"status": "Running", "attempts": attempts, "started_on": now_datetime(),
								   "next_retry_on": None})
	_commit()
	frappe.db.savepoint("gi_job")
	try:
		frappe.get_attr(HANDLERS[row.job_type])(row.reference_name)
	except Exception as e:
		frappe.db.rollback(save_point="gi_job")  # undo the handler's partial work only
		error = f"{type(e).__name__}: {str(e)[:300]}"
		frappe.log_error(title=f"grid_interview job failed: {row.job_type} {row.reference_name}")
		if attempts < cint(row.max_attempts or 5) and not isinstance(e, PermanentError):
			base = cint(get_settings().retry_base_seconds) or 60
			frappe.db.set_value(JOB, job, {"status": "Retry Scheduled", "error_summary": error,
										   "next_retry_on": add_to_date(now_datetime(), seconds=base * 2 ** (attempts - 1))})
		else:
			frappe.db.set_value(JOB, job, {"status": "Failed", "error_summary": error, "finished_on": now_datetime()})
			audit.log("job.failed", JOB, job, actor="Administrator", job_type=row.job_type)
			try:
				frappe.get_attr(FAILURE_HANDLERS[row.job_type])(row.reference_name, error)
			except Exception:
				frappe.log_error(title=f"grid_interview failure handler failed: {row.job_type}")
		_commit()
		return
	frappe.db.set_value(JOB, job, {"status": "Succeeded", "finished_on": now_datetime(), "error_summary": None})
	_commit()


def _commit():
	# Workers persist job state between steps; tests keep everything in one rolled-back transaction.
	if not (frappe.flags.in_test or getattr(frappe, "in_test", False)):
		frappe.db.commit()


class PermanentError(Exception):
	"""Raise from a handler when retrying cannot help (bad input, policy refusal)."""


def retry_due(now=None):
	"""Scheduler: re-enqueue jobs whose backoff elapsed; recover jobs orphaned by a dead worker."""
	now = now or now_datetime()
	for name in frappe.get_all(JOB, filters={"status": "Retry Scheduled", "next_retry_on": ["<=", now]}, pluck="name"):
		_enqueue(name)
	for row in frappe.get_all(JOB, filters={"status": "Running"}, fields=["name", "started_on"]):
		if row.started_on and get_datetime(row.started_on) < now - STALE_RUNNING:
			frappe.db.set_value(JOB, row.name, {"status": "Retry Scheduled", "next_retry_on": now,
												"error_summary": "Recovered after worker interruption"})
			_enqueue(row.name)


def admin_retry(name: str):
	if frappe.session.user != "Administrator" and not {"Grid Interview Administrator", "System Manager"} & set(frappe.get_roles()):
		raise frappe.PermissionError(_("Only administrators can retry jobs."))
	status = frappe.db.get_value(JOB, name, "status")
	if status not in ("Failed", "Cancelled", "Retry Scheduled"):
		frappe.throw(_("Only failed, cancelled or waiting jobs can be retried."))
	frappe.db.set_value(JOB, name, {"status": "Queued", "attempts": 0, "error_summary": None, "next_retry_on": None})
	audit.log("job.retried", JOB, name)
	_enqueue(name)
