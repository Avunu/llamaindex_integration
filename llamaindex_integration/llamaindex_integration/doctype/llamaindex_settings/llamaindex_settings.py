# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

import re
from typing import cast

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class LlamaIndexSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from llamaindex_integration.llamaindex_integration.doctype.llama_doctype_rule.llama_doctype_rule import (
			LlamaDocTypeRule,
		)
		from llamaindex_integration.llamaindex_integration.doctype.llama_entity_match.llama_entity_match import (
			LlamaEntityMatch,
		)

		allowed_extensions: DF.Data
		doctype_rules: DF.Table[LlamaDocTypeRule]
		enable_packet_splitting: DF.Check
		enabled: DF.Check
		entity_matches: DF.Table[LlamaEntityMatch]
		fuzzy_match_threshold: DF.Int
		llama_cloud_api_key: DF.Password
		min_classification_confidence: DF.Float
		monitored_email_account: DF.Link
	# end: auto-generated types

	def validate(self):
		self._validate_ranges()
		self.allowed_extensions = _normalize_extensions(self.allowed_extensions)
		# Frappe does not run validate() on child rows, so each rule and entity match is checked here, on every save.
		for rule in self.doctype_rules:
			rule.validate_target_and_keys()
		self._validate_entity_matches()
		if self.enabled:
			self._validate_ready_to_enable()

	def _validate_ranges(self):
		if not 0 <= cint(self.fuzzy_match_threshold) <= 100:
			frappe.throw(_("Fuzzy Match Threshold must be between 0 and 100."), exc=frappe.ValidationError)
		if not 0 <= flt(self.min_classification_confidence) <= 1:
			frappe.throw(
				_("Minimum Classification Confidence must be between 0 and 1."),
				exc=frappe.ValidationError,
			)

	def _validate_entity_matches(self):
		# The resolver takes the single row for a DocType, so a second row for the same DocType is refused.
		seen: set[str] = set()
		for match in self.entity_matches:
			match.validate_entity_match()
			if match.link_doctype in seen:
				frappe.throw(
					_("{0} has more than one Entity Match row. Keep one row for each DocType.").format(
						match.link_doctype
					),
					exc=frappe.ValidationError,
				)
			seen.add(match.link_doctype)

	def _validate_ready_to_enable(self):
		# The key is read only to test that it is present. Its value never goes into a message or a log.
		if not self.get_password("llama_cloud_api_key", raise_exception=False):
			frappe.throw(
				_("Enter the LlamaCloud API Key before enabling LlamaIndex Settings."),
				exc=frappe.ValidationError,
			)
		if not self.doctype_rules:
			frappe.throw(
				_("Add at least one Document Rule before enabling LlamaIndex Settings."),
				exc=frappe.ValidationError,
			)


def get_settings() -> LlamaIndexSettings:
	"""Return the single LlamaIndex Settings document."""
	return cast(LlamaIndexSettings, frappe.get_single("LlamaIndex Settings"))


def _normalize_extensions(raw: str | None) -> str:
	"""Return the allowed extensions as lowercase, comma-separated values with no dots or spaces.

	A leading dot is removed, so ".PDF" becomes "pdf". Any other dot or space inside an extension is
	rejected, and so is a list with no extensions.
	"""
	extensions: list[str] = []
	for part in (raw or "").split(","):
		extension = part.strip().lstrip(".").strip().lower()
		if not extension:
			continue
		if re.search(r"[.\s]", extension):
			frappe.throw(
				_(
					"Allowed Extensions must be comma-separated file extensions with no dots or spaces, for example pdf,png."
				),
				exc=frappe.ValidationError,
			)
		if extension not in extensions:
			extensions.append(extension)
	if not extensions:
		frappe.throw(
			_("Allowed Extensions must list at least one file extension."), exc=frappe.ValidationError
		)
	return ",".join(extensions)
