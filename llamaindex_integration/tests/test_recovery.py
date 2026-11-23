# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for the recovery paths in tasks.py.

The stale-log sweep fails a non-terminal log that stopped without a terminal status (a killed worker, or a
Queued log whose job never reached RQ), so Retry Ingestion works. _record_failure keeps the stored state of
a log whose attempt is no longer current, and of a log whose document already exists.

These tests commit their fixtures, because _record_failure rolls back the open transaction first.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from llamaindex_integration import tasks

LOG = "Llama Document Log"
IS_ENQUEUED = "llamaindex_integration.tasks.is_job_enqueued"


class _LogFixtures(IntegrationTestCase):
	def _make_log(self, status: str, retry_count: int = 0, **overrides) -> str:
		suffix = frappe.generate_hash(length=8)
		source = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"recovery-{suffix}.pdf",
				"file_url": f"https://files.example.com/{suffix}/recovery.pdf",
				"is_remote_file": 1,
			}
		).insert(ignore_permissions=True)
		values = {"doctype": LOG, "status": status, "source_file": source.name, "retry_count": retry_count}
		values.update(overrides)
		name = frappe.get_doc(values).insert(ignore_permissions=True).name
		frappe.db.commit()
		return name

	def _age(self, name: str, seconds: int) -> None:
		"""Make the log look as if it last changed `seconds` ago."""
		stamp = add_to_date(now_datetime(), seconds=-seconds)
		frappe.db.set_value(LOG, name, "modified", stamp, update_modified=False)
		frappe.db.commit()

	def _status(self, name: str) -> str:
		return frappe.db.get_value(LOG, name, "status")


class TestFailStaleLogs(_LogFixtures):
	def setUp(self):
		super().setUp()
		self.is_enqueued = self.enterContext(patch(IS_ENQUEUED, return_value=False))

	def test_stale_in_progress_log_is_failed_and_gets_the_retry_message(self):
		name = self._make_log("Extracting")
		self._age(name, tasks.STALE_AFTER_S + 60)

		tasks.fail_stale_logs()

		log = frappe.get_doc(LOG, name)
		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.error_traceback, tasks.STALE_MESSAGE)

	def test_queued_log_with_no_job_is_failed(self):
		# Covers a commit-time enqueue failure, which leaves a Queued log with no job behind it.
		name = self._make_log("Queued")
		self._age(name, tasks.STALE_AFTER_S + 60)

		tasks.fail_stale_logs()

		self.assertEqual(self._status(name), "Failed")

	def test_recent_log_is_left_alone(self):
		name = self._make_log("Classifying")
		self._age(name, 60)

		tasks.fail_stale_logs()

		self.assertEqual(self._status(name), "Classifying")

	def test_log_whose_job_is_still_queued_or_started_is_left_alone(self):
		name = self._make_log("Queued", retry_count=2)
		self._age(name, tasks.STALE_AFTER_S + 60)
		self.is_enqueued.return_value = True

		tasks.fail_stale_logs()

		self.assertEqual(self._status(name), "Queued")
		self.is_enqueued.assert_any_call(f"llama-log-{name}-a2")

	def test_terminal_logs_are_never_touched(self):
		names = {status: self._make_log(status) for status in ("Needs Review", "Failed", "Completed")}
		for name in names.values():
			self._age(name, tasks.STALE_AFTER_S * 10)

		tasks.fail_stale_logs()

		self.assertEqual(
			{status: self._status(name) for status, name in names.items()},
			{
				"Needs Review": "Needs Review",
				"Failed": "Failed",
				"Completed": "Completed",
			},
		)


class TestRecordFailureKeepsStoredState(_LogFixtures):
	def test_failure_after_a_committed_document_keeps_its_terminal_status(self):
		# A callback can raise after tasks.py has committed the document. The log must not be marked Failed.
		target = frappe.get_doc({"doctype": "ToDo", "description": "Recovery target"}).insert(
			ignore_permissions=True
		)
		name = self._make_log("Completed", created_doctype="ToDo", created_docname=target.name)

		tasks._record_failure(name, RuntimeError("after commit"), retry=False)

		self.assertEqual(self._status(name), "Completed")
		self.assertEqual(frappe.db.get_value(LOG, name, "created_docname"), target.name)

	def test_superseded_attempt_failure_does_not_overwrite_the_newer_attempt(self):
		name = self._make_log("Queued", retry_count=1)

		tasks._record_failure(name, RuntimeError("from attempt 0"), retry=True, attempt=0)

		self.assertEqual(self._status(name), "Queued")
		self.assertEqual(frappe.db.get_value(LOG, name, "retry_count"), 1)
