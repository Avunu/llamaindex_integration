# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""File.after_insert handler: turns a received email attachment into a Llama Document Log.

The handler only guards, creates the log and enqueues the job. Inline (Content-Id) status is
checked later, inside the job, against the final Communication content.
"""

import os
from typing import Any

import frappe

from llamaindex_integration import tasks
from llamaindex_integration.llamaindex_integration.doctype.llamaindex_settings.llamaindex_settings import (
	get_settings,
)

LOG_DOCTYPE = "Llama Document Log"
# Data fields are varchar(140) unless they set a length. Longer values make the insert fail.
DATA_FIELD_LENGTH = 140
FAILURE_TITLE = "LlamaIndex: could not queue email attachment"


def handle_incoming_email_attachment(doc: Any, method: str | None = None) -> None:
	"""File.after_insert entry point. Never raises.

	Guards: attached_to_doctype is Communication and the file is not a folder. Settings are enabled.
	The Communication is Received, belongs to monitored_email_account, and has communication_type
	Communication. The extension is allowed. Then it creates the log with status Queued and calls
	tasks.enqueue_processing. The whole body runs inside try/except Exception, and only the file
	name is logged.
	"""
	try:
		_queue_attachment(doc)
	except Exception:
		_report_failure(doc)


def is_candidate_file(file_doc: Any, settings: Any) -> bool:
	"""Return True when the file's extension is in settings.allowed_extensions, ignoring case.

	Pure; reads no database rows. Inline-image status is not decided here. The job decides it.
	"""
	return _extension(file_doc.file_name) in _parse_extensions(settings.allowed_extensions)


def _queue_attachment(doc: Any) -> None:
	if doc.attached_to_doctype != "Communication" or doc.is_folder:
		return

	settings = get_settings()
	if not settings.enabled:
		return

	comm = _get_communication(doc.attached_to_name)
	if comm is None:
		return
	if comm.communication_type != "Communication" or comm.sent_or_received != "Received":
		return
	# An empty monitored account must not match a Communication that has no email account.
	if not settings.monitored_email_account or comm.email_account != settings.monitored_email_account:
		return

	if not is_candidate_file(doc, settings):
		return

	if frappe.db.exists(LOG_DOCTYPE, {"source_file": doc.name, "status": ("!=", "Failed")}):
		return

	log = frappe.get_doc(
		{
			"doctype": LOG_DOCTYPE,
			"status": "Queued",
			"source_file": doc.name,
			"source_communication": doc.attached_to_name,
			"sender_email": _fit_data(comm.sender),
			"email_subject": _fit_data(comm.subject),
		}
	).insert(ignore_permissions=True)

	try:
		tasks.enqueue_processing(log.name, 0)
	except Exception:
		# A Queued log with no job would block a new log for this file and a manual retry.
		# Failed keeps it retryable, and the outer handler records the file name.
		log.set_status("Failed", error="Could not queue processing.")
		log.save(ignore_permissions=True)
		raise


def _get_communication(name: str | None) -> Any:
	if not name:
		return None
	return frappe.db.get_value(
		"Communication",
		name,
		["communication_type", "sent_or_received", "email_account", "sender", "subject"],
		as_dict=True,
	)


def _report_failure(doc: Any) -> None:
	file_name = getattr(doc, "file_name", None) or ""
	try:
		frappe.log_error(title=FAILURE_TITLE, message=f"File: {file_name}")
	except Exception:
		frappe.logger("llamaindex_integration").error(
			"Could not record ingestion failure for file %s", file_name
		)


def _fit_data(value: str | None) -> str | None:
	return value[:DATA_FIELD_LENGTH] if value else value


def _parse_extensions(raw: str | None) -> set[str]:
	parts = (part.strip().lstrip(".").lower() for part in (raw or "").split(","))
	return {part for part in parts if part}


def _extension(file_name: str | None) -> str:
	return os.path.splitext(file_name or "")[1].lstrip(".").lower()
