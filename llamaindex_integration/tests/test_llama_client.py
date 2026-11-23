# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for services/llama_client.py. _load_sdk is patched with fakes, so no network calls are made.

The test api key is the literal "test-key".
"""

import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from llamaindex_integration.services import llama_client
from llamaindex_integration.services.llama_client import (
	Classification,
	ExtractionResult,
	LlamaPermanentError,
	LlamaTransientError,
	Rule,
)

API_KEY = "test-key"
FILE_PATH = "invoice.pdf"
RULES = [Rule(doctype="ToDo", description="Follow-up requests")]
SCHEMA = {"type": "object", "properties": {"description": {"type": "string"}}}
DEFAULT_DATA = {"description": "Call the vendor"}

TRANSIENT_STATUSES = (408, 425, 429, 500, 502, 503, 504)
PERMANENT_STATUSES = (400, 401, 403, 404, 409, 413, 422)


class _FakeApiError(Exception):
	def __init__(self, status_code: int, body: object = None) -> None:
		super().__init__(f"status_code: {status_code}, body: {body}")
		self.status_code = status_code
		self.body = body


class _FakeHTTPStatusError(Exception):
	def __init__(self, status_code: int) -> None:
		super().__init__(f"HTTP {status_code}")
		self.response = SimpleNamespace(status_code=status_code)


class _FakeTransportError(Exception):
	"""Stands in for httpx.TransportError, which httpx.TimeoutException also subclasses."""


def _results(*results: object) -> SimpleNamespace:
	return SimpleNamespace(items=[SimpleNamespace(result=result) for result in results])


def _classification_result(
	doctype: str | None, confidence: float = 0.93, reasoning: str = "follow-up"
) -> object:
	return SimpleNamespace(type=doctype, confidence=confidence, reasoning=reasoning)


def _run(
	status: str = "SUCCESS", data: object = DEFAULT_DATA, raw_json: str = '{"id": "run-1"}'
) -> mock.Mock:
	run = mock.Mock()
	run.status = status
	run.data = data
	run.json.return_value = raw_json
	return run


def _sdk_importers(tree: ast.Module) -> set[str]:
	"""Names of the top-level statements or functions that import a llama_cloud package."""
	importers: set[str] = set()
	for node in tree.body:
		scope = node.name if isinstance(node, ast.FunctionDef) else "<module>"
		for sub in ast.walk(node):
			if isinstance(sub, ast.Import):
				names = [alias.name for alias in sub.names]
			elif isinstance(sub, ast.ImportFrom):
				names = [sub.module or ""]
			else:
				continue
			if any(name.startswith("llama_cloud") for name in names):
				importers.add(scope)
	return importers


class _SdkTestCase(unittest.TestCase):
	def setUp(self) -> None:
		patcher = mock.patch.object(llama_client, "_load_sdk")
		self.load_sdk = patcher.start()
		self.addCleanup(patcher.stop)

		self.classifier = mock.Mock()
		self.extractor = mock.Mock()
		self.sdk = llama_client._Sdk(
			classifier=self.classifier,
			new_extractor=mock.Mock(return_value=self.extractor),
			classifier_rule=mock.Mock(side_effect=lambda **kwargs: kwargs),
			extract_config=mock.sentinel.extract_config,
			api_error=_FakeApiError,
			transport_error=_FakeTransportError,
			http_status_error=_FakeHTTPStatusError,
		)
		self.load_sdk.return_value = self.sdk


class ClassifyTests(_SdkTestCase):
	def test_returns_doctype_and_scores(self) -> None:
		self.classifier.classify.return_value = _results(_classification_result("ToDo"))

		got = llama_client.classify(API_KEY, RULES, FILE_PATH)

		self.assertEqual(got, Classification(doctype="ToDo", confidence=0.93, reasoning="follow-up"))
		self.load_sdk.assert_called_once_with(API_KEY)

	def test_builds_rules_inside_module_and_passes_file(self) -> None:
		self.classifier.classify.return_value = _results(_classification_result("ToDo"))

		llama_client.classify(API_KEY, RULES, FILE_PATH)

		self.classifier.classify.assert_called_once_with(
			rules=[{"type": "ToDo", "description": "Follow-up requests"}],
			files=FILE_PATH,
			raise_on_error=True,
		)

	def test_none_item_result_maps_to_no_doctype(self) -> None:
		self.classifier.classify.return_value = _results(None)

		got = llama_client.classify(API_KEY, RULES, FILE_PATH)

		self.assertEqual(got, Classification(doctype=None, confidence=0.0, reasoning=""))

	def test_empty_type_maps_to_no_doctype(self) -> None:
		self.classifier.classify.return_value = _results(_classification_result(None, 0.4, "unsure"))

		got = llama_client.classify(API_KEY, RULES, FILE_PATH)

		self.assertEqual(got, Classification(doctype=None, confidence=0.4, reasoning="unsure"))

	def test_no_item_is_permanent(self) -> None:
		self.classifier.classify.return_value = _results()

		with self.assertRaises(LlamaPermanentError):
			llama_client.classify(API_KEY, RULES, FILE_PATH)

	def test_job_error_value_error_is_permanent(self) -> None:
		self.classifier.classify.side_effect = ValueError("Error classifying files under job ID job-1")

		with self.assertRaises(LlamaPermanentError):
			llama_client.classify(API_KEY, RULES, FILE_PATH)

	def test_timeout_is_transient(self) -> None:
		self.classifier.classify.side_effect = TimeoutError("Job job-1 timed out")

		with self.assertRaises(LlamaTransientError):
			llama_client.classify(API_KEY, RULES, FILE_PATH)

	def test_network_errors_are_transient(self) -> None:
		for error in (ConnectionError("connection reset"), _FakeTransportError("read timed out")):
			with self.subTest(error=type(error).__name__):
				self.classifier.classify.side_effect = error

				with self.assertRaises(LlamaTransientError):
					llama_client.classify(API_KEY, RULES, FILE_PATH)

	def test_api_error_status_codes_map_to_transient_or_permanent(self) -> None:
		for status in TRANSIENT_STATUSES:
			with self.subTest(status=status):
				self.classifier.classify.side_effect = _FakeApiError(status)

				with self.assertRaises(LlamaTransientError):
					llama_client.classify(API_KEY, RULES, FILE_PATH)
		for status in PERMANENT_STATUSES:
			with self.subTest(status=status):
				self.classifier.classify.side_effect = _FakeApiError(status)

				with self.assertRaises(LlamaPermanentError):
					llama_client.classify(API_KEY, RULES, FILE_PATH)

	def test_httpx_status_error_maps_by_status(self) -> None:
		self.classifier.classify.side_effect = _FakeHTTPStatusError(503)
		with self.assertRaises(LlamaTransientError):
			llama_client.classify(API_KEY, RULES, FILE_PATH)

		self.classifier.classify.side_effect = _FakeHTTPStatusError(404)
		with self.assertRaises(LlamaPermanentError):
			llama_client.classify(API_KEY, RULES, FILE_PATH)

	def test_unmapped_exception_propagates_unchanged(self) -> None:
		self.classifier.classify.side_effect = RuntimeError("unrelated")

		with self.assertRaises(RuntimeError) as cm:
			llama_client.classify(API_KEY, RULES, FILE_PATH)

		self.assertNotIsInstance(cm.exception, (LlamaTransientError, LlamaPermanentError))

	def test_empty_key_is_permanent_without_touching_sdk(self) -> None:
		with self.assertRaises(LlamaPermanentError):
			llama_client.classify("", RULES, FILE_PATH)

		self.load_sdk.assert_not_called()

	def test_no_rules_is_permanent_without_touching_sdk(self) -> None:
		with self.assertRaises(LlamaPermanentError):
			llama_client.classify(API_KEY, [], FILE_PATH)

		self.load_sdk.assert_not_called()


class ExtractTests(_SdkTestCase):
	def test_stores_the_extracted_data_not_the_run_envelope(self) -> None:
		# The retry path reads raw_extracted_json back as the extracted data, so the envelope must not be stored.
		run = _run()
		self.extractor.extract.return_value = run

		got = llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

		self.assertEqual(got, ExtractionResult(data=DEFAULT_DATA, raw_json=json.dumps(DEFAULT_DATA)))
		run.json.assert_not_called()

	def test_passes_key_timeout_schema_and_config(self) -> None:
		self.extractor.extract.return_value = _run()

		llama_client.extract(API_KEY, SCHEMA, FILE_PATH, max_timeout_s=120)

		self.load_sdk.assert_called_once_with(API_KEY)
		self.sdk.new_extractor.assert_called_once_with(max_timeout=120)
		self.extractor.extract.assert_called_once_with(
			data_schema=SCHEMA,
			config=mock.sentinel.extract_config,
			files=FILE_PATH,
		)

	def test_default_timeout_is_900_seconds(self) -> None:
		self.extractor.extract.return_value = _run()

		llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

		self.sdk.new_extractor.assert_called_once_with(max_timeout=900)

	def test_error_state_is_permanent(self) -> None:
		self.extractor.extract.return_value = _run(status="ERROR", data=None)

		with self.assertRaises(LlamaPermanentError):
			llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

	def test_no_run_is_permanent(self) -> None:
		self.extractor.extract.return_value = None

		with self.assertRaises(LlamaPermanentError):
			llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

	def test_run_without_data_is_permanent(self) -> None:
		self.extractor.extract.return_value = _run(status="SUCCESS", data=None)

		with self.assertRaises(LlamaPermanentError):
			llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

	def test_poll_timeout_is_transient(self) -> None:
		self.extractor.extract.side_effect = Exception("Timeout while extracting the file: job-1")

		with self.assertRaises(LlamaTransientError):
			llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

	def test_no_run_runtime_error_is_permanent(self) -> None:
		self.extractor.extract.side_effect = RuntimeError("Failed to get extraction result for job job-1")

		with self.assertRaises(LlamaPermanentError):
			llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

	def test_poll_timeout_match_requires_bare_exception(self) -> None:
		self.extractor.extract.side_effect = RuntimeError("Timeout while extracting the file: job-1")

		with self.assertRaises(RuntimeError) as cm:
			llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

		self.assertNotIsInstance(cm.exception, LlamaTransientError)

	def test_value_error_is_permanent(self) -> None:
		self.extractor.extract.side_effect = ValueError("Unsupported file type")

		with self.assertRaises(LlamaPermanentError):
			llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

	def test_network_errors_are_transient(self) -> None:
		for error in (TimeoutError("timed out"), ConnectionError("network"), _FakeTransportError("read")):
			with self.subTest(error=type(error).__name__):
				self.extractor.extract.side_effect = error

				with self.assertRaises(LlamaTransientError):
					llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

	def test_api_error_status_codes_map_to_transient_or_permanent(self) -> None:
		for status in TRANSIENT_STATUSES:
			with self.subTest(status=status):
				self.extractor.extract.side_effect = _FakeApiError(status)

				with self.assertRaises(LlamaTransientError):
					llama_client.extract(API_KEY, SCHEMA, FILE_PATH)
		for status in PERMANENT_STATUSES:
			with self.subTest(status=status):
				self.extractor.extract.side_effect = _FakeApiError(status)

				with self.assertRaises(LlamaPermanentError):
					llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

	def test_empty_key_is_permanent_without_touching_sdk(self) -> None:
		with self.assertRaises(LlamaPermanentError):
			llama_client.extract("", SCHEMA, FILE_PATH)

		self.load_sdk.assert_not_called()


class SecretHandlingTests(_SdkTestCase):
	def test_api_key_never_appears_in_raised_messages(self) -> None:
		# Several fake errors carry the key in their messages or bodies, the worst case the SDK could produce.
		leaky_errors = [
			_FakeApiError(401, body="token test-key rejected"),
			_FakeApiError(503, body="test-key"),
			_FakeHTTPStatusError(500),
			_FakeTransportError("GET https://example.invalid?key=test-key failed"),
			TimeoutError("test-key timed out"),
			ValueError("bad test-key"),
		]
		for error in leaky_errors:
			for operation, fail in (("classify", self._fail_classify), ("extract", self._fail_extract)):
				with (
					self.subTest(operation=operation, error=type(error).__name__),
					self.assertRaises((LlamaTransientError, LlamaPermanentError)) as cm,
				):
					fail(error)
				self._assert_key_free(cm.exception)

	def test_extract_poll_timeout_message_is_key_free(self) -> None:
		self.extractor.extract.side_effect = Exception("Timeout while extracting the file: test-key")

		with self.assertRaises(LlamaTransientError) as cm:
			llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

		self._assert_key_free(cm.exception)

	def test_empty_key_message_is_key_free(self) -> None:
		with self.assertRaises(LlamaPermanentError) as cm:
			llama_client.extract("", SCHEMA, FILE_PATH)

		self._assert_key_free(cm.exception)

	def _fail_classify(self, error: BaseException) -> None:
		self.classifier.classify.side_effect = error
		llama_client.classify(API_KEY, RULES, FILE_PATH)

	def _fail_extract(self, error: BaseException) -> None:
		self.extractor.extract.side_effect = error
		llama_client.extract(API_KEY, SCHEMA, FILE_PATH)

	def _assert_key_free(self, exc: BaseException) -> None:
		self.assertNotIn(API_KEY, str(exc))
		self.assertNotIn(API_KEY, repr(exc))
		self.assertFalse(any(API_KEY in str(arg) for arg in exc.args))


class SeamTests(unittest.TestCase):
	def test_sdk_is_imported_only_inside_load_sdk(self) -> None:
		source = Path(llama_client.__file__).read_text(encoding="utf-8")

		self.assertEqual(_sdk_importers(ast.parse(source)), {"_load_sdk"})
