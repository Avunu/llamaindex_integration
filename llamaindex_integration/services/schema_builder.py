# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Build the JSON Schema that LlamaCloud extraction uses for a target DocType (spec section 6, step 2).

Nothing here names a particular DocType. The default selection is the required fields of the target. A required Table
is part of it, with its own required columns, and those columns follow the same rules one level down, up to
MAX_TABLE_DEPTH. Read-only, attachment, password, dynamic link, button and layout fields are never part of it. A Table
that is not required is extracted only when its own fieldname is listed. A reconciliation field can be added on
request. It is a numeric field of the target that the document creator compares with the draft's computed value after
the draft is inserted.
"""

import re
from collections.abc import Mapping
from typing import Any

import frappe

# Table fields are expanded at most this many levels deep. A Table nested at this depth is dropped.
MAX_TABLE_DEPTH = 2

# Never extracted, whatever the target.
ALWAYS_EXCLUDED_FIELDNAMES = frozenset({"naming_series", "amended_from"})

# Owned by the workflow of a submittable target. A target that is not submittable keeps them, so a required status is
# extracted.
SUBMITTABLE_EXCLUDED_FIELDNAMES = frozenset({"status", "docstatus"})

LAYOUT_FIELDTYPES = frozenset({"Section Break", "Column Break", "Tab Break", "HTML", "Fold", "Heading"})

# Fieldtypes that the default selection never includes. A required Table is not in this set: the default selection
# includes it with its required columns, and a Table that is not required is left out.
DEFAULT_EXCLUDED_FIELDTYPES = frozenset(
	{"Attach", "Attach Image", "Password", "Dynamic Link", "Button"} | LAYOUT_FIELDTYPES
)

NUMERIC_FIELDTYPES = frozenset({"Int", "Long Int", "Float", "Currency", "Percent"})

RECONCILIATION_DESCRIPTION = "Total printed on the source document. For reconciliation only: the draft's computed value is checked against it."
MATCH_KEY_DESCRIPTION = "{label}. Used to match a record of the linked doctype."

_STRING_FIELDTYPES = frozenset({"Data", "Small Text", "Text", "Long Text", "Text Editor", "Link"})
_INTEGER_FIELDTYPES = frozenset({"Int", "Long Int"})
_NUMBER_FIELDTYPES = frozenset({"Float", "Currency", "Percent"})


class SchemaError(ValueError):
	"""An included_fields entry does not exist on the DocType, or the schema would be empty."""


def build_json_schema_for_doctype(
	target_doctype: str,
	included_fields: list[str] | None = None,
	*,
	reconciliation_field: str | None = None,
	match_keys: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
	"""Return a JSON Schema object for target_doctype.

	When included_fields is None or empty, the default set is every required field of the target. A required Table is
	included with its own required columns. Read-only, system, attachment, password, dynamic link, button and layout
	fields are skipped, and so is a Table that is not required. The system fields are amended_from and naming_series,
	always. status and docstatus are also skipped for a submittable target.

	When included_fields is given, only the named fields are extracted. A Table is extracted only when its own fieldname
	is listed. Listed row fieldnames restrict that Table's columns, and a listed Table with none of its columns listed
	gets the default columns. A name that is listed but is a read-only or system field is dropped silently.

	When reconciliation_field names a numeric field of the target, that field is added as a header property in both
	modes, with a description that says it is for reconciliation only. A reconciliation field that is missing or not
	numeric is ignored.

	match_keys adds the keys that the entity resolver reads to match linked records. The key "" is the header level, and
	any other key is a Table fieldname of the target. Each inner mapping goes from an extracted key to a spec of
	{"fieldtype", "label", "options"}. A key is added as a property at its level, typed from its spec and described as
	used to match a record of the linked doctype. A key that is already a property of that level is not added again. A
	Table level is used only when that Table is extracted. Nothing is added below the row level of a Table.

	Raises frappe.DoesNotExistError when the DocType is unknown. Raises SchemaError when a listed name is neither a
	header field nor a column of a listed Table, or when nothing is left to extract.
	"""
	properties = target_properties(target_doctype, included_fields, reconciliation_field=reconciliation_field)
	if not properties:
		raise SchemaError(f"No extractable fields for {target_doctype}")
	_add_match_keys(properties, match_keys or {})
	return {"type": "object", "properties": properties}


def target_properties(
	target_doctype: str,
	included_fields: list[str] | None = None,
	*,
	reconciliation_field: str | None = None,
) -> dict[str, Any]:
	"""Return the header properties that build_json_schema_for_doctype extracts, before any match keys are added.

	The selection, the SchemaError for a listed name that is not on the target, and the reconciliation field all work as
	they do in build_json_schema_for_doctype. The result can be empty.
	"""
	meta = frappe.get_meta(target_doctype)
	wanted = frozenset(included_fields) if included_fields else None
	if wanted is not None:
		orphans = sorted(wanted - _claimable_fieldnames(meta, wanted, depth=0))
		if orphans:
			raise SchemaError(
				f"Fields not found on {target_doctype}, or in a listed Table: {', '.join(orphans)}"
			)

	properties = _build_properties(meta, wanted, depth=0)
	reconciliation = _find_field(meta, reconciliation_field) if reconciliation_field else None
	if reconciliation is not None and reconciliation.fieldtype in NUMERIC_FIELDTYPES:
		properties[reconciliation.fieldname] = _reconciliation_schema(reconciliation)
	return properties


def _add_match_keys(
	properties: dict[str, Any], match_keys: Mapping[str, Mapping[str, Mapping[str, Any]]]
) -> None:
	"""Add each match key as a property of its level, unless that level already has a property with the same name."""
	for level, keys in match_keys.items():
		if level:
			table = properties.get(level)
			if not table or table.get("type") != "array":
				# The Table is not extracted, so no rows arrive for the key to sit in.
				continue
			level_properties = table["items"]["properties"]
		else:
			level_properties = properties
		for key, spec in keys.items():
			if key not in level_properties:
				level_properties[key] = _match_key_schema(key, spec)


def _match_key_schema(key: str, spec: Mapping[str, Any]) -> dict[str, Any]:
	"""Return the schema for one match key. The type follows the spec's fieldtype, and a string is used when it has none."""
	label = spec.get("label") or key
	schema = fieldtype_to_schema(spec.get("fieldtype") or "", label, spec.get("options")) or {
		"type": "string"
	}
	description = MATCH_KEY_DESCRIPTION.format(label=label)
	# Keep the field's own hint, such as Select options or a date format. A hint that is only the label adds nothing.
	hint = schema.get("description")
	if hint and hint != label:
		description = f"{description} {hint}"
	return {**schema, "description": description}


