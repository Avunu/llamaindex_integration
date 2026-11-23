# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Create the Draft target document and attach the source file (spec section 6, step 5).

Documents are always created as Draft (docstatus 0). Nothing is submitted in v1. The target doctype, its duplicate key,
its default values and its reconciliation field all come from the Llama DocType Rule, so nothing here names a specific
doctype. These functions do not open, release or commit savepoints, and they do not commit. tasks.py owns the savepoint,
the create lock and the commit.
"""

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import frappe
from frappe import _
from frappe.model import table_fields
from frappe.utils import cint, flt, getdate, now_datetime

from llamaindex_integration.services.entity_resolver import (
	VALUE_FIELDTYPES,
	Resolution,
	is_blank,
	parse_fieldnames,
)

TOTALS_TOLERANCE = 0.01
MISSING_TOTAL_NOTE = "No reconciliation total was extracted, so the totals were not reconciled."
MISSING_COMPUTED_TOTAL_NOTE = (
	"The computed total was not set on the draft, so the totals were not reconciled."
)
KEY_SKIPPED_NOTE = "The duplicate check was skipped because these key fields are empty: {0}."
INT_FIELDTYPES = frozenset({"Int"})
NUMERIC_FIELDTYPES = frozenset({"Float", "Currency", "Percent"})
# The fieldtypes the rule accepts for a reconciliation field. Kept in step with RECONCILIATION_FIELDTYPES in the rule.
RECONCILIATION_FIELDTYPES = frozenset({"Currency", "Float", "Int", "Long Int", "Percent"})
# A number with thousands separators in groups of three, such as 1,234,567.50. Any other comma is not a total.
THOUSANDS_RE = re.compile(r"[+-]?\d{1,3}(,\d{3})+(\.\d*)?")
# Fieldtypes that hold one scalar value a duplicate key can compare. Table fields are not listed. Text fields
# are not listed either, because Frappe refuses a search index on them, and the key lookup needs one.
DUPLICATE_KEY_FIELDTYPES = ("Data", "Link", "Select", "Int", "Float", "Currency", "Percent", "Date")
KEY_FIELDTYPES = DUPLICATE_KEY_FIELDTYPES
FILE_ASSOCIATION_DOCTYPE = "File Association"
FILE_ASSOCIATION_FIELD = "file_association"


class DuplicateDocumentError(frappe.ValidationError):
	"""An existing document has the same values for the rule's duplicate key fields."""


class TotalsMismatchError(frappe.ValidationError):
	"""The extracted reconciliation total differs from the computed total beyond tolerance."""


def parse_duplicate_key_fields(raw: str | None) -> tuple[str, ...]:
	"""Split the rule's comma-separated duplicate_key_fields into fieldnames. Delegates to parse_fieldnames."""
	return parse_fieldnames(raw)


def parse_default_values(raw: str | None) -> dict[str, Any]:
	"""Parse the rule's default_values JSON object. None and blank input give {}.

	Raises frappe.ValidationError when the text is not valid JSON, or when the JSON is not an object.
	"""
	if not raw or not raw.strip():
		return {}
	try:
		parsed = json.loads(raw)
	except json.JSONDecodeError as exc:
		raise frappe.ValidationError(_("Default Values is not valid JSON.")) from exc
	if not isinstance(parsed, dict):
		raise frappe.ValidationError(_("Default Values must be a JSON object."))
	return parsed


def create_draft_document(
	target_doctype: str,
	extracted: dict[str, Any],
	resolution: Resolution,
	*,
	duplicate_key_fields: Sequence[str] = (),
	default_values: Mapping[str, Any] | None = None,
	reconciliation_field: str | None = None,
) -> str:
	"""Insert the Draft document with ignore_permissions and return its name.

	The draft is built first. The values from extraction and resolution go onto a new document, the rule's default values
	fill the empty ones, and then the defaults that Frappe fills and set_missing_values run. A key is a header fieldname,
	or table.fieldname for a column of a Table, and a child row that lacks the value gets the default. The key fields and
	the default keys are checked against the target before anything is built.

	When duplicate_key_fields is given, the key values of the built draft are compared with existing documents of the
	target, so a value that Frappe fills counts. A blank key value skips the check, adds a note and flags the draft for
	review. A match raises DuplicateDocumentError before anything is inserted. A submittable target ignores cancelled
	documents.

	When reconciliation_field is set, the document is inserted and then the value the target computed for that field is
	compared with the extracted value. A mismatch, or a computed value that is not set, raises TotalsMismatchError, and the
	caller rolls the insert back. A missing extracted value is flagged for review. Raises frappe.ValidationError from
	insert. Does not submit and does not commit. A reconciliation field that is not a read-only numeric field of the
	target raises before anything is built.
	"""
	if reconciliation_field:
		_require_computed_reconciliation_field(target_doctype, reconciliation_field)
	_check_duplicate_key_fields(target_doctype, duplicate_key_fields)
	values = _parent_values(target_doctype, extracted, resolution)
	child_rows = {fieldname: [dict(row) for row in rows] for fieldname, rows in resolution.child_rows.items()}
	if default_values:
		_apply_default_values(target_doctype, values, child_rows, default_values)

	doc = _build_draft(target_doctype, values, child_rows)
	if duplicate_key_fields:
		_check_duplicate(doc, resolution, duplicate_key_fields)

	return _insert_draft(doc, extracted, resolution, reconciliation_field)


