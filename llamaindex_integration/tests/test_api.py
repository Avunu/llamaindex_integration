# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for api.retry_document_log: the POST-only whitelist, the status check, and the refusal when
created_docname is already set.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from llamaindex_integration.api import retry_document_log

ENQUEUE = "llamaindex_integration.tasks.enqueue_processing"
LOG = "Llama Document Log"
NON_RETRYABLE_STATUSES = (
	"Queued",
	"Classifying",
	"Extracting",
	"Resolving Entities",
	"Completed",
	"Ignored",
)


class TestRetryDocumentLog(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.enqueue = self.enterContext(patch(ENQUEUE, return_value="job-retry"))

	def _make_log(self, status: str, **overrides) -> str:
		suffix = frappe.generate_hash(length=8)
		source = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"scan-{suffix}.pdf",
				"file_url": f"https://files.example.com/{suffix}/scan.pdf",
				"is_remote_file": 1,
			}
		).insert(ignore_permissions=True)
		values = {"doctype": LOG, "status": status, "source_file": source.name}
		values.update(overrides)
		return frappe.get_doc(values).insert(ignore_permissions=True).name

	def _log_state(self, name: str) -> frappe._dict:
		return frappe.db.get_value(LOG, name, ["status", "retry_count"], as_dict=True)

	def test_whitelist_is_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[retry_document_log], ["POST"])

	def test_retry_requeues_failed_log(self):
		name = self._make_log("Failed")

		job_id = retry_document_log(name)

		self.assertEqual(job_id, "job-retry")
		state = self._log_state(name)
		self.assertEqual(state.status, "Queued")
		self.assertEqual(state.retry_count, 1)
		self.enqueue.assert_called_once_with(name, 1)

	def test_retry_allowed_for_needs_review(self):
		name = self._make_log("Needs Review")

		retry_document_log(name)

		self.assertEqual(self._log_state(name).status, "Queued")
		self.enqueue.assert_called_once_with(name, 1)

	def test_each_retry_gets_a_new_attempt_number(self):
		name = self._make_log("Failed", retry_count=1)

		retry_document_log(name)

		self.assertEqual(self._log_state(name).retry_count, 2)
		self.enqueue.assert_called_once_with(name, 2)

	def test_refuses_when_document_already_created(self):
		name = self._make_log("Failed", created_doctype="User", created_docname="Administrator")

		with self.assertRaises(frappe.ValidationError):
			retry_document_log(name)

		self.assertEqual(self._log_state(name).status, "Failed")
		self.enqueue.assert_not_called()

	def test_refuses_statuses_other_than_failed_and_needs_review(self):
		for status in NON_RETRYABLE_STATUSES:
			with self.subTest(status=status):
				name = self._make_log(status)

				with self.assertRaises(frappe.ValidationError):
					retry_document_log(name)

				self.assertEqual(self._log_state(name).status, status)
		self.enqueue.assert_not_called()

	def test_only_system_manager_may_retry(self):
		name = self._make_log("Failed")
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			retry_document_log(name)

		self.assertEqual(self._log_state(name).status, "Failed")
		self.enqueue.assert_not_called()
