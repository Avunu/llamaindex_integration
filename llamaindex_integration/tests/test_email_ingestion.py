# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for services/email_ingestion.py: the File.after_insert guards, log creation, and the rule that
the handler never raises.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase

from llamaindex_integration.services.email_ingestion import (
	handle_incoming_email_attachment,
	is_candidate_file,
)

ENQUEUE = "llamaindex_integration.tasks.enqueue_processing"
GET_SETTINGS = "llamaindex_integration.services.email_ingestion.get_settings"
LOG = "Llama Document Log"
ALLOWED_EXTENSIONS = "pdf,png,jpg,jpeg,docx,xlsx"
RULE_DOCTYPE = "ToDo"
LOG_FIELDS = ["name", "status", "source_communication", "sender_email", "email_subject", "error_traceback"]


class TestHandleIncomingEmailAttachment(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.enqueue = self.enterContext(patch(ENQUEUE, return_value="job-test"))
		self.log_error = self.enterContext(patch("frappe.log_error"))
		self._configure_settings(enabled=False, account=None)

	def _configure_settings(self, *, enabled: bool, account: str | None) -> None:
		settings = frappe.get_single("LlamaIndex Settings")
		settings.enabled = 1 if enabled else 0
		settings.llama_cloud_api_key = "test-key"
		settings.monitored_email_account = account
		settings.allowed_extensions = ALLOWED_EXTENSIONS
		# Enabling requires a Document Rule. The handler itself reads only `enabled`. Rule validation refuses
		# ToDo as a target, because targets must be submittable. These fixtures never create a document, so
		# they save without validation. The monitored account is blank while the handler is switched off.
		if not settings.doctype_rules:
			settings.append(
				"doctype_rules",
				{"target_doctype": RULE_DOCTYPE, "classification_description": "Follow-up request"},
			)
		# The monitored account is mandatory, and the fixtures switch the handler off with it blank.
		settings.flags.ignore_mandatory = True
		settings.flags.ignore_validate = True
		settings.save()

	def _make_inbox(self) -> str:
		suffix = frappe.generate_hash(length=8)
		account = frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_account_name": f"Llama Test Inbox {suffix}",
				"email_id": f"llama-{suffix}@example.com",
			}
		).insert(ignore_permissions=True)
		return account.name

	def _make_communication(self, account: str | None, **overrides) -> Document:
		values = {
			"doctype": "Communication",
			"communication_type": "Communication",
			"communication_medium": "Email",
			"sent_or_received": "Received",
			"email_account": account,
			"sender": "billing@vendor.example",
			"subject": "Invoice INV-0042",
			"content": "Invoice attached.",
		}
		values.update(overrides)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	def _make_attachment(self, comm: Document, file_name: str) -> Document:
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"file_url": f"https://files.example.com/{suffix}/{file_name}",
				"is_remote_file": 1,
				"attached_to_doctype": "Communication",
				"attached_to_name": comm.name,
			}
		).insert(ignore_permissions=True)

	def _scenario(
		self,
		*,
		file_name: str = "invoice-42.pdf",
		monitored: str | None = None,
		enabled: bool = True,
		**comm_overrides,
	) -> Document:
		"""Create an inbox, a received Communication and its attachment, then set the settings.

		Settings are off while the fixtures are inserted, so File.after_insert creates nothing.
		`monitored` defaults to the inbox the Communication arrived on.
		"""
		self._configure_settings(enabled=False, account=None)
		account = self._make_inbox()
		comm = self._make_communication(account, **comm_overrides)
		attachment = self._make_attachment(comm, file_name)
		self._configure_settings(enabled=enabled, account=monitored or account)
		return attachment

	def _logs_for(self, attachment: Document) -> list[frappe._dict]:
		return frappe.get_all(LOG, filters={"source_file": attachment.name}, fields=LOG_FIELDS)

	def test_queues_log_and_enqueues_job(self):
		attachment = self._scenario()

		handle_incoming_email_attachment(attachment)

		logs = self._logs_for(attachment)
		self.assertEqual(len(logs), 1)
		log = logs[0]
		self.assertEqual(log.status, "Queued")
		self.assertEqual(log.source_communication, attachment.attached_to_name)
		self.assertEqual(log.sender_email, "billing@vendor.example")
		self.assertEqual(log.email_subject, "Invoice INV-0042")
		self.enqueue.assert_called_once_with(log.name, 0)

	def test_does_nothing_while_disabled(self):
		attachment = self._scenario(enabled=False)

		handle_incoming_email_attachment(attachment)

		self.assertEqual(self._logs_for(attachment), [])
		self.enqueue.assert_not_called()

	def test_ignores_attachments_not_on_a_communication(self):
		attachment = self._scenario()
		attachment.attached_to_doctype = "ToDo"
		attachment.attached_to_name = "not-a-communication"

		handle_incoming_email_attachment(attachment)

		self.assertEqual(self._logs_for(attachment), [])
		self.enqueue.assert_not_called()

	def test_ignores_folders(self):
		attachment = self._scenario()
		attachment.is_folder = 1

		handle_incoming_email_attachment(attachment)

		self.assertEqual(self._logs_for(attachment), [])
		self.enqueue.assert_not_called()

	def test_ignores_sent_and_automated_communications(self):
		cases = [{"sent_or_received": "Sent"}, {"communication_type": "Automated Message"}]
		for overrides in cases:
			with self.subTest(overrides=overrides):
				attachment = self._scenario(**overrides)

				handle_incoming_email_attachment(attachment)

				self.assertEqual(self._logs_for(attachment), [])
		self.enqueue.assert_not_called()

	def test_ignores_communication_on_another_account(self):
		other_inbox = self._make_inbox()
		attachment = self._scenario(monitored=other_inbox)

		handle_incoming_email_attachment(attachment)

		self.assertEqual(self._logs_for(attachment), [])
		self.enqueue.assert_not_called()

	def test_blank_monitored_account_matches_nothing(self):
		# A Communication without an email account must not match a blank monitored account.
		self._configure_settings(enabled=False, account=None)
		comm = self._make_communication(None)
		attachment = self._make_attachment(comm, "invoice-42.pdf")
		self._configure_settings(enabled=True, account=None)

		handle_incoming_email_attachment(attachment)

		self.assertEqual(self._logs_for(attachment), [])
		self.enqueue.assert_not_called()

	def test_ignores_disallowed_extension(self):
		attachment = self._scenario(file_name="malware.exe")

		handle_incoming_email_attachment(attachment)

		self.assertEqual(self._logs_for(attachment), [])
		self.enqueue.assert_not_called()

	def test_extension_match_ignores_case(self):
		attachment = self._scenario(file_name="INVOICE-42.PDF")

		handle_incoming_email_attachment(attachment)

		self.assertEqual(len(self._logs_for(attachment)), 1)

	def test_is_candidate_file_checks_extension_only(self):
		settings = SimpleNamespace(allowed_extensions=" PDF, .png ,jpg")

		self.assertTrue(is_candidate_file(SimpleNamespace(file_name="scan.Pdf"), settings))
		self.assertTrue(is_candidate_file(SimpleNamespace(file_name="logo.PNG"), settings))
		self.assertFalse(is_candidate_file(SimpleNamespace(file_name="notes.txt"), settings))
		self.assertFalse(is_candidate_file(SimpleNamespace(file_name="report.pdf.exe"), settings))
		self.assertFalse(is_candidate_file(SimpleNamespace(file_name="pdf"), settings))
		self.assertFalse(is_candidate_file(SimpleNamespace(file_name=None), settings))
		self.assertFalse(
			is_candidate_file(SimpleNamespace(file_name="scan.pdf"), SimpleNamespace(allowed_extensions=""))
		)

	def test_does_not_duplicate_an_open_log(self):
		attachment = self._scenario()
		frappe.get_doc({"doctype": LOG, "status": "Needs Review", "source_file": attachment.name}).insert(
			ignore_permissions=True
		)

		handle_incoming_email_attachment(attachment)

		self.assertEqual([log.status for log in self._logs_for(attachment)], ["Needs Review"])
		self.enqueue.assert_not_called()

	def test_creates_new_log_when_only_failed_logs_exist(self):
		attachment = self._scenario()
		frappe.get_doc({"doctype": LOG, "status": "Failed", "source_file": attachment.name}).insert(
			ignore_permissions=True
		)

		handle_incoming_email_attachment(attachment)

		logs = self._logs_for(attachment)
		self.assertEqual(sorted(log.status for log in logs), ["Failed", "Queued"])
		queued = next(log for log in logs if log.status == "Queued")
		self.enqueue.assert_called_once_with(queued.name, 0)

	def test_truncates_subject_to_data_field_length(self):
		attachment = self._scenario(subject="Invoice " + "x" * 300)

		handle_incoming_email_attachment(attachment)

		log = self._logs_for(attachment)[0]
		self.assertEqual(len(log.email_subject), 140)

	def test_log_is_marked_failed_when_enqueue_fails(self):
		attachment = self._scenario()
		self.enqueue.side_effect = RuntimeError("queue unavailable")

		handle_incoming_email_attachment(attachment)

		logs = self._logs_for(attachment)
		self.assertEqual([log.status for log in logs], ["Failed"])
		self.assertEqual(logs[0].error_traceback, "Could not queue processing.")
		self.log_error.assert_called_once()
		self.assertEqual(self.log_error.call_args.kwargs["message"], f"File: {attachment.file_name}")
		self.assertNotIn("queue unavailable", str(self.log_error.call_args))

	def test_failure_logs_file_name_only(self):
		attachment = self._scenario()

		with patch(GET_SETTINGS, side_effect=RuntimeError("secret body text")):
			handle_incoming_email_attachment(attachment)

		self.log_error.assert_called_once()
		self.assertEqual(self.log_error.call_args.kwargs["message"], f"File: {attachment.file_name}")
		self.assertNotIn("secret", str(self.log_error.call_args))
		self.assertEqual(self._logs_for(attachment), [])

	def test_never_raises(self):
		attachment = self._scenario()
		self.log_error.side_effect = RuntimeError("error log unavailable")

		with patch(GET_SETTINGS, side_effect=RuntimeError("boom")):
			handle_incoming_email_attachment(attachment)
			handle_incoming_email_attachment(None)
