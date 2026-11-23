# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""The only module that talks to LlamaCloud (spec section 6, step 3).

_load_sdk is the only place that imports llama_cloud_services or llama_cloud, and those imports stay lazy.
Public functions return the app-owned dataclasses below, so SDK types never leave this module. Extraction
uses ExtractConfig(extraction_target=PER_DOC, extraction_mode=MULTIMODAL) and serializes the result
with .json(), not model_dump(), because the SDK models use pydantic.v1.
"""

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

# Statuses that are worth one automatic retry. Other 4xx responses are permanent.
_RETRYABLE_STATUSES = frozenset({408, 425, 429})

# extract.py raises a bare Exception with this prefix when polling runs past max_timeout (extract.py:159).
_EXTRACT_POLL_TIMEOUT_PREFIX = "Timeout while extracting the file"
# extract.py raises a bare RuntimeError with this prefix when aextract gets no run back (extract.py:996).
_EXTRACT_NO_RUN_PREFIX = "Failed to get extraction result"


@dataclass(frozen=True)
class Rule:
	doctype: str
	description: str


@dataclass(frozen=True)
class Classification:
	doctype: str | None
	confidence: float
	reasoning: str


@dataclass(frozen=True)
class ExtractionResult:
	data: dict[str, Any]
	raw_json: str


class LlamaTransientError(Exception):
	"""Retryable: timeouts, httpx transport errors, ApiError 408, 425, 429 and 5xx, and extract poll timeouts."""


class LlamaPermanentError(Exception):
	"""Not retryable: other 4xx, a classify job in ERROR, an extract run in ERROR, or no run returned."""


@dataclass(frozen=True)
class _Sdk:
	"""The SDK handles this module uses. Only _load_sdk builds one, and it never leaves this module."""

	classifier: Any
	new_extractor: Callable[..., Any]
	classifier_rule: Any
	extract_config: Any
	api_error: type[Exception]
	transport_error: type[Exception]
	http_status_error: type[Exception]


def classify(api_key: str, rules: Sequence[Rule], file_path: str) -> Classification:
	"""Classify one file against rules. Raises LlamaTransientError or LlamaPermanentError.

	A None result for the file maps to doctype None. The caller turns that into Needs Review.
	"""
	_require_key(api_key)
	if not rules:
		raise LlamaPermanentError("classify needs at least one rule")
	sdk = _load_sdk(api_key)
	with _translate_errors(sdk, "classify"):
		try:
			sdk_rules = [
				sdk.classifier_rule(type=rule.doctype, description=rule.description) for rule in rules
			]
			# raise_on_error=True makes the SDK raise ValueError when the classify job ends in ERROR.
			results = sdk.classifier.classify(rules=sdk_rules, files=file_path, raise_on_error=True)
		except ValueError:
			raise LlamaPermanentError("classify job failed or returned an invalid response") from None

	items = list(results.items)
	if len(items) != 1:
		raise LlamaPermanentError("classify returned no single result for the file")
	result = items[0].result
	if result is None:
		return Classification(doctype=None, confidence=0.0, reasoning="")
	return Classification(
		doctype=result.type or None,
		confidence=float(result.confidence),
		reasoning=result.reasoning or "",
	)


def extract(
	api_key: str, schema: dict[str, Any], file_path: str, *, max_timeout_s: int = 900
) -> ExtractionResult:
	"""Extract data from one file against a JSON Schema. Raises LlamaTransientError or LlamaPermanentError."""
	_require_key(api_key)
	sdk = _load_sdk(api_key)
	with _translate_errors(sdk, "extract"):
		try:
			run = sdk.new_extractor(max_timeout=max_timeout_s).extract(
				data_schema=schema, config=sdk.extract_config, files=file_path
			)
		except ValueError:
			raise LlamaPermanentError("extract rejected the input or returned an invalid response") from None
		except Exception as exc:
			mapped = _extract_signal(exc)
			if mapped is None:
				raise
			raise mapped from None

	if run is None:
		raise LlamaPermanentError("extract returned no run")
	if run.status == "ERROR":
		raise LlamaPermanentError("extract run ended in ERROR")
	if not isinstance(run.data, dict):
		raise LlamaPermanentError("extract run has no result data")
	# Store the extracted data, not the ExtractRun envelope. The retry path reads raw_extracted_json back as data.
	return ExtractionResult(data=run.data, raw_json=json.dumps(run.data))


def _extract_signal(exc: Exception) -> LlamaTransientError | LlamaPermanentError | None:
	"""Map the two extract failures the SDK raises as bare Exception or RuntimeError, matched by type and message."""
	if type(exc) is Exception and str(exc).startswith(_EXTRACT_POLL_TIMEOUT_PREFIX):
		return LlamaTransientError("extract polling timed out")
	if type(exc) is RuntimeError and str(exc).startswith(_EXTRACT_NO_RUN_PREFIX):
		return LlamaPermanentError("extract returned no run")
	return None


def _require_key(api_key: str) -> None:
	# An empty key would make the SDK fall back to LLAMA_CLOUD_API_KEY from the environment.
	if not api_key:
		raise LlamaPermanentError("LlamaCloud API key is not configured")


@contextmanager
def _translate_errors(sdk: _Sdk, operation: str) -> Iterator[None]:
	"""Map SDK and httpx failures to the app errors. Anything else passes through unchanged.

	The SDK exception is not chained (from None). Its message can carry the response body, and tasks.py
	stores the formatted exception chain in error_traceback.
	"""
	try:
		yield
	except (TimeoutError, ConnectionError, sdk.transport_error) as exc:
		raise LlamaTransientError(f"{operation} hit a network error ({type(exc).__name__})") from None
	except (sdk.api_error, sdk.http_status_error) as exc:
		raise _status_error(_status_of(exc), operation) from None


def _status_of(exc: BaseException) -> int | None:
	"""HTTP status from an SDK ApiError (status_code) or an httpx.HTTPStatusError (response.status_code)."""
	status = getattr(exc, "status_code", None)
	if status is None:
		status = getattr(getattr(exc, "response", None), "status_code", None)
	return status


def _status_error(status: int | None, operation: str) -> LlamaTransientError | LlamaPermanentError:
	# The message carries the status and operation only. The response body and the SDK message are left out.
	message = f"{operation} failed with HTTP status {status}"
	if status is not None and (status in _RETRYABLE_STATUSES or status >= 500):
		return LlamaTransientError(message)
	return LlamaPermanentError(message)


def _load_sdk(api_key: str) -> Any:
	"""The only function that imports llama_cloud_services. Keep the import inside this body."""
	import httpx
	from llama_cloud import ClassifierRule, ExtractConfig, ExtractMode, ExtractTarget
	from llama_cloud.core import ApiError
	from llama_cloud_services.beta.classifier import LlamaClassify
	from llama_cloud_services.extract import LlamaExtract

	return _Sdk(
		classifier=LlamaClassify.from_api_key(api_key),
		new_extractor=partial(LlamaExtract, api_key=api_key),
		classifier_rule=ClassifierRule,
		extract_config=ExtractConfig(
			extraction_target=ExtractTarget.PER_DOC,
			extraction_mode=ExtractMode.MULTIMODAL,
		),
		api_error=ApiError,
		transport_error=httpx.TransportError,
		http_status_error=httpx.HTTPStatusError,
	)