def attach_source_file(source_file: str, target_doctype: str, target_name: str) -> str:
	"""Attach the source File to target_doctype and target_name, and return the name that now represents it.

	source_file is the File name (Llama Document Log.source_file). It runs as Administrator, so it works when the job
	user cannot read a private source file. The previous user is restored even when the attach fails.

	When cloud_storage is installed and File has a file_association table, the source is linked with one File
	Association row, and its own name is returned. Otherwise File.create_attachment_copy makes a new File that reuses
	file_url, and the name of that copy is returned. The copy does not match the Communication guard in
	email_ingestion, so the hook does not run again for it.
	"""
	previous_user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		source = frappe.get_doc("File", source_file)
		if _uses_file_association():
			_add_file_association(source_file, target_doctype, target_name)
			return source_file
		attachment = source.create_attachment_copy(
			attached_to_doctype=target_doctype,
			attached_to_name=target_name,
			ignore_permissions=True,
		)
		return attachment.name
	finally:
		frappe.set_user(previous_user)


def _uses_file_association() -> bool:
	return "cloud_storage" in frappe.get_installed_apps() and frappe.get_meta("File").has_field(
		FILE_ASSOCIATION_FIELD
	)


def _add_file_association(source_file: str, target_doctype: str, target_name: str) -> None:
	"""Link the source File to the target with one File Association row, and no File is saved or created.

	The row is written with db_insert. Calling this again for the same target adds nothing, because a row with the same
	link already exists on the source.
	"""
	parent_filters = {"parent": source_file, "parenttype": "File", "parentfield": FILE_ASSOCIATION_FIELD}
	link_filters = {"link_doctype": target_doctype, "link_name": target_name}
	if frappe.db.exists(FILE_ASSOCIATION_DOCTYPE, {**parent_filters, **link_filters}):
		return

	idx = frappe.db.count(FILE_ASSOCIATION_DOCTYPE, parent_filters) + 1
	frappe.get_doc(
		{
			"doctype": FILE_ASSOCIATION_DOCTYPE,
			**parent_filters,
			**link_filters,
			"user": frappe.session.user,
			"timestamp": now_datetime(),
			"idx": idx,
		}
	).db_insert()


def _parent_values(target_doctype: str, extracted: dict[str, Any], resolution: Resolution) -> dict[str, Any]:
	"""Return the parent field values to set on the new document.

	Link values come only from the resolver, so a raw extracted name never reaches a Link field.
	Only value fieldtypes are copied (the same set the resolver uses), so Attach, Password and Dynamic
	Link fields are never written from extracted data. Read-only extracted values only feed the totals check.
	Table fields and unknown fields are dropped.
	"""
	meta = frappe.get_meta(target_doctype)
	values: dict[str, Any] = {}
	for fieldname, value in extracted.items():
		df = meta.get_field(fieldname)
		if df is None or df.fieldtype not in VALUE_FIELDTYPES or df.fieldtype == "Link" or df.read_only:
			continue
		values[fieldname] = value

	for fieldname, value in resolution.values.items():
		df = meta.get_field(fieldname)
		if df is not None and df.fieldtype not in table_fields:
			values[fieldname] = value
	return values


def _apply_default_values(
	target_doctype: str,
	values: dict[str, Any],
	child_rows: dict[str, list[dict[str, Any]]],
	default_values: Mapping[str, Any],
) -> None:
	"""Fill the empty header fields and the child rows that lack a value, from the rule's default values.

	A value that the extraction or the resolver set is kept. A key that does not name a value field of the target, or
	a column of a Table on the target, raises frappe.ValidationError before anything is inserted, because a silent skip
	would hide a misconfigured rule.
	"""
	meta = frappe.get_meta(target_doctype)
	for key, default in default_values.items():
		table_name, dot, column = key.partition(".")
		if not dot:
			df = meta.get_field(key)
			if df is None or df.fieldtype in table_fields:
				raise frappe.ValidationError(_("{0} is not a value field of {1}").format(key, target_doctype))
			if is_blank(df.fieldtype, values.get(key)):
				values[key] = default
			continue

		df = meta.get_field(table_name)
		if df is None or df.fieldtype not in table_fields:
			raise frappe.ValidationError(
				_("{0} is not a table field of {1}").format(table_name, target_doctype)
			)
		child_df = frappe.get_meta(df.options).get_field(column)
		if child_df is None:
			raise frappe.ValidationError(_("{0} is not a field of {1}").format(column, df.options))
		for row in child_rows.get(table_name, []):
			if is_blank(child_df.fieldtype, row.get(column)):
				row[column] = default


