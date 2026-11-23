# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for tasks.py: stage checkpoints, the llama_doc_creation savepoint rollback, the single
automatic retry for LlamaTransientError and LockTimeoutError, the file-level duplicate check, the
llama-create lock (taken before the savepoint and released after the final commit), and the rule that
process_document_log never re-raises.

Settings come from a stand-in object patched into tasks.get_settings. The LlamaCloud, schema,
resolver and creator functions are patched at their module paths. Log and File rows are real. The lock is
the real create_lock, which runs on frappe.cache, unless a test patches it.
"""

import json
import secrets
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.core.doctype.file.utils import get_content_hash
from frappe.tests import IntegrationTestCase

from llamaindex_integration import tasks
from llamaindex_integration.services import document_creator, entity_resolver, llama_client, locks
from llamaindex_integration.services.entity_resolver import Resolution
from llamaindex_integration.services.locks import LockTimeoutError
from llamaindex_integration.tests import MINIMAL_PDF

LOG = "Llama Document Log"
SAMPLE_DATA = {"description": "Follow up with the vendor"}
SAVEPOINT = "llama_doc_creation"
LOCK_NAME = "llama-create:ToDo"

SETTINGS_PATH = "llamaindex_integration.tasks.get_settings"
ENQUEUE_PATH = "llamaindex_integration.tasks.enqueue_processing"
LOCK_PATH = "llamaindex_integration.services.locks.create_lock"
CLASSIFY_PATH = "llamaindex_integration.services.llama_client.classify"
EXTRACT_PATH = "llamaindex_integration.services.llama_client.extract"
BUILD_SCHEMA_PATH = "llamaindex_integration.services.schema_builder.build_json_schema_for_doctype"
PARSE_FIELDS_PATH = "llamaindex_integration.services.schema_builder.parse_included_fields"
RESOLVE_PATH = "llamaindex_integration.services.entity_resolver.resolve_entities"
CREATE_PATH = "llamaindex_integration.services.document_creator.create_draft_document"
ATTACH_PATH = "llamaindex_integration.services.document_creator.attach_source_file"

# The real lock. _record_lock_and_commits wraps it, so the lock still works while the spy records it.
REAL_CREATE_LOCK = locks.create_lock
# The real resolver and creator, for the tests that check what reaches the draft.
REAL_RESOLVE = entity_resolver.resolve_entities
REAL_CREATE = document_creator.create_draft_document


def _settings(**overrides):
	rule = SimpleNamespace(
		target_doctype="ToDo",
		classification_description="Follow-up request",
		included_fields="description",
		duplicate_key_fields="",
		default_values="",
		reconciliation_field="",
	)
	values = {
		"enabled": 1,
		"min_classification_confidence": 0.8,
		"doctype_rules": [rule],
		"get_password": lambda fieldname, raise_exception=True: "test-key",
	}
	values.update(overrides)
	return SimpleNamespace(**values)


def _schema(properties):
	"""A stand-in for build_json_schema_for_doctype. Only the property names matter to the pipeline."""
	return {"type": "object", "properties": {name: {"type": "string"} for name in properties}}


def _classification(doctype, confidence):
	return llama_client.Classification(doctype=doctype, confidence=confidence, reasoning="Vendor wording")


def _extraction():
	return llama_client.ExtractionResult(data=SAMPLE_DATA, raw_json=json.dumps(SAMPLE_DATA))


def _resolution(*, needs_review=False):
	return Resolution(
		values={}, child_rows={}, notes=["matched contact"], needs_review=needs_review, created=[]
	)


def _unique_pdf() -> bytes:
	"""A valid PDF whose bytes are unique to this call.

	Committed logs keep their content_hash. Two logs with the same bytes would hit the file-level duplicate
	check, so every log in these tests gets its own bytes unless a test sets the bytes on purpose.
	"""
	return MINIMAL_PDF + f"% llama-test {frappe.generate_hash(length=16)}\n".encode()


@contextmanager
def _record_lock_and_commits(events: list, log_name: str):
	"""Record the lock, the savepoint opens and every commit, in the order they happen.

	The lock still runs for real. Each commit records the log's status at that moment, so a test can tell
	which commit stored the final status. The lock release is recorded after the real release runs.
	"""
	real_commit = frappe.db.commit
	real_savepoint = frappe.db.savepoint

	@contextmanager
	def lock(name, *args, **kwargs):
		events.append(("acquire", name))
		try:
			with REAL_CREATE_LOCK(name, *args, **kwargs):
				yield
		finally:
			events.append(("release", name))

	def commit(*args, **kwargs):
		events.append(("commit", frappe.db.get_value(LOG, log_name, "status")))
		return real_commit(*args, **kwargs)

	def savepoint(save_point):
		events.append(("savepoint", save_point))
		return real_savepoint(save_point)

	with (
		patch(LOCK_PATH, side_effect=lock),
		patch.object(frappe.db, "commit", side_effect=commit),
		patch.object(frappe.db, "savepoint", side_effect=savepoint),
	):
		yield


class _PipelineTestCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.settings = _settings()
		self.get_settings = self.enterContext(patch(SETTINGS_PATH, return_value=self.settings))
		self.classify = self.enterContext(patch(CLASSIFY_PATH, return_value=_classification("ToDo", 0.95)))
		self.extract = self.enterContext(patch(EXTRACT_PATH, return_value=_extraction()))
		self.build_schema = self.enterContext(patch(BUILD_SCHEMA_PATH, return_value=_schema(["description"])))
		self.parse_fields = self.enterContext(patch(PARSE_FIELDS_PATH, return_value=["description"]))
		self.resolve = self.enterContext(patch(RESOLVE_PATH, return_value=_resolution()))
		self.target = frappe.get_doc({"doctype": "ToDo", "description": "Llama pipeline test target"})
		self.target.insert(ignore_permissions=True)
		self.create = self.enterContext(patch(CREATE_PATH, return_value=self.target.name))
		self.attach = self.enterContext(patch(ATTACH_PATH, return_value="FILE-COPY"))
		frappe.db.commit()

	def _make_log(self, *, file_name=None, content=None, **fields):
		source = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name or f"llama-test-{frappe.generate_hash(length=8)}.pdf",
				"content": content or _unique_pdf(),
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		log = frappe.get_doc(
			{"doctype": LOG, "status": "Queued", "source_file": source.name, **fields}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return log

	def _process(self, log_name):
		tasks.process_document_log(log_name)
		return frappe.get_doc(LOG, log_name)

	def _todo(self, description):
		return frappe.get_doc({"doctype": "ToDo", "description": description}).insert(ignore_permissions=True)

	def _earlier_log(self, content: bytes, created_docname: str, status: str = "Completed"):
		"""A finished log for the given bytes. It stores the content_hash that the job would have copied."""
		return self._make_log(
			content=content,
			status=status,
			created_doctype="ToDo",
			created_docname=created_docname,
			content_hash=get_content_hash(content),
		)


class TestEnqueueProcessing(IntegrationTestCase):
	def test_enqueues_on_long_queue_with_attempt_job_id(self):
		with patch("frappe.enqueue") as enqueue:
			job_id = tasks.enqueue_processing("LLAMA-2026-00001", attempt=2)

		self.assertEqual(job_id, "llama-log-LLAMA-2026-00001-a2")
		enqueue.assert_called_once_with(
			"llamaindex_integration.tasks.process_document_log",
			queue="long",
			timeout=1500,
			job_id="llama-log-LLAMA-2026-00001-a2",
			deduplicate=True,
			enqueue_after_commit=True,
			log_name="LLAMA-2026-00001",
			attempt=2,
		)


class TestProcessDocumentLog(_PipelineTestCase):
	def test_success_creates_draft_and_completes(self):
		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Completed")
		self.assertEqual(log.detected_doctype, "ToDo")
		self.assertEqual(log.created_doctype, "ToDo")
		self.assertEqual(log.created_docname, self.target.name)
		self.assertEqual(log.resolution_notes, "matched contact")
		self.assertEqual(json.loads(log.raw_extracted_json), SAMPLE_DATA)
		self.assertEqual(self.classify.call_args.args[0], "test-key")
		self.assertEqual(
			self.classify.call_args.args[1],
			[llama_client.Rule(doctype="ToDo", description="Follow-up request")],
		)
		self.build_schema.assert_called_once_with(
			"ToDo", ["description"], reconciliation_field=None, match_keys={}
		)
		self.assertEqual(self.extract.call_args.kwargs["max_timeout_s"], 900)
		self.attach.assert_called_once_with(log.source_file, "ToDo", self.target.name)

	def test_success_copies_the_source_content_hash_onto_the_log(self):
		log = self._make_log()
		source_hash = frappe.db.get_value("File", log.source_file, "content_hash")
		self.assertTrue(source_hash)
		self.assertFalse(log.content_hash)

		log = self._process(log.name)

		self.assertEqual(log.content_hash, source_hash)

	def test_duplicate_key_fields_from_the_rule_are_parsed_and_passed_to_the_creator(self):
		self.settings.doctype_rules[0].duplicate_key_fields = " description, status, "

		self._process(self._make_log().name)

		self.assertEqual(self.create.call_args.kwargs["duplicate_key_fields"], ("description", "status"))

	def test_blank_duplicate_key_fields_pass_an_empty_key(self):
		self._process(self._make_log().name)

		self.assertEqual(self.create.call_args.kwargs["duplicate_key_fields"], ())

	def test_default_values_and_reconciliation_field_from_the_rule_reach_the_schema_and_creator(self):
		self.settings.doctype_rules[0].default_values = '{"priority": "High"}'
		self.settings.doctype_rules[0].reconciliation_field = "amount"

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Completed")
		self.build_schema.assert_called_once_with(
			"ToDo", ["description"], reconciliation_field="amount", match_keys={}
		)
		self.assertEqual(self.create.call_args.kwargs["default_values"], {"priority": "High"})
		self.assertEqual(self.create.call_args.kwargs["reconciliation_field"], "amount")

	def test_blank_default_values_and_reconciliation_field_pass_empty_values(self):
		self._process(self._make_log().name)

		self.assertEqual(self.create.call_args.kwargs["default_values"], {})
		self.assertIsNone(self.create.call_args.kwargs["reconciliation_field"])
		self.assertIsNone(self.build_schema.call_args.kwargs["reconciliation_field"])

	def test_invalid_default_values_fail_before_the_create_lock(self):
		# The rule validation refuses this on save. If it were stored anyway, the log must fail without a document.
		self.settings.doctype_rules[0].default_values = "[1, 2]"
		name = self._make_log().name

		with patch(LOCK_PATH, side_effect=REAL_CREATE_LOCK) as create_lock:
			log = self._process(name)

		self.assertEqual(log.status, "Failed")
		self.assertIn("ValidationError", log.error_traceback)
		self.create.assert_not_called()
		create_lock.assert_not_called()

	def test_needs_review_flag_still_creates_the_document(self):
		self.resolve.return_value = _resolution(needs_review=True)

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Needs Review")
		self.assertEqual(log.created_docname, self.target.name)

	def test_low_confidence_stores_classification_and_needs_review(self):
		self.classify.return_value = _classification("ToDo", 0.4)

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Needs Review")
		self.assertEqual(log.detected_doctype, "ToDo")
		self.assertEqual(log.classification_confidence, 0.4)
		self.assertEqual(log.classification_reasoning, "Vendor wording")
		self.extract.assert_not_called()
		self.assertFalse(log.created_docname)

	def test_missing_doctype_needs_review(self):
		self.classify.return_value = _classification(None, 0.9)

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Needs Review")
		self.assertFalse(log.detected_doctype)
		self.extract.assert_not_called()

	def test_classification_skipped_when_doctype_already_set(self):
		log = self._process(self._make_log(detected_doctype="ToDo", classification_confidence=0.9).name)

		self.classify.assert_not_called()
		self.assertEqual(log.status, "Completed")

	def test_low_confidence_classification_is_repeated_on_retry(self):
		name = self._make_log(
			detected_doctype="ToDo", classification_confidence=0.4, classification_reasoning="Old guess"
		).name

		log = self._process(name)

		self.classify.assert_called_once()
		self.assertEqual(log.status, "Completed")
		self.assertEqual(log.classification_confidence, 0.95)
		self.assertEqual(log.classification_reasoning, "Vendor wording")

	def test_classification_at_exactly_the_minimum_confidence_is_reused(self):
		log = self._process(self._make_log(detected_doctype="ToDo", classification_confidence=0.8).name)

		self.classify.assert_not_called()
		self.assertEqual(log.status, "Completed")

	def test_reclassified_doctype_discards_extraction_made_for_the_old_doctype(self):
		name = self._make_log(
			detected_doctype="Note",
			classification_confidence=0.4,
			raw_extracted_json=json.dumps({"title": "Made for Note"}),
		).name

		log = self._process(name)

		self.classify.assert_called_once()
		self.extract.assert_called_once()
		self.assertEqual(self.resolve.call_args.args[1], SAMPLE_DATA)
		self.assertEqual(log.detected_doctype, "ToDo")

	def test_extraction_skipped_when_raw_json_already_stored(self):
		stored = {"description": "Stored earlier"}
		log = self._process(
			self._make_log(
				detected_doctype="ToDo",
				classification_confidence=0.9,
				raw_extracted_json=json.dumps(stored),
			).name
		)

		self.classify.assert_not_called()
		self.extract.assert_not_called()
		self.assertEqual(self.resolve.call_args.args[1], stored)
		self.assertEqual(log.status, "Completed")

	def test_sender_email_is_passed_to_the_resolver(self):
		name = self._make_log(sender_email="billing@vendor.example").name

		self._process(name)

		self.assertEqual(self.resolve.call_args.kwargs["sender_email"], "billing@vendor.example")

	def test_classification_is_kept_when_a_later_step_fails(self):
		# The paid classification is committed before the schema is built, so a retry does not bill it again.
		name = self._make_log().name

		with patch(BUILD_SCHEMA_PATH, side_effect=frappe.ValidationError("schema build failed")):
			self.assertEqual(self._process(name).status, "Failed")
		self.assertEqual(self._process(name).status, "Completed")

		self.classify.assert_called_once()

	def test_extraction_is_kept_when_a_later_step_fails(self):
		# The paid extraction is committed right after it returns, so a retry does not extract the file again.
		name = self._make_log().name

		with patch.object(tasks, "_fill_content_hash", side_effect=frappe.ValidationError("boom")):
			failed = self._process(name)
		self.assertEqual(failed.status, "Failed")
		self.assertTrue(failed.raw_extracted_json)

		self.assertEqual(self._process(name).status, "Completed")
		self.extract.assert_called_once()

	def test_needs_review_drops_after_commit_work_queued_in_the_savepoint(self):
		# A savepoint rollback keeps the after-commit jobs that the rolled-back inserts queued. The Needs Review
		# checkpoint commits, so such a job would run against a document that no longer exists.
		ran = []

		def create_and_queue(*args, **kwargs):
			frappe.db.after_commit.add(lambda: ran.append("queued job"))
			raise frappe.MandatoryError("Missing Values Required: Description")

		self.create.side_effect = create_and_queue
		name = self._make_log().name

		self.assertEqual(self._process(name).status, "Needs Review")
		self.assertEqual(ran, [])

	def test_small_image_is_ignored_and_larger_image_is_processed(self):
		small = self._make_log(
			file_name=f"image-{frappe.generate_hash(length=8)}.png",
			content=secrets.token_bytes(15 * 1024 - 1),
		)
		larger = self._make_log(
			file_name=f"scan-{frappe.generate_hash(length=8)}.png", content=secrets.token_bytes(15 * 1024)
		)

		self.assertEqual(self._process(small.name).status, "Ignored")
		self.classify.assert_not_called()

		self.assertEqual(self._process(larger.name).status, "Completed")
		self.classify.assert_called_once()

	def test_inline_image_is_ignored(self):
		log = self._make_log()
		file_url = frappe.db.get_value("File", log.source_file, "file_url")
		comm = frappe.get_doc(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"communication_medium": "Email",
				"sent_or_received": "Received",
				"subject": "Inline image test",
				"content": f'<p>Logo</p><img src="{file_url}">',
			}
		).insert(ignore_permissions=True)
		log.source_communication = comm.name
		log.save(ignore_permissions=True)
		frappe.db.commit()

		log = self._process(log.name)

		self.assertEqual(log.status, "Ignored")
		self.classify.assert_not_called()

	def test_disabled_settings_fail_with_clear_message(self):
		self.settings.enabled = 0

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Failed")
		self.assertIn("disabled", log.error_traceback)
		self.classify.assert_not_called()

	def test_transient_error_schedules_one_retry(self):
		self.classify.side_effect = llama_client.LlamaTransientError("timeout")
		name = self._make_log().name

		with patch(ENQUEUE_PATH) as enqueue:
			log = self._process(name)

		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.retry_count, 1)
		enqueue.assert_called_once_with(name, attempt=1)

	def test_transient_error_after_the_retry_fails_without_enqueue(self):
		self.classify.side_effect = llama_client.LlamaTransientError("timeout")
		name = self._make_log(retry_count=1).name

		with patch(ENQUEUE_PATH) as enqueue:
			log = self._process(name)

		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.retry_count, 1)
		enqueue.assert_not_called()

	def test_permanent_error_fails_without_retry(self):
		self.classify.side_effect = llama_client.LlamaPermanentError("bad request")
		name = self._make_log().name

		with patch(ENQUEUE_PATH) as enqueue:
			log = self._process(name)

		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.retry_count, 0)
		self.assertIn("LlamaPermanentError", log.error_traceback)
		enqueue.assert_not_called()

	def test_unexpected_exception_is_recorded_and_not_reraised(self):
		self.classify.side_effect = RuntimeError("boom")

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Failed")
		self.assertIn("RuntimeError", log.error_traceback)

	def test_error_traceback_drops_urls_and_request_payloads(self):
		self.classify.side_effect = RuntimeError(
			'call https://api.example.com/v1/classify failed with {"supplier": "Secret Vendor"}'
		)

		log = self._process(self._make_log().name)

		self.assertNotIn("https://", log.error_traceback)
		self.assertNotIn("Secret Vendor", log.error_traceback)
		self.assertIn("RuntimeError", log.error_traceback)

	def test_error_traceback_is_truncated_to_8_kb(self):
		self.classify.side_effect = RuntimeError("x" * 20_000)

		log = self._process(self._make_log().name)

		self.assertLessEqual(len(log.error_traceback), 8 * 1024)
		self.assertTrue(log.error_traceback.startswith("[truncated]"))

	def test_savepoint_rolls_back_when_the_creator_fails(self):
		self.create.side_effect = frappe.ValidationError("Description is required")
		name = self._make_log().name

		with patch.object(frappe.db, "rollback", wraps=frappe.db.rollback) as rollback:
			log = self._process(name)

		rollback.assert_any_call(save_point=SAVEPOINT)
		self.assertEqual(log.status, "Failed")
		self.assertFalse(log.created_docname)
		self.assertIn("ValidationError", log.error_traceback)

	def test_duplicate_and_totals_mismatch_go_to_needs_review(self):
		errors = [
			document_creator.DuplicateDocumentError("ToDo TODO-0001 already exists for description, status"),
			document_creator.TotalsMismatchError("Extracted total 100 differs from computed 120"),
		]
		for error in errors:
			with self.subTest(error=type(error).__name__):
				self.create.side_effect = error
				log = self._process(self._make_log().name)

				self.assertEqual(log.status, "Needs Review")
				self.assertFalse(log.created_docname)
				self.assertFalse(log.error_traceback)
				self.assertIn(str(error), log.resolution_notes)

	def test_existing_created_document_blocks_reprocessing(self):
		name = self._make_log(
			status="Needs Review", created_doctype="ToDo", created_docname=self.target.name
		).name

		log = self._process(name)

		self.classify.assert_not_called()
		self.create.assert_not_called()
		self.assertEqual(log.status, "Needs Review")

	def test_superseded_attempt_does_nothing(self):
		# A Retry raised retry_count after this job was queued, so the job is no longer the current attempt.
		name = self._make_log(retry_count=1).name

		tasks.process_document_log(name, attempt=0)

		self.classify.assert_not_called()
		self.assertEqual(frappe.db.get_value(LOG, name, "status"), "Queued")

	def test_current_attempt_runs(self):
		name = self._make_log(retry_count=1).name

		tasks.process_document_log(name, attempt=1)

		self.assertEqual(frappe.get_doc(LOG, name).status, "Completed")

	def test_stored_envelope_from_an_older_run_is_unwrapped_on_retry(self):
		# Logs written before run.data was stored hold the whole ExtractRun envelope.
		envelope = {"id": "run-1", "status": "SUCCESS", "data_schema": {}, "data": SAMPLE_DATA}
		name = self._make_log(
			status="Needs Review",
			detected_doctype="ToDo",
			classification_confidence=0.9,
			raw_extracted_json=json.dumps(envelope),
		).name

		log = self._process(name)

		self.extract.assert_not_called()
		self.assertEqual(self.resolve.call_args.args[1], SAMPLE_DATA)
		self.assertEqual(log.status, "Completed")

	def test_keys_outside_the_schema_are_dropped_before_resolution(self):
		self.extract.return_value = llama_client.ExtractionResult(
			data={"description": "Vendor invoice", "is_return": 1}, raw_json="{}"
		)

		self._process(self._make_log().name)

		self.assertEqual(self.resolve.call_args.args[1], {"description": "Vendor invoice"})

	def test_mandatory_error_goes_to_needs_review_with_the_reason(self):
		# An unmatched link is flagged by the resolver, and insert then fails on a reqd field left blank.
		self.resolve.return_value = _resolution(needs_review=True)
		self.create.side_effect = frappe.MandatoryError("Missing Values Required: Description")

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Needs Review")
		self.assertFalse(log.created_docname)
		self.assertIn("Missing Values Required: Description", log.resolution_notes)
		self.attach.assert_not_called()

	def test_linked_record_written_before_a_failure_rolls_the_whole_draft_back(self):
		# The resolver wrote a linked record and then failed, so the draft is not created and the log goes to review.
		self.resolve.return_value = Resolution(
			notes=["Role: could not create Role from 'Ops' (ValidationError). Left blank."],
			needs_review=True,
			write_failed=True,
		)

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Needs Review")
		self.assertFalse(log.created_docname)
		self.assertIn("A linked record was written and then failed", log.resolution_notes)
		self.create.assert_not_called()
		self.attach.assert_not_called()

	def test_rolled_back_creations_are_named_in_the_notes(self):
		self.resolve.return_value = Resolution(
			notes=["Created Note NOTE-0001 from 'Acme'."], created=[("Note", "NOTE-0001")]
		)
		self.create.side_effect = document_creator.DuplicateDocumentError(
			"ToDo TODO-0001 already exists for description, status"
		)

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Needs Review")
		self.assertIn("Rolled back, so these records were not kept: Note NOTE-0001.", log.resolution_notes)
		self.assertIn("TODO-0001 already exists", log.resolution_notes)

	def test_file_name_in_the_email_body_does_not_ignore_the_invoice(self):
		log = self._make_log(file_name="invoice-42.pdf")
		comm = frappe.get_doc(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"communication_medium": "Email",
				"sent_or_received": "Received",
				"subject": "Invoice",
				"content": "Please find attached invoice-42.pdf.",
			}
		).insert(ignore_permissions=True)
		log.source_communication = comm.name
		log.save(ignore_permissions=True)
		frappe.db.commit()

		log = self._process(log.name)

		self.assertEqual(log.status, "Completed")
		self.classify.assert_called_once()

	def test_configured_key_is_redacted_from_the_stored_traceback(self):
		self.classify.side_effect = RuntimeError("upstream rejected the request, key test-key")

		log = self._process(self._make_log().name)

		self.assertEqual(log.status, "Failed")
		self.assertNotIn("test-key", log.error_traceback)
		self.assertIn("<redacted>", log.error_traceback)


class TestMatchKeysAreNotWritten(_PipelineTestCase):
	"""A match key is read by the resolver and never written. These tests run the real resolver and creator."""

	def _process_with_real_write_path(self, *, included, extracted):
		self.parse_fields.return_value = included
		self.build_schema.return_value = _schema(["description", "priority"])
		self.extract.return_value = llama_client.ExtractionResult(data=extracted, raw_json="{}")
		with (
			patch(RESOLVE_PATH, side_effect=REAL_RESOLVE),
			patch(CREATE_PATH, side_effect=REAL_CREATE),
		):
			return self._process(self._make_log().name)

	def test_match_key_named_like_an_unrequested_writable_field_is_not_written(self):
		# priority is writable on ToDo and not requested, so here it is only a match key. The resolver reads it, and the
		# draft keeps the default that Frappe gives priority.
		log = self._process_with_real_write_path(
			included=["description"],
			extracted={"description": "Call the supplier", "priority": "High"},
		)

		self.assertEqual(log.status, "Completed")
		created = frappe.get_doc("ToDo", log.created_docname)
		self.assertEqual(created.description, "Call the supplier")
		self.assertEqual(created.priority, "Medium")

	def test_match_key_that_is_requested_is_still_written(self):
		log = self._process_with_real_write_path(
			included=["description", "priority"],
			extracted={"description": "Call the supplier", "priority": "High"},
		)

		self.assertEqual(frappe.db.get_value("ToDo", log.created_docname, "priority"), "High")


class TestFileLevelDuplicate(_PipelineTestCase):
	"""The same File bytes must not produce a second document while an earlier log still holds one."""

	def test_earlier_completed_document_for_the_same_file_sends_the_log_to_review(self):
		content = _unique_pdf()
		earlier_doc = self._todo("Earlier copy")
		self._earlier_log(content, earlier_doc.name)
		name = self._make_log(content=content).name

		log = self._process(name)

		self.assertEqual(log.status, "Needs Review")
		self.assertFalse(log.created_docname)
		self.assertIn(earlier_doc.name, log.resolution_notes)
		self.resolve.assert_not_called()
		self.create.assert_not_called()
		self.attach.assert_not_called()

	def test_earlier_needs_review_document_for_the_same_file_also_blocks(self):
		content = _unique_pdf()
		earlier_doc = self._todo("Earlier copy under review")
		self._earlier_log(content, earlier_doc.name, status="Needs Review")
		name = self._make_log(content=content).name

		log = self._process(name)

		self.assertEqual(log.status, "Needs Review")
		self.assertIn(earlier_doc.name, log.resolution_notes)
		self.create.assert_not_called()

	def test_deleted_earlier_document_does_not_block(self):
		content = _unique_pdf()
		earlier_doc = self._todo("Deleted copy")
		self._earlier_log(content, earlier_doc.name)
		frappe.delete_doc("ToDo", earlier_doc.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		name = self._make_log(content=content).name

		log = self._process(name)

		self.assertEqual(log.status, "Completed")
		self.create.assert_called_once()

	def test_different_bytes_with_an_earlier_completed_document_are_not_a_duplicate(self):
		earlier_doc = self._todo("Other file")
		self._earlier_log(_unique_pdf(), earlier_doc.name)
		name = self._make_log().name

		log = self._process(name)

		self.assertEqual(log.status, "Completed")
		self.create.assert_called_once()


class TestLockTimeout(_PipelineTestCase):
	def test_lock_timeout_schedules_one_automatic_retry(self):
		name = self._make_log().name

		with (
			patch(LOCK_PATH, side_effect=LockTimeoutError("timed out waiting for llama-create")),
			patch(ENQUEUE_PATH) as enqueue,
		):
			log = self._process(name)

		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.retry_count, 1)
		enqueue.assert_called_once_with(name, attempt=1)
		self.resolve.assert_not_called()
		self.create.assert_not_called()

	def test_lock_timeout_after_the_retry_fails_without_enqueue(self):
		name = self._make_log(retry_count=1).name

		with (
			patch(LOCK_PATH, side_effect=LockTimeoutError("timed out waiting for llama-create")),
			patch(ENQUEUE_PATH) as enqueue,
		):
			log = self._process(name)

		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.retry_count, 1)
		enqueue.assert_not_called()


class TestLockAroundCreation(_PipelineTestCase):
	"""The llama-create lock is taken after the Resolving Entities checkpoint and before the savepoint.

	It is released once, after the commit that stores the final status, on success and on failure.
	"""

	def _assert_released_after_the_commit(self, events, status, file_lock):
		# The file lock is taken first, then the doctype lock. Both are taken after the Resolving Entities commit,
		# and both are released after the commit that stores the final status. The doctype lock is released first.
		acquired = events.index(("acquire", LOCK_NAME))
		self.assertLess(
			events.index(("commit", "Resolving Entities")), events.index(("acquire", file_lock)), events
		)
		self.assertLess(events.index(("acquire", file_lock)), acquired, events)
		self.assertEqual(events.count(("release", LOCK_NAME)), 1, events)
		self.assertEqual(events.count(("release", file_lock)), 1, events)
		self.assertLess(acquired, events.index(("commit", status)), events)
		self.assertLess(events.index(("commit", status)), events.index(("release", LOCK_NAME)), events)
		self.assertLess(events.index(("release", LOCK_NAME)), events.index(("release", file_lock)), events)

	def test_lock_is_held_from_before_the_savepoint_until_after_the_final_commit_on_success(self):
		name = self._make_log().name
		events: list = []

		with _record_lock_and_commits(events, name):
			log = self._process(name)

		self.assertEqual(log.status, "Completed")
		file_lock = tasks.FILE_LOCK_PREFIX + log.content_hash
		self._assert_released_after_the_commit(events, "Completed", file_lock)
		self.assertLess(events.index(("acquire", LOCK_NAME)), events.index(("savepoint", SAVEPOINT)), events)

	def test_stage_five_locks_outlive_the_job_timeout(self):
		# A lock key that expires while its job is still running would let a second job in. The job is killed
		# JOB_TIMEOUT_S after it starts, so both keys must last at least that long.
		name = self._make_log().name

		with patch(LOCK_PATH, side_effect=REAL_CREATE_LOCK) as create_lock:
			log = self._process(name)

		ttl_by_name = {call.args[0]: call.kwargs["ttl_s"] for call in create_lock.call_args_list}
		self.assertEqual(
			set(ttl_by_name), {tasks.FILE_LOCK_PREFIX + log.content_hash, LOCK_NAME}, ttl_by_name
		)
		for lock_name, ttl in ttl_by_name.items():
			self.assertGreaterEqual(ttl, tasks.JOB_TIMEOUT_S, lock_name)

	def test_lock_is_released_after_the_failure_is_committed(self):
		self.create.side_effect = frappe.ValidationError("Description is required")
		name = self._make_log().name
		events: list = []

		with _record_lock_and_commits(events, name):
			log = self._process(name)

		self.assertEqual(log.status, "Failed")
		file_lock = tasks.FILE_LOCK_PREFIX + log.content_hash
		self._assert_released_after_the_commit(events, "Failed", file_lock)
		# The Resolving Entities commit is made in _run, before the locks. The Failed commit is made inside them.
		self.assertEqual(
			events[events.index(("commit", "Resolving Entities")) :],
			[
				("commit", "Resolving Entities"),
				("acquire", file_lock),
				("acquire", LOCK_NAME),
				("savepoint", SAVEPOINT),
				("commit", "Failed"),
				("release", LOCK_NAME),
				("release", file_lock),
			],
			events,
		)

	def test_lock_is_released_after_the_review_status_is_committed_for_a_file_duplicate(self):
		content = _unique_pdf()
		earlier_doc = self._todo("Earlier copy")
		self._earlier_log(content, earlier_doc.name)
		name = self._make_log(content=content).name
		events: list = []

		with _record_lock_and_commits(events, name):
			log = self._process(name)

		self.assertEqual(log.status, "Needs Review")
		self._assert_released_after_the_commit(
			events, "Needs Review", tasks.FILE_LOCK_PREFIX + log.content_hash
		)
