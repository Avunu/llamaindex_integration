# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

from typing import Any

import frappe
from frappe import _
from frappe.model import table_fields
from frappe.model.document import Document

from llamaindex_integration.services import schema_builder
from llamaindex_integration.services.document_creator import (
	KEY_FIELDTYPES,
	parse_default_values,
	parse_duplicate_key_fields,
)

# Fieldtypes a reconciliation total can be read from. Frappe counts Check as numeric, but a checkbox is not a total.
RECONCILIATION_FIELDTYPES = ("Currency", "Float", "Int", "Long Int", "Percent")


class LlamaDocTypeRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		auto_submit: DF.Check
		classification_description: DF.SmallText
		default_values: DF.Code
		duplicate_key_fields: DF.SmallText
		included_fields: DF.SmallText
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		reconciliation_field: DF.Data
		target_doctype: DF.Link
	# end: auto-generated types

	def validate(self):
		self.validate_target_and_keys()

	def validate_target_and_keys(self):
		"""Check the target, the keys, the reconciliation field and the default values against the target DocType.

		Frappe runs validate() only on the parent document, never on a child row. LlamaIndex Settings calls this
		for each of its rows, so a bad rule is refused when Settings is saved. Documents are created with
		ignore_permissions, so the target must be a transaction DocType. Singles, child tables and masters such
		as User are refused.
		"""
		# Parsed before the target is checked, so invalid default values are refused even for a blank or unknown target.
		default_values = parse_default_values(self.default_values)
		if not self.target_doctype or not frappe.db.exists("DocType", self.target_doctype):
			return
		meta = frappe.get_meta(self.target_doctype)
		if not meta.is_submittable:
			frappe.throw(
				_(
					"{0} is not a submittable transaction DocType, so it cannot be a Document Rule target."
				).format(self.target_doctype),
				exc=frappe.ValidationError,
			)
		self._validate_duplicate_key_fields(meta)
		self._validate_included_fields()
		self._validate_reconciliation_field(meta)
		self._validate_default_values(meta, default_values)

	def _validate_duplicate_key_fields(self, meta):
		# Key fields are parent fields only. get_field returns None for child-table fields and unknown names.
		for fieldname in parse_duplicate_key_fields(self.duplicate_key_fields):
			docfield = meta.get_field(fieldname)
			if not docfield:
				frappe.throw(
					_("Duplicate key field {0} does not exist on {1}.").format(
						fieldname, self.target_doctype
					),
					exc=frappe.ValidationError,
				)
			if docfield.fieldtype not in KEY_FIELDTYPES:
				frappe.throw(
					_(
						"Duplicate key field {0} on {1} has fieldtype {2}. Choose a field of one of these types: {3}."
					).format(
						fieldname,
						self.target_doctype,
						docfield.fieldtype,
						", ".join(KEY_FIELDTYPES),
					),
					exc=frappe.ValidationError,
				)
			if docfield.read_only:
				# Read-only values are never copied from extracted data, so this key would always be blank.
				frappe.throw(
					_(
						"Duplicate key field {0} on {1} is read only, so it is never filled from the document."
					).format(fieldname, self.target_doctype),
					exc=frappe.ValidationError,
				)
			if not (docfield.search_index or docfield.unique):
				# The key lookup takes row locks, and without an index it would lock the whole table.
				frappe.throw(
					_(
						"Duplicate key field {0} on {1} needs Search Index turned on, so the duplicate check does not lock the whole table."
					).format(fieldname, self.target_doctype),
					exc=frappe.ValidationError,
				)

	def _validate_included_fields(self):
		# The extraction schema raises SchemaError for a name that is not on the target. Checking it here stops
		# every job for this rule from failing later on the same name.
		try:
			schema_builder.build_json_schema_for_doctype(
				self.target_doctype, schema_builder.parse_included_fields(self.included_fields)
			)
		except schema_builder.SchemaError as exc:
			frappe.throw(str(exc), exc=frappe.ValidationError)

	def _validate_reconciliation_field(self, meta):
		if not self.reconciliation_field:
			return
		docfield = meta.get_field(self.reconciliation_field)
		if not docfield:
			frappe.throw(
				_("Reconciliation Field {0} does not exist on {1}.").format(
					self.reconciliation_field, self.target_doctype
				),
				exc=frappe.ValidationError,
			)
		if docfield.fieldtype not in RECONCILIATION_FIELDTYPES:
			frappe.throw(
				_(
					"Reconciliation Field {0} on {1} has fieldtype {2}. Choose a numeric field of one of these types: {3}."
				).format(
					self.reconciliation_field,
					self.target_doctype,
					docfield.fieldtype,
					", ".join(RECONCILIATION_FIELDTYPES),
				),
				exc=frappe.ValidationError,
			)
		if not docfield.read_only:
			# A writable field is copied from the extraction, so the check would compare the value with itself.
			frappe.throw(
				_(
					"Reconciliation Field {0} on {1} must be a read-only, computed field such as grand_total, because a writable field is copied from the extraction and then compared with itself."
				).format(self.reconciliation_field, self.target_doctype),
				exc=frappe.ValidationError,
			)

	def _validate_default_values(self, meta, default_values: dict[str, Any]):
		for key in default_values:
			if not _is_default_key(meta, key):
				frappe.throw(
					_(
						"Default value key {0} is not a field of {1}. Use a fieldname, or table.fieldname for a child row."
					).format(key, self.target_doctype),
					exc=frappe.ValidationError,
				)


def _is_default_key(meta, key: str) -> bool:
	"""Return True when key is a parent fieldname, or table.fieldname of a child row, and is not a Table."""
	table_fieldname, dot, child_fieldname = key.partition(".")
	if not dot:
		docfield = meta.get_field(key)
		return docfield is not None and docfield.fieldtype not in table_fields
	table = meta.get_field(table_fieldname)
	if table is None or table.fieldtype not in table_fields:
		return False
	child_docfield = frappe.get_meta(table.options).get_field(child_fieldname)
	return child_docfield is not None and child_docfield.fieldtype not in table_fields