def _check_duplicate_key_fields(target_doctype: str, duplicate_key_fields: Sequence[str]) -> None:
	"""Raise frappe.ValidationError unless each key is a field of target_doctype with a fieldtype a duplicate key can use.

	A key field that is not on the target, or whose fieldtype cannot be a key, is a configuration error. A silent skip would
	switch the guard off. This runs before any document is built.
	"""
	meta = frappe.get_meta(target_doctype)
	for fieldname in duplicate_key_fields:
		df = meta.get_field(fieldname)
		if df is None:
			raise frappe.ValidationError(_("{0} is not a field of {1}").format(fieldname, target_doctype))
		if df.fieldtype not in DUPLICATE_KEY_FIELDTYPES:
			raise frappe.ValidationError(
				_("{0} on {1} has fieldtype {2}, which a duplicate key cannot use.").format(
					fieldname, target_doctype, df.fieldtype
				)
			)


def _build_draft(
	target_doctype: str,
	values: dict[str, Any],
	child_rows: dict[str, list[dict[str, Any]]],
) -> Any:
	"""Return a new document with the values and child rows set, and with the defaults that Frappe fills applied.

	frappe.new_doc fills the fields that have a Frappe default, such as the date of a ToDo. The values are set after it, so an
	extracted or rule value wins over that default. set_missing_values and calculate_taxes_and_totals are the target's own
	defaults and totals, and they run here too, so the duplicate check sees the values the draft will hold.
	"""
	doc = frappe.new_doc(target_doctype)
	for fieldname, value in values.items():
		doc.set(fieldname, value)
	_append_child_rows(doc, target_doctype, child_rows)

	if hasattr(doc, "set_missing_values"):
		doc.set_missing_values()
	if hasattr(doc, "calculate_taxes_and_totals"):
		doc.calculate_taxes_and_totals()
	return doc


def _check_duplicate(doc: Any, resolution: Resolution, duplicate_key_fields: Sequence[str]) -> None:
	"""Compare the key values of the built draft with existing documents, or flag the draft when a key value is blank.

	The key values are read from the draft, so a value that Frappe filled from a default is compared too. They are normalized
	on the draft, so the stored values match the values that were compared. A blank key skips the check. The key fields must
	already be checked by _check_duplicate_key_fields.
	"""
	meta = frappe.get_meta(doc.doctype)
	key_values: dict[str, Any] = {}
	empty: list[str] = []
	for fieldname in duplicate_key_fields:
		df = meta.get_field(fieldname)
		value = _normalize_key_value(df.fieldtype, doc.get(fieldname))
		doc.set(fieldname, value)
		if value is None:
			empty.append(fieldname)
		else:
			key_values[fieldname] = value

	if empty:
		resolution.notes.append(KEY_SKIPPED_NOTE.format(", ".join(empty)))
		resolution.needs_review = True
		return

	existing = _find_existing(doc.doctype, key_values, submittable=bool(meta.is_submittable))
	if existing:
		raise DuplicateDocumentError(
			_("{0} {1} already exists with the same {2}.").format(
				doc.doctype, existing, ", ".join(duplicate_key_fields)
			)
		)


def _normalize_key_value(fieldtype: str, value: Any) -> Any:
	"""Return the key value as it is stored, or None when it is blank.

	Strings are stripped. Numeric fields are converted, so "1,234.50" compares with the stored number. Date
	values are parsed, so "2026-1-5" compares with the stored 2026-01-05. An unreadable date raises
	frappe.ValidationError, and a date Frappe reads as empty counts as blank.
	"""
	if isinstance(value, str):
		value = value.strip()
	if value is None or value == "":
		return None
	if fieldtype in INT_FIELDTYPES:
		return cint(value)
	if fieldtype in NUMERIC_FIELDTYPES:
		return flt(value)
	if fieldtype == "Date":
		return getdate(value)
	return value


