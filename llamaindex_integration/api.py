# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint

from llamaindex_integration import tasks

LOG_DOCTYPE = "Llama Document Log"
RETRYABLE_STATUSES = ("Failed", "Needs Review")


@frappe.whitelist(methods=["POST"])
def retry_document_log(log_name: str) -> str:
	"""Re-enqueue a Llama Document Log that is Failed or Needs Review.

	Only System Manager may call this. Raises frappe.ValidationError when created_docname is already
	set, so a retry never creates a second document, or when the status is neither Failed nor Needs
	Review. Otherwise it increments retry_count, sets the status to Queued, and returns the job id
	from tasks.enqueue_processing. The attempt number is the new retry_count, so the job id is new.
	"""
	frappe.only_for("System Manager")
	log = frappe.get_doc(LOG_DOCTYPE, log_name, for_update=True)

	if log.created_docname:
		frappe.throw(
			_("{0} {1} was already created from this log, so it cannot be retried.").format(
				log.created_doctype, log.created_docname
			),
			exc=frappe.ValidationError,
		)
	if log.status not in RETRYABLE_STATUSES:
		frappe.throw(
			_("Only logs with status Failed or Needs Review can be retried. This log is {0}.").format(
				log.status
			),
			exc=frappe.ValidationError,
		)

	log.retry_count = cint(log.retry_count) + 1
	log.set_status("Queued")
	log.save()
	return tasks.enqueue_processing(log.name, log.retry_count)
