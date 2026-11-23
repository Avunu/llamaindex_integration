# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""How the entity resolver matches one linked DocType. Each row lives in LlamaIndex Settings, one row per DocType.

Frappe does not run validate() on child rows, so LlamaIndex Settings calls validate_entity_match() for each row.
"""

import json
from typing import Any

import frappe
from frappe import _
from frappe.model import table_fields
from frappe.model.document import Document

from llamaindex_integration.services.entity_resolver import parse_fieldnames

# Long text is never compared as a title, exactly or fuzzily.
TEXT_TITLE_FIELDTYPES = ("Text Editor", "Text", "Long Text")


class LlamaEntityMatch(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		auto_create: DF.Check
		default_values: DF.Code
		field_map: DF.Code
		fuzzy: DF.Check
		identifier_fields: DF.SmallText
		link_doctype: DF.Link
		match_sender_contact: DF.Check
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
	# end: auto-generated types

	def validate(self):
		self.validate_entity_match()

	def validate_entity_match(self):
		"""Check the linked DocType, the identifier fields, the fuzzy title and the JSON maps against that DocType.

		Singles and child tables are refused, because the resolver matches records by name or title.
		"""
		if not self.link_doctype:
			frappe.throw(
				_("Choose the DocType to match in each Entity Match row."), exc=frappe.ValidationError
			)
		if not frappe.db.exists("DocType", self.link_doctype):
			frappe.throw(_("{0} is not a DocType.").format(self.link_doctype), exc=frappe.ValidationError)
		meta = frappe.get_meta(self.link_doctype)
		if meta.istable or meta.issingle:
			frappe.throw(
				_("{0} is a child table or a single, so records cannot be matched against it.").format(
					self.link_doctype
				),
				exc=frappe.ValidationError,
			)
		self._validate_identifier_fields(meta)
		if self.fuzzy:
			self._validate_fuzzy_title(meta)
		self._validate_field_map(meta)
		self._validate_default_values(meta)

	def _validate_identifier_fields(self, meta):
		# Identifiers are compared exactly. Without an index, each lookup would scan the whole table.
		for fieldname in parse_fieldnames(self.identifier_fields):
			docfield = meta.get_field(fieldname)
			if not docfield:
				frappe.throw(
					_("Identifier field {0} does not exist on {1}.").format(fieldname, self.link_doctype),
					exc=frappe.ValidationError,
				)
			if not (docfield.search_index or docfield.unique):
				frappe.throw(
					_(
						"Identifier field {0} on {1} needs Search Index turned on or Unique, so the match does not scan the whole table."
					).format(fieldname, self.link_doctype),
					exc=frappe.ValidationError,
				)

	def _validate_fuzzy_title(self, meta):
		# A DocType whose title falls back to name has no title field to compare.
		title = meta.get_field(meta.get_title_field())
		if not title:
			frappe.throw(
				_(
					"Fuzzy matching needs a title field on {0}. Set a Title Field on that DocType, or turn Fuzzy Match off."
				).format(self.link_doctype),
				exc=frappe.ValidationError,
			)
		if title.fieldtype in TEXT_TITLE_FIELDTYPES:
			frappe.throw(
				_(
					"Fuzzy matching is not allowed on {0}, because its title field {1} is {2}. Turn Fuzzy Match off, or choose a DocType with a short title field."
				).format(self.link_doctype, title.fieldname, title.fieldtype),
				exc=frappe.ValidationError,
			)

	def _validate_field_map(self, meta):
		# field_map maps each extracted key (the object key) to the linked field it fills (the value). Extracted
		# keys come from LlamaCloud, so only the linked field names can be checked against the DocType.
		filled: set[str] = set()
		for extracted_key, fieldname in parse_json_object(self.field_map, _("Field Map")).items():
			if not extracted_key.strip():
				frappe.throw(
					_("Field Map has a blank key. Each key must be the name of an extracted field."),
					exc=frappe.ValidationError,
				)
			# The extracted key is matched exactly, so a key with spaces around it would never match anything.
			if extracted_key != extracted_key.strip():
				frappe.throw(
					_(
						"Field Map key {0} has spaces around it. Remove them, so the key matches the extracted field."
					).format(repr(extracted_key)),
					exc=frappe.ValidationError,
				)
			if not isinstance(fieldname, str) or not meta.get_field(fieldname):
				frappe.throw(
					_("Field Map value {0} is not a field of {1}.").format(fieldname, self.link_doctype),
					exc=frappe.ValidationError,
				)
			# Two extracted keys that fill one linked field would make the later one silently win.
			if fieldname in filled:
				frappe.throw(
					_("Field Map fills {0} from more than one key. Keep one key for each field.").format(
						fieldname
					),
					exc=frappe.ValidationError,
				)
			filled.add(fieldname)

	def _validate_default_values(self, meta):
		for fieldname in parse_json_object(self.default_values, _("Default Values")):
			docfield = meta.get_field(fieldname)
			if not docfield or docfield.fieldtype in table_fields:
				frappe.throw(
					_("Default Values key {0} is not a field of {1}.").format(fieldname, self.link_doctype),
					exc=frappe.ValidationError,
				)


def parse_json_object(raw: str | None, label: str) -> dict[str, Any]:
	"""Return raw as a JSON object. A blank value is an empty object. Invalid JSON, or JSON that is not an object, is refused."""
	if not (raw or "").strip():
		return {}
	try:
		value = json.loads(raw or "")
	except ValueError:
		value = None
	if not isinstance(value, dict):
		frappe.throw(
			_("{0} must be a JSON object, for example {{}}.").format(label), exc=frappe.ValidationError
		)
	return value