def _duplicate_query(target_doctype: str, key_values: dict[str, Any], *, submittable: bool) -> Any:
	"""Build the duplicate lookup as a locking read, so the newest committed row is seen.

	FOR UPDATE returns the latest committed row even under REPEATABLE READ, where a plain read would keep the
	snapshot taken at the transaction's first read. Cancelled documents are ignored only for submittable targets.
	"""
	table = frappe.qb.DocType(target_doctype)
	query = frappe.qb.from_(table).select(table.name)
	for fieldname, value in key_values.items():
		query = query.where(table[fieldname] == value)
	if submittable:
		query = query.where(table.docstatus != 2)
	return query.for_update().limit(1)


def _find_existing(target_doctype: str, key_values: dict[str, Any], *, submittable: bool) -> str | None:
	names = _duplicate_query(target_doctype, key_values, submittable=submittable).run(pluck=True)
	return names[0] if names else None


def _insert_draft(
	doc: Any,
	extracted: Mapping[str, Any],
	resolution: Resolution,
	reconciliation_field: str | None,
) -> str:
	doc.insert(ignore_permissions=True)
	_check_totals(reconciliation_field, extracted, doc, resolution)
	return doc.name


def _append_child_rows(doc: Any, target_doctype: str, child_rows: dict[str, list[dict[str, Any]]]) -> None:
	meta = frappe.get_meta(target_doctype)
	for fieldname, rows in child_rows.items():
		df = meta.get_field(fieldname)
		if df is None or df.fieldtype not in table_fields:
			raise frappe.ValidationError(
				_("{0} is not a table field of {1}").format(fieldname, target_doctype)
			)
		child_meta = frappe.get_meta(df.options)
		for row in rows:
			doc.append(fieldname, {key: value for key, value in row.items() if child_meta.has_field(key)})


def _require_computed_reconciliation_field(target_doctype: str, fieldname: str) -> None:
	"""Raise frappe.ValidationError unless fieldname is a read-only numeric field of target_doctype.

	The rule checks this when it is saved. This check runs for every draft too, because a field can become writable later,
	for example through Customize Form. A writable field is copied from the extraction, and the totals check would then
	compare the value with itself.
	"""
	docfield = frappe.get_meta(target_doctype).get_field(fieldname)
	if docfield is None or not docfield.read_only or docfield.fieldtype not in RECONCILIATION_FIELDTYPES:
		raise frappe.ValidationError(
			_("{0} on {1} must be a read-only numeric field to be reconciled.").format(
				fieldname, target_doctype
			)
		)


def _check_totals(
	reconciliation_field: str | None,
	extracted: Mapping[str, Any],
	doc: Any,
	resolution: Resolution,
) -> None:
	"""Reconcile the extracted value of reconciliation_field with the value the target computed for it after insert.

	Nothing is checked when no reconciliation field is set. The computed value is read first. When it is None the total was
	not computed, so nothing can be reconciled. A zero must not pass for it, so the note is added and TotalsMismatchError
	rolls the draft back to Needs Review. Otherwise, when the extraction has no readable total, the draft is flagged for
	review, because nothing was reconciled. When both values exist, they must agree within the tolerance. The extracted
	total is parsed strictly, because flt returns zero for text it cannot read. Raises TotalsMismatchError on a mismatch.
	The messages do not include the amounts, because the error text is stored on the log.
	"""
	if not reconciliation_field:
		return
	computed = getattr(doc, reconciliation_field, None)
	if computed is None:
		resolution.notes.append(MISSING_COMPUTED_TOTAL_NOTE)
		raise TotalsMismatchError(_("The computed total was not set, so the totals could not be reconciled."))
	extracted_total = _parse_total(extracted.get(reconciliation_field))
	if extracted_total is None:
		resolution.notes.append(MISSING_TOTAL_NOTE)
		resolution.needs_review = True
		return
	if abs(extracted_total - flt(computed)) > TOTALS_TOLERANCE:
		raise TotalsMismatchError(_("The extracted total does not match the computed total."))


def _parse_total(value: Any) -> float | None:
	"""Return the extracted total as a finite number, or None when it does not read as one. Pure.

	Commas are taken as thousands separators only when they group digits in threes, so "1,234.50" reads as 1234.5. A comma
	anywhere else, such as the decimal comma of "1.234,50", is not read, because it would give a wrong number. Booleans,
	lists, blank text, NaN and infinity are not totals.
	"""
	if isinstance(value, bool) or not isinstance(value, int | float | str):
		return None
	if isinstance(value, str):
		value = value.strip()
		if "," in value and not THOUSANDS_RE.fullmatch(value):
			return None
		value = value.replace(",", "")
	try:
		number = float(value)
	except ValueError:
		return None
	return number if math.isfinite(number) else None
