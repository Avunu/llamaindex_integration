# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Background pipeline for one Llama Document Log (spec section 6, steps 2 to 5).

Only this module opens, rolls back and releases the savepoint named llama_doc_creation. It wraps
resolve_entities, create_draft_document and attach_source_file. The classification and extraction
checkpoints are committed before the savepoint opens. Service functions never open, release or commit
savepoints.

Stage 5 holds two redis locks from before the savepoint opens until after the final commit: one per source
file content_hash, then one per target doctype. Every job takes them in that order, so none waits for a file
lock while it holds a doctype lock. A stage-5 failure is recorded as Failed before the locks are released.
The locks are what make the duplicate checks hold across jobs. Service functions never take them.
"""

import json
import os
import re
import time
import traceback
from collections.abc import Mapping
from contextlib import ExitStack, suppress
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from llamaindex_integration.llamaindex_integration.doctype.llamaindex_settings.llamaindex_settings import (
	get_settings,
)
from llamaindex_integration.services import (
	document_creator,
	entity_resolver,
	llama_client,
	locks,
	schema_builder,
)
from llamaindex_integration.services.entity_resolver import Resolution

LOG_DOCTYPE = "Llama Document Log"
JOB_PATH = "llamaindex_integration.tasks.process_document_log"
JOB_TIMEOUT_S = 1500
EXTRACT_TIMEOUT_S = 900
SAVEPOINT = "llama_doc_creation"
CREATE_LOCK_PREFIX = "llama-create:"
FILE_LOCK_PREFIX = "llama-file:"
# The job is killed JOB_TIMEOUT_S after it starts, and stage 5 takes its locks after that start. So a lock key
# set with this TTL outlives the job that holds it, and it cannot expire while its holder is still running.
LOCK_TTL_S = JOB_TIMEOUT_S
# Logs in these states can hold the document that a new log for the same file must not duplicate.
FILE_DUPLICATE_STATUSES = ("Completed", "Needs Review")
MAX_AUTO_RETRIES = 1
TRACEBACK_LIMIT = 8 * 1024
INLINE_IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif")
INLINE_IMAGE_MAX_BYTES = 15 * 1024
# A live job is killed at JOB_TIMEOUT_S, so a log that has not changed for twice that is not being worked on.
STALE_AFTER_S = 2 * JOB_TIMEOUT_S
IN_PROGRESS_STATUSES = ("Queued", "Classifying", "Extracting", "Resolving Entities")
STALE_MESSAGE = "Processing stopped before it finished. Retry Ingestion to run it again."

DISABLED_MESSAGE = "LlamaIndex Settings is disabled. Enable it, then retry."
NO_API_KEY_MESSAGE = "The LlamaCloud API key is not set in LlamaIndex Settings."
NO_RULES_MESSAGE = "No Llama DocType Rule is configured in LlamaIndex Settings."

_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_SECRET_PATTERN = re.compile(r"(?i)(bearer\s+|api[_-]?key[\"']?\s*[:=]\s*[\"']?)\S+")
_INLINE_PAYLOAD_PATTERN = re.compile(r"[{\[]\s*[\"']")
_JSON_MEMBER_PATTERN = re.compile(r"^\s*[\"'][^\"']*[\"']\s*:")
# Keys that only the ExtractRun envelope carries. Logs written before run.data was stored hold the envelope.
_ENVELOPE_KEY = "data_schema"


class _FailureRecorded(Exception):
	"""Raised after a stage-5 failure was recorded and committed under the create lock.

	process_document_log treats it as handled. The failure is not recorded again, and nothing is re-raised.
	"""


def enqueue_processing(log_name: str, attempt: int = 0) -> str:
	"""Enqueue process_document_log on the long queue and return the job id.

	job_id is "llama-log-<log_name>-a<attempt>" with deduplicate=True. The attempt number keeps the
	id distinct, so a retry is not blocked by a STARTED job with the same id. The job receives the attempt
	and does nothing if the log has moved on to a newer one. Job timeout is 1500 s. The job is enqueued
	after the current transaction commits.
	"""
	job_id = _job_id(log_name, attempt)
	frappe.enqueue(
		JOB_PATH,
		queue="long",
		timeout=JOB_TIMEOUT_S,
		job_id=job_id,
		deduplicate=True,
		enqueue_after_commit=True,
		log_name=log_name,
		attempt=attempt,
	)
	return job_id


def _job_id(log_name: str, attempt: int) -> str:
	return f"llama-log-{log_name}-a{attempt}"


def process_document_log(log_name: str, attempt: int | None = None) -> None:
	"""Run the pipeline for one log and write a terminal status.

	attempt is the retry_count the job was enqueued for. A job whose attempt is no longer the log's
	retry_count returns without doing anything. None skips that check, for jobs queued without an attempt.
	Catches everything, logs the exception class and the log name only, and never re-raises, so frappe's
	execute_job does not re-run the job. Stages that already have output are skipped, so a retry does not
	bill LlamaCloud twice.
	"""
	started = time.monotonic()
	try:
		_run(log_name, attempt)
	except (llama_client.LlamaTransientError, locks.LockTimeoutError) as exc:
		_record_failure(log_name, exc, retry=True, attempt=attempt)
	except _FailureRecorded:
		# Stage 5 already recorded and committed the failure while it held the lock.
		pass
	except Exception as exc:
		_record_failure(log_name, exc, retry=False, attempt=attempt)
	_log(log_name, "finished", elapsed=f"{time.monotonic() - started:.1f}s")


def _run(log_name: str, attempt: int | None) -> None:
	"""Stages 1 to 5. Each checkpoint is committed before the next stage starts."""
	settings = get_settings()
	log = frappe.get_doc(LOG_DOCTYPE, log_name)
	if attempt is not None and (log.retry_count or 0) != attempt:
		_log(log_name, "guard", outcome="skipped", reason="superseded by a newer attempt")
		return
	if log.created_docname:
		_log(log_name, "guard", outcome="skipped", reason="document already created")
		return
	if not settings.enabled:
		_checkpoint(log, "Failed", error=DISABLED_MESSAGE)
		return
	log.error_traceback = None
	if _is_inline_image(log):
		_checkpoint(log, "Ignored")
		return
	api_key = settings.get_password("llama_cloud_api_key", raise_exception=False)
	if not api_key:
		_checkpoint(log, "Failed", error=NO_API_KEY_MESSAGE)
		return

	if not _classification_is_reusable(log, settings):
		rules = _classification_rules(settings)
		if not rules:
			_checkpoint(log, "Failed", error=NO_RULES_MESSAGE)
			return
		_checkpoint(log, "Classifying")
		previous_doctype = log.detected_doctype
		result = llama_client.classify(api_key, rules, _source_path(log))
		if result.doctype != previous_doctype:
			# Extraction was made for the previous doctype's schema, so it cannot be reused.
			log.raw_extracted_json = None
		log.detected_doctype = result.doctype
		log.classification_confidence = result.confidence
		log.classification_reasoning = result.reasoning
		if result.doctype is None or result.confidence < settings.min_classification_confidence:
			_checkpoint(log, "Needs Review")
			return
		# Commit the paid classification now, so a failure later in this run does not bill it again on retry.
		_checkpoint(log, "Classifying")

	doctype = log.detected_doctype
	rule = _rule_for(settings, doctype)
	if rule is None:
		_checkpoint(log, "Failed", error=f"No Llama DocType Rule for {doctype} in LlamaIndex Settings.")
		return
	included_fields = schema_builder.parse_included_fields(rule.included_fields)
	reconciliation_field = rule.reconciliation_field or None
	# The properties the target requested. The match keys are added to the schema after this, so they are not in it.
	requested = schema_builder.target_properties(
		doctype, included_fields, reconciliation_field=reconciliation_field
	)
	schema = schema_builder.build_json_schema_for_doctype(
		doctype,
		included_fields,
		reconciliation_field=reconciliation_field,
		match_keys=entity_resolver.match_keys(doctype, settings),
	)
	if log.raw_extracted_json:
		data = _stored_extraction(log.raw_extracted_json)
	else:
		_checkpoint(log, "Extracting")
		result = llama_client.extract(api_key, schema, _source_path(log), max_timeout_s=EXTRACT_TIMEOUT_S)
		log.raw_extracted_json = result.raw_json
		# Commit the paid extraction before any later step can fail, so a retry does not bill it again.
		_checkpoint(log, "Extracting")
		data = result.data
	data = _schema_values(data, schema)
	# Before the checkpoint, because no DB read may happen between that commit and the create lock.
	_fill_content_hash(log)
	_checkpoint(log, "Resolving Entities")
	_create_document(log, doctype, rule, data, settings, attempt, requested)


def _create_document(
	log: Any,
	doctype: str,
	rule: Any,
	data: dict[str, Any],
	settings: Any,
	attempt: int | None,
	requested: dict[str, Any],
) -> None:
	"""Stage 5 under the file lock and the create lock for the target doctype.

	The locks are taken before the savepoint opens and released after the final commit, on every path. A job
	that waits for them, or times out waiting, never opens a transaction, and LockTimeoutError is retried by
	process_document_log. Two jobs for one file, or two jobs for one doctype, therefore run one after the other,
	and the second one's duplicate checks see the first one's committed document.

	A failure in the savepoint is recorded as Failed while the lock is still held, so the next job for this
	doctype cannot start before that status is committed. Then _FailureRecorded tells process_document_log
	that the failure is handled. The lock itself is not in the try, so a LockTimeoutError still propagates.
	"""
	duplicate_key_fields = document_creator.parse_duplicate_key_fields(rule.duplicate_key_fields)
	default_values = document_creator.parse_default_values(rule.default_values)
	reconciliation_field = rule.reconciliation_field or None
	with ExitStack() as held:
		# The file lock comes first, so jobs for one file run one at a time whatever their doctype. Every job
		# takes the file lock before any doctype lock, so no job waits for a file lock while holding a doctype lock.
		if log.content_hash:
			held.enter_context(locks.create_lock(FILE_LOCK_PREFIX + log.content_hash, ttl_s=LOCK_TTL_S))
		held.enter_context(locks.create_lock(CREATE_LOCK_PREFIX + doctype, ttl_s=LOCK_TTL_S))
		try:
			_create_in_savepoint(
				log,
				doctype,
				data,
				settings,
				requested=requested,
				duplicate_key_fields=duplicate_key_fields,
				default_values=default_values,
				reconciliation_field=reconciliation_field,
			)
		except Exception as exc:
			_record_failure(log.name, exc, retry=False, attempt=attempt)
			raise _FailureRecorded() from exc


def _create_in_savepoint(
	log: Any,
	doctype: str,
	data: dict[str, Any],
	settings: Any,
	*,
	requested: dict[str, Any],
	duplicate_key_fields: tuple[str, ...],
	default_values: dict[str, Any],
	reconciliation_field: str | None,
) -> None:
	"""Check the file, resolve, insert the Draft, and attach the source File inside one savepoint.

	The file-level duplicate check is the first step inside the savepoint. It raises before anything is
	written, so it rolls back with the rest. The resolver reads the full extracted data, match keys included.
	Only the values the target requested reach the draft, so a match key is read and never written.

	A duplicate, a totals mismatch, a MandatoryError (for example a required link that could not be matched and was
	left blank), or a linked record that was written and then failed rolls back and goes to Needs Review, so a person
	can fix the data and retry. Any other error rolls back and re-raises. _create_document then records it as Failed,
	under the locks.
	"""
	resolution: Resolution | None = None
	frappe.db.savepoint(SAVEPOINT)
	try:
		_raise_if_file_duplicate(log)
		resolution = entity_resolver.resolve_entities(doctype, data, settings, sender_email=log.sender_email)
		if resolution.write_failed:
			raise entity_resolver.LinkedWriteError(
				_("A linked record was written and then failed, so the draft was not kept.")
			)
		resolution.values = _requested_only(resolution.values, requested)
		resolution.child_rows = _requested_only(resolution.child_rows, requested)
		docname = document_creator.create_draft_document(
			doctype,
			_requested_only(data, requested),
			resolution,
			duplicate_key_fields=duplicate_key_fields,
			default_values=default_values,
			reconciliation_field=reconciliation_field,
		)
		document_creator.attach_source_file(log.source_file, doctype, docname)
	except (
		document_creator.DuplicateDocumentError,
		document_creator.TotalsMismatchError,
		entity_resolver.LinkedWriteError,
		frappe.MandatoryError,
	) as exc:
		# A full rollback, not a savepoint rollback. A savepoint rollback keeps the after-commit jobs that the
		# rolled-back inserts queued, and the checkpoint below would run them.
		frappe.db.rollback()
		log.resolution_notes = _review_notes(resolution, exc)
		_checkpoint(log, "Needs Review")
		return
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		raise
	else:
		frappe.db.release_savepoint(SAVEPOINT)
		log.created_doctype = doctype
		log.created_docname = docname
		log.resolution_notes = "\n".join(resolution.notes)
		_checkpoint(log, "Needs Review" if resolution.needs_review else "Completed")


def _fill_content_hash(log: Any) -> None:
	"""Copy the source File's content_hash onto the log when the log has none.

	The file-level duplicate check compares this hash between logs. It reads the File, so it runs before the
	Resolving Entities checkpoint commits, and never between that commit and the create lock.
	"""
	if log.content_hash:
		return
	log.content_hash = frappe.db.get_value("File", log.source_file, "content_hash") or None


def _raise_if_file_duplicate(log: Any) -> None:
	"""Raise DuplicateDocumentError when another log already created a live document from this file.

	The other log must share the content_hash, be Completed or Needs Review, and name a created document that
	still exists and is not cancelled. It runs under the create lock, so a job that finished first is visible.
	"""
	if not log.content_hash:
		return
	earlier = frappe.get_all(
		LOG_DOCTYPE,
		filters={
			"content_hash": log.content_hash,
			"name": ["!=", log.name],
			"status": ["in", FILE_DUPLICATE_STATUSES],
			"created_docname": ["is", "set"],
		},
		fields=["name", "created_doctype", "created_docname"],
		order_by="creation asc",
	)
	for row in earlier:
		if row.created_doctype and _is_live_document(row.created_doctype, row.created_docname):
			raise document_creator.DuplicateDocumentError(
				_("This file was already processed by {0}, which created {1} {2}.").format(
					row.name, row.created_doctype, row.created_docname
				)
			)


def _is_live_document(doctype: str, name: str) -> bool:
	"""True when the document exists and is not cancelled. Only submittable doctypes can be cancelled."""
	if frappe.get_meta(doctype).is_submittable:
		docstatus = frappe.db.get_value(doctype, name, "docstatus")
		return docstatus is not None and docstatus != 2
	return frappe.db.exists(doctype, name) is not None


def _review_notes(resolution: Resolution | None, exc: Exception) -> str:
	"""Notes for an attempt that was rolled back.

	Records created inside the savepoint no longer exist after the rollback, so the note names them as
	rolled back. The resolver's own notes are kept, because they explain the review.
	"""
	notes = list(resolution.notes) if resolution else []
	if resolution and resolution.created:
		created = ", ".join(f"{doctype} {name}" for doctype, name in resolution.created)
		notes.append(f"Rolled back, so these records were not kept: {created}.")
	notes.append(str(exc))
	return "\n".join(notes)


def _record_failure(log_name: str, exc: Exception, *, retry: bool, attempt: int | None = None) -> None:
	"""Roll back uncommitted work and record Failed with a sanitized traceback.

	A LlamaTransientError or LockTimeoutError gets one automatic retry while retry_count is 0. The new job is enqueued
	after commit, so it starts only once the Failed state and the new retry_count are stored. A job whose
	attempt is no longer current, and a log whose document already exists, keep the state they have.
	Stage 5 calls it with retry=False, while it still holds the create lock. Never raises.
	"""
	error_text = _sanitize_traceback("".join(traceback.format_exception(exc)), _configured_secrets())
	try:
		frappe.db.rollback()
		log = frappe.get_doc(LOG_DOCTYPE, log_name)
		if (attempt is not None and (log.retry_count or 0) != attempt) or log.created_docname:
			_log(log_name, "failed", error=type(exc).__name__, outcome="stored state kept")
			return
		can_retry = retry and (log.retry_count or 0) < MAX_AUTO_RETRIES
		log.set_status("Failed", error=error_text)
		if can_retry:
			log.retry_count = (log.retry_count or 0) + 1
		log.save(ignore_permissions=True)
		if can_retry:
			enqueue_processing(log_name, attempt=log.retry_count)
		frappe.db.commit()
		_log(log_name, "failed", error=type(exc).__name__, retried=can_retry)
	except Exception as save_exc:
		# The save failed, so nothing from it may stay open when the caller releases the create lock.
		with suppress(Exception):
			# A dead connection has no open transaction left to discard.
			frappe.db.rollback()
		_log(log_name, "failed", error=type(exc).__name__, record_error=type(save_exc).__name__)


def fail_stale_logs() -> int:
	"""Scheduled sweep (every 15 minutes, see hooks.py). Return the number of logs it failed.

	A non-terminal log that has not changed for STALE_AFTER_S is set to Failed. That makes it retryable,
	and it no longer blocks a new log for its File. This covers a worker that was killed mid-job, and a
	Queued log whose job was never enqueued. A log whose job is still queued or started in RQ is left alone,
	because a long queue backlog is not a failure.
	"""
	cutoff = add_to_date(now_datetime(), seconds=-STALE_AFTER_S)
	candidates = frappe.get_all(
		LOG_DOCTYPE,
		filters={"status": ["in", IN_PROGRESS_STATUSES], "modified": ["<", cutoff]},
		fields=["name", "retry_count"],
	)
	failed = 0
	for row in candidates:
		if is_job_enqueued(_job_id(row.name, row.retry_count or 0)):
			continue
		try:
			failed += _fail_if_still_stale(row.name, cutoff)
		except Exception as exc:
			frappe.db.rollback()
			_log(row.name, "sweep", outcome="skipped", error=type(exc).__name__)
	return failed


def _fail_if_still_stale(log_name: str, cutoff: Any) -> int:
	"""Lock the log, check it is still stale, and fail it. Return 1 when it was failed, otherwise 0."""
	log = frappe.get_doc(LOG_DOCTYPE, log_name, for_update=True)
	if log.status not in IN_PROGRESS_STATUSES or get_datetime(log.modified) >= cutoff:
		frappe.db.rollback()
		return 0
	log.set_status("Failed", error=STALE_MESSAGE)
	log.save(ignore_permissions=True)
	frappe.db.commit()
	_log(log_name, "failed", error="StaleLog")
	return 1


def _checkpoint(log: Any, status: str, *, error: str | None = None) -> None:
	"""Store the status (and error text, when given), then commit."""
	log.set_status(status, error=error)
	log.save(ignore_permissions=True)
	frappe.db.commit()
	_log(log.name, "checkpoint", status=status)


def _is_inline_image(log: Any) -> bool:
	"""True for a small image (png, jpg, jpeg or gif under 15 KB), or when the source File's URL appears in the final Communication content.

	Only the file_url is matched in the content. The file name is not, because a name such as INV-0042.pdf
	can appear in the body of an ordinary invoice email. Content-Id images are rewritten into the
	Communication after the File hook runs, so only the job can see them.
	"""
	source = frappe.db.get_value(
		"File", log.source_file, ["file_name", "file_url", "file_size"], as_dict=True
	)
	if not source:
		return False
	if _is_small_image(source.file_name, source.file_size):
		return True
	if not log.source_communication or not source.file_url:
		return False
	content = frappe.db.get_value("Communication", log.source_communication, "content")
	if not content:
		return False
	return source.file_url in content


def _is_small_image(file_name: str | None, file_size: int | None) -> bool:
	if file_size is None:
		return False
	extension = os.path.splitext(file_name or "")[1].lower().lstrip(".")
	return extension in INLINE_IMAGE_EXTENSIONS and file_size < INLINE_IMAGE_MAX_BYTES


def _classification_is_reusable(log: Any, settings: Any) -> bool:
	"""A stored classification is reused only when it names a doctype and meets the minimum confidence.

	A low-confidence result stays stored for audit, but the next run classifies again.
	"""
	if not log.detected_doctype or log.classification_confidence is None:
		return False
	return log.classification_confidence >= settings.min_classification_confidence


def _classification_rules(settings: Any) -> list[llama_client.Rule]:
	return [
		llama_client.Rule(doctype=row.target_doctype, description=row.classification_description)
		for row in settings.doctype_rules
	]


def _rule_for(settings: Any, doctype: str) -> Any | None:
	return next((row for row in settings.doctype_rules if row.target_doctype == doctype), None)


def _source_path(log: Any) -> str:
	return frappe.get_doc("File", log.source_file).get_full_path()


def _stored_extraction(raw: str) -> dict[str, Any]:
	"""Return the extracted data stored in raw_extracted_json.

	Logs written before run.data was stored hold the whole ExtractRun envelope. Unwrap it, so a retry of
	such a log extracts the same data as a fresh run does.
	"""
	parsed = json.loads(raw)
	if isinstance(parsed, dict) and _ENVELOPE_KEY in parsed and isinstance(parsed.get("data"), dict):
		return parsed["data"]
	return parsed


def _schema_values(data: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
	"""Keep only the top-level keys that the schema asked for. Any other key is dropped before resolution."""
	allowed = schema.get("properties", {})
	return {key: value for key, value in data.items() if key in allowed}


def _requested_only(values: Mapping[str, Any], requested: Mapping[str, Any]) -> dict[str, Any]:
	"""Keep the header values the target requested. A Table keeps only the columns that its rows requested.

	requested is schema_builder.target_properties, which has no match keys. A value that only a match key asked for is
	therefore dropped, whether it comes from the extraction or from the resolver's output.
	"""
	kept: dict[str, Any] = {}
	for key, value in values.items():
		schema = requested.get(key)
		if schema is None:
			continue
		if schema.get("type") == "array" and isinstance(value, list):
			columns = schema["items"]["properties"]
			value = [
				{k: v for k, v in row.items() if k in columns} if isinstance(row, dict) else row
				for row in value
			]
		kept[key] = value
	return kept


def _configured_secrets() -> tuple[str, ...]:
	"""The configured LlamaCloud key, so that it can be removed from stored text. Empty if unavailable."""
	try:
		key = get_settings().get_password("llama_cloud_api_key", raise_exception=False)
	except Exception:
		return ()
	return (key,) if key else ()


def _sanitize_traceback(text: str, secrets: tuple[str, ...] = ()) -> str:
	"""Strip the given secrets, URLs, secrets written as bearer tokens or api_key pairs, and request payloads,
	then keep the last 8 KB of what remains.

	The tail is kept on purpose. The raising frame and the exception line are the last lines of a traceback,
	so they survive the cut. A payload that starts mid-line is cut at its start, so the exception class and
	message survive. Pretty-printed JSON member lines are replaced whole.
	"""
	for secret in secrets:
		text = text.replace(secret, "<redacted>")
	text = _URL_PATTERN.sub("<url removed>", text)
	text = _SECRET_PATTERN.sub(r"\1<redacted>", text)
	lines = [_strip_payload(line) for line in text.splitlines()]
	text = "\n".join(lines)
	if len(text) > TRACEBACK_LIMIT:
		marker = "[truncated]\n"
		text = marker + text[-(TRACEBACK_LIMIT - len(marker)) :]
	return text


def _strip_payload(line: str) -> str:
	if _JSON_MEMBER_PATTERN.search(line):
		return "<payload removed>"
	match = _INLINE_PAYLOAD_PATTERN.search(line)
	if match:
		return line[: match.start()] + "<payload removed>"
	return line


def _log(log_name: str, stage: str, **fields: object) -> None:
	"""Write one structured line. It carries the log name and metadata only, never payloads."""
	details = "".join(f" {key}={value}" for key, value in fields.items())
	frappe.logger("llamaindex_integration").info(f"log={log_name} stage={stage}{details}")