def parse_included_fields(raw: str | None) -> list[str] | None:
	"""Split the included_fields Small Text value on commas or newlines. Blank or None returns None."""
	if not raw:
		return None
	names = [part.strip() for part in re.split(r"[,\n]", raw)]
	return list(dict.fromkeys(name for name in names if name)) or None


def fieldtype_to_schema(fieldtype: str, label: str, options: str | None) -> dict[str, Any] | None:
	"""Map one Frappe fieldtype to a JSON Schema fragment. Pure.

	Returns None for fieldtypes that are not extracted: layout fields, Table (the builder recurses
	into it), Dynamic Link, Attach, Attach Image, Password and any fieldtype not listed here.
	"""
	if fieldtype in _STRING_FIELDTYPES:
		return {"type": "string", "description": label}
	if fieldtype == "Select":
		description = f"{label} (Options: {options})" if options else label
		return {"type": "string", "description": description}
	if fieldtype in _INTEGER_FIELDTYPES:
		return {"type": "integer"}
	if fieldtype in _NUMBER_FIELDTYPES:
		return {"type": "number"}
	if fieldtype == "Date":
		return {"type": "string", "format": "date", "description": "YYYY-MM-DD"}
	if fieldtype == "Datetime":
		return {"type": "string", "format": "date-time", "description": "YYYY-MM-DD HH:MM:SS"}
	if fieldtype == "Check":
		return {"type": "boolean"}
	return None


def _reconciliation_schema(df: Any) -> dict[str, Any]:
	"""Return the schema for a numeric reconciliation field, with the reconciliation-only description."""
	schema = fieldtype_to_schema(df.fieldtype, df.label or df.fieldname, df.options) or {}
	return {**schema, "description": RECONCILIATION_DESCRIPTION}


def _find_field(meta: Any, fieldname: str) -> Any | None:
	return next((df for df in meta.fields if df.fieldname == fieldname), None)


def _excluded_fieldnames(meta: Any) -> frozenset[str]:
	if meta.is_submittable:
		return ALWAYS_EXCLUDED_FIELDNAMES | SUBMITTABLE_EXCLUDED_FIELDNAMES
	return ALWAYS_EXCLUDED_FIELDNAMES


def _claimable_fieldnames(meta: Any, wanted: frozenset[str], depth: int) -> set[str]:
	"""Return the names a listed fieldname may match: this level's fields and the columns of listed Tables."""
	names: set[str] = set()
	for df in meta.fields:
		names.add(df.fieldname)
		if df.fieldtype == "Table" and df.fieldname in wanted and df.options and depth < MAX_TABLE_DEPTH:
			names |= _claimable_fieldnames(frappe.get_meta(df.options), wanted, depth + 1)
	return names


def _build_properties(meta: Any, wanted: frozenset[str] | None, depth: int) -> dict[str, Any]:
	"""Build the properties for one DocType level. wanted None selects the default fields."""
	excluded = _excluded_fieldnames(meta)
	properties: dict[str, Any] = {}
	for df in meta.fields:
		if df.fieldname in excluded or df.read_only:
			continue
		schema = _field_schema(df, wanted, depth)
		if schema is not None:
			properties[df.fieldname] = schema
	return properties


def _field_schema(df: Any, wanted: frozenset[str] | None, depth: int) -> dict[str, Any] | None:
	"""Return the schema for one field of a level, or None when the field is not extracted."""
	if wanted is None:
		if not df.reqd or df.fieldtype in DEFAULT_EXCLUDED_FIELDTYPES:
			return None
	elif df.fieldname not in wanted:
		return None
	if df.fieldtype == "Table":
		return _table_schema(df, wanted, depth)
	return fieldtype_to_schema(df.fieldtype, df.label or df.fieldname, df.options)


def _table_schema(df: Any, wanted: frozenset[str] | None, depth: int) -> dict[str, Any] | None:
	"""Return the array schema for a Table field, or None when the Table is not extracted.

	wanted None is the default selection, so the Table gives its required columns. A listed Table with none of its
	columns listed gets its default columns.
	"""
	if depth >= MAX_TABLE_DEPTH or not df.options:
		return None
	child_meta = frappe.get_meta(df.options)
	row_properties = _build_properties(child_meta, wanted, depth + 1)
	if not row_properties and wanted is not None:
		# The Table was listed without any of its columns, so it gets the default columns.
		row_properties = _build_properties(child_meta, None, depth + 1)
	if not row_properties:
		return None
	return {"type": "array", "items": {"type": "object", "properties": row_properties}}
