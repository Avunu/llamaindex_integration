# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Resolve the Link fields of the target doctype and of its child doctypes (spec section 6, step 4).

Each Link value is matched against its linked doctype in this order: exact name, exact title (case-insensitive),
identifier fields, the sender's Contact, fuzzy match on the title, then auto-create. The rule for each linked
doctype is its Llama Entity Match row in LlamaIndex Settings. A linked doctype with no row gets exact name and
exact title only. Nothing here names a particular doctype, so the same rules apply on any Frappe site.

A value accepted by anything other than an exact name or title match gets a note and sets needs_review, so a
person confirms it before the draft is used. A value that matches nothing is left blank and flagged.
resolve_entities runs inside the savepoint that tasks.py opens. It does not open, release or commit savepoints.
"""

import json
import re
from collections import ChainMap
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe.model.meta import Meta
from frappe.query_builder.functions import Lower
from frappe.utils import cint, parse_addr, strip_html
from rapidfuzz import fuzz, process, utils

from llamaindex_integration.services import schema_builder

DEFAULT_FUZZY_THRESHOLD = 85

# A fuzzy match scans every active row of the linked doctype, so larger tables are not fuzzy-matched.
FUZZY_ROW_CAP = 5000

# A title of one of these types holds prose, so a fuzzy match on it is not meaningful.
TEXT_TITLE_FIELDTYPES = frozenset({"Long Text", "Text", "Text Editor"})

# Only these fieldtypes are copied from extracted data. Layout, table, attachment, password and dynamic
# link fields are never copied, which matches the schema builder.
VALUE_FIELDTYPES = frozenset(
	{
		"Check",
		"Currency",
		"Data",
		"Date",
		"Datetime",
		"Float",
		"Int",
		"Link",
		"Long Int",
		"Long Text",
		"Percent",
		"Select",
		"Small Text",
		"Text",
		"Text Editor",
	}
)

_TOKEN_RE = re.compile(r"\w+")


@dataclass
class Resolution:
	values: dict[str, Any] = field(default_factory=dict)
	child_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
	notes: list[str] = field(default_factory=list)
	needs_review: bool = False
	created: list[tuple[str, str]] = field(default_factory=list)
	# Set when a linked record was written and then failed (for example in after_insert). That row is inside the
	# savepoint, and only tasks.py can roll it back, so the whole draft is rolled back when this is set.
	write_failed: bool = False


class LinkedWriteError(frappe.ValidationError):
	"""A linked record was written and then failed, so the draft cannot be kept without it."""


class _RuleError(ValueError):
	"""A Llama Entity Match row holds JSON that is not a usable object. Its message is safe to show."""


@dataclass(frozen=True)
class _LinkRule:
	"""The Llama Entity Match rule for one linked doctype. The default rule has no row and allows exact matches only."""

	identifier_fields: tuple[str, ...] = ()
	fuzzy: bool = False
	auto_create: bool = False
	match_sender_contact: bool = False
	# Linked fieldname to the extracted key that fills it. field_map is stored the other way round:
	# extracted key to linked fieldname.
	extracted_keys: Mapping[str, str] = field(default_factory=dict)
	default_values: Mapping[str, Any] = field(default_factory=dict)


def best_fuzzy_match(query: str, choices: Mapping[str, str], threshold: int) -> str | None:
	"""Return the key of the best choice whose WRatio score meets threshold, or None.

	Pure. The match is rejected when the identifying tokens of query and the matched choice differ.
	Identifying tokens are those containing a digit, and single letters such as the A in "Warehouse A".
	So "Widget 10mm" does not match "Widget 100mm", and "Warehouse A" does not match "Warehouse B".
	Only the single best candidate is checked, so a rejected best candidate returns None even when a
	weaker candidate would pass. That is the conservative outcome.
	"""
	if not query or not choices:
		return None
	keys = list(choices)
	best = process.extractOne(
		query,
		[choices[key] or "" for key in keys],
		scorer=fuzz.WRatio,
		processor=utils.default_process,
		score_cutoff=threshold,
	)
	if best is None:
		return None
	matched_text, _score, index = best
	if _identifying_tokens(query) != _identifying_tokens(matched_text):
		return None
	return keys[index]


def parse_fieldnames(raw: str | None) -> tuple[str, ...]:
	"""Split a comma-separated fieldname list. Whitespace is stripped, blank parts are dropped, and the order is kept."""
	if not raw:
		return ()
	return tuple(part.strip() for part in raw.split(",") if part.strip())


def resolve_entities(
	target_doctype: str, extracted: dict[str, Any], settings: Any, *, sender_email: str | None = None
) -> Resolution:
	"""Resolve every Link field of target_doctype and of its child doctypes.

	May auto-create linked records when their Llama Entity Match row allows it. sender_email is the sender's
	address, with or without a display name. It is used for the Contact lookup. Called by tasks.py inside the
	llama_doc_creation savepoint. Never raises.
	"""
	res = Resolution()
	try:
		_resolve_into(target_doctype, extracted or {}, settings, res, sender_email=sender_email)
	except Exception as exc:
		_flag(res, f"Entity resolution for {target_doctype} failed ({type(exc).__name__}). Review needed.")
	return res


def _fuzzy_title_allowed(meta: Meta) -> bool:
	"""False when the title field holds prose (Text Editor, Text or Long Text). Falls back to name, which is allowed."""
	docfield = meta.get_field(meta.get_title_field())
	return docfield is None or docfield.fieldtype not in TEXT_TITLE_FIELDTYPES


def _identifying_tokens(text: str) -> list[str]:
	return sorted(
		token
		for token in _TOKEN_RE.findall(text.lower())
		if len(token) == 1 or any(char.isdigit() for char in token)
	)


def _text(value: Any) -> str:
	return "" if value is None else str(value).strip()


def _sender_address(sender_email: str | None) -> str:
	"""The lowercased address in sender_email, which may carry a display name, or '' when there is none."""
	text = _text(sender_email)
	if not text:
		return ""
	return (parse_addr(text)[1] or "").strip().lower()


def _flag(res: Resolution, note: str) -> None:
	res.notes.append(note)
	res.needs_review = True


def _ambiguous(res: Resolution, where: str, doctype: str, what: str) -> None:
	_flag(res, f"{where}: several {doctype} records match {what}. Pick one by hand.")


def _json_object(raw: Any, label: str) -> dict[str, Any]:
	if raw is None or raw == "":
		return {}
	if isinstance(raw, Mapping):
		return dict(raw)
	try:
		value = json.loads(raw)
	except (TypeError, ValueError) as exc:
		raise _RuleError(f"{label} is not valid JSON") from exc
	if not isinstance(value, dict):
		raise _RuleError(f"{label} must be a JSON object")
	return value


def _link_rule(settings: Any, linked: str) -> _LinkRule:
	row = next(
		(
			row
			for row in (getattr(settings, "entity_matches", None) or [])
			if getattr(row, "link_doctype", None) == linked
		),
		None,
	)
	if row is None:
		return _LinkRule()
	field_map = _json_object(getattr(row, "field_map", None), "field_map")
	return _LinkRule(
		identifier_fields=parse_fieldnames(getattr(row, "identifier_fields", None)),
		fuzzy=bool(cint(getattr(row, "fuzzy", 0))),
		auto_create=bool(cint(getattr(row, "auto_create", 0))),
		match_sender_contact=bool(cint(getattr(row, "match_sender_contact", 0))),
		extracted_keys={linked_field: extracted for extracted, linked_field in field_map.items()},
		default_values=_json_object(getattr(row, "default_values", None), "default_values"),
	)


def _fuzzy_threshold(settings: Any) -> int:
	return cint(getattr(settings, "fuzzy_match_threshold", None)) or DEFAULT_FUZZY_THRESHOLD


def _active_filters(meta: Meta) -> dict[str, Any]:
	"""The disabled check, which applies only when the doctype has a disabled field."""
	return {"disabled": 0} if meta.has_field("disabled") else {}


def _active_count(doctype: str, filters: dict[str, Any]) -> int:
	return frappe.db.count(doctype, filters=filters or None)


def _names(doctype: str, filters: dict[str, Any], limit: int = 2) -> list[str]:
	"""Names matching filters. At most limit names, which is enough to detect ambiguity."""
	return frappe.get_all(doctype, filters=filters, pluck="name", limit=limit)


def _title_names(doctype: str, meta: Meta, value: str, active: dict[str, Any]) -> list[str]:
	"""Names whose title equals value, ignoring case. Empty when the title falls back to name."""
	title_field = meta.get_title_field()
	if title_field == "name":
		return []
	table = frappe.qb.DocType(doctype)
	query = frappe.qb.from_(table).select(table.name).where(Lower(table[title_field]) == value.lower())
	for fieldname, expected in active.items():
		query = query.where(table[fieldname] == expected)
	return query.limit(2).run(pluck=True)


def _identifier_hits(
	doctype: str, rule: _LinkRule, source: Mapping[str, Any], active: dict[str, Any]
) -> list[tuple[str, str, list[str]]]:
	"""For each identifier field with a value in source: (fieldname, value, names that match it exactly)."""
	hits = []
	for fieldname in rule.identifier_fields:
		value = _text(source.get(rule.extracted_keys.get(fieldname, fieldname)))
		if value:
			hits.append((fieldname, value, _names(doctype, {**active, fieldname: value})))
	return hits


def _contact_names(doctype: str, sender: str, active: dict[str, Any]) -> list[str]:
	"""Active records of doctype that are Dynamic Linked to a Contact whose email is sender."""
	contacts = set(
		frappe.get_all("Contact Email", filters={"parenttype": "Contact", "email_id": sender}, pluck="parent")
	)
	contacts.update(frappe.get_all("Contact", filters={"email_id": sender}, pluck="name"))
	if not contacts:
		return []
	linked = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Contact", "parent": ["in", sorted(contacts)], "link_doctype": doctype},
		pluck="link_name",
	)
	if not linked:
		return []
	return _names(doctype, {**active, "name": ["in", sorted(set(linked))]})


def _fuzzy_match(doctype: str, meta: Meta, value: str, active: dict[str, Any], settings: Any) -> str | None:
	if not _fuzzy_title_allowed(meta):
		return None
	if _active_count(doctype, active) > FUZZY_ROW_CAP:
		return None
	title_field = meta.get_title_field()
	rows = frappe.get_all(doctype, filters=active, fields=list(dict.fromkeys(["name", title_field])))
	choices = {row["name"]: row.get(title_field) or row["name"] for row in rows}
	return best_fuzzy_match(value, choices, _fuzzy_threshold(settings))


def is_blank(fieldtype: str | None, value: Any) -> bool:
	"""True when value counts as not set for a field of fieldtype.

	None, blank text and an empty list are blank. Text Editor text that holds only HTML, such as "<p><br></p>", is blank too.
	The creator uses this for default values, and the resolver uses it for required values, so both agree.
	"""
	if isinstance(value, str):
		text = strip_html(value) if fieldtype == "Text Editor" else value
		return not text.strip()
	return value is None or value == []


def _is_missing(docfield: Any, value: Any) -> bool:
	"""A value is missing when is_blank says so. Check fields are never missing."""
	if docfield.fieldtype == "Check":
		return False
	return is_blank(docfield.fieldtype, value)


def _auto_create(
	doctype: str,
	meta: Meta,
	rule: _LinkRule,
	source: Mapping[str, Any],
	res: Resolution,
	*,
	where: str,
	value: str,
) -> str | None:
	"""Insert a record from default_values and the extracted values of the required fields, if all are present."""
	values = dict(rule.default_values)
	missing: list[str] = []
	for docfield in meta.fields:
		if not docfield.reqd or docfield.fieldtype not in VALUE_FIELDTYPES:
			continue
		fieldname = docfield.fieldname
		if _is_missing(docfield, values.get(fieldname)):
			values[fieldname] = source.get(rule.extracted_keys.get(fieldname, fieldname))
		if _is_missing(docfield, values.get(fieldname)):
			missing.append(docfield.label or fieldname)
	if missing:
		_flag(res, f"Cannot create {doctype}: no value for {', '.join(missing)}")
		return None
	doc = None
	try:
		doc = frappe.get_doc({**values, "doctype": doctype})
		doc.insert(ignore_permissions=True)
	except Exception as exc:
		# insert writes the parent row before after_insert and the post-save methods run. A failure after that
		# write leaves a row in the savepoint, and this module cannot roll that back, so the whole draft is flagged.
		if doc is not None and doc.name and frappe.db.exists(doctype, doc.name):
			res.write_failed = True
		_flag(res, f"{where}: could not create {doctype} from '{value}' ({type(exc).__name__}). Left blank.")
		return None
	res.created.append((doctype, doc.name))
	_flag(res, f"{where}: created {doctype} {doc.name} from '{value}'. Check it.")
	return doc.name


def _check_identifiers_agree(
	doctype: str,
	match: str,
	rule: _LinkRule,
	source: Mapping[str, Any],
	active: dict[str, Any],
	res: Resolution,
	where: str,
	*,
	how: str = "by name",
) -> None:
	"""Flag review when an identifier points to a different record than the exact name or title match."""
	for fieldname, value, names in _identifier_hits(doctype, rule, source, active):
		others = [name for name in names if name != match]
		if others:
			_flag(
				res,
				f"{where}: {doctype} {match} matched {how}, but {fieldname} '{value}' points to {others[0]}. "
				"Check it.",
			)


def _match_link(
	doctype: str,
	value: str,
	settings: Any,
	res: Resolution,
	*,
	source: Mapping[str, Any],
	sender: str,
	where: str,
) -> str | None:
	"""Return the name of the record that value resolves to, or None. Appends notes and flags to res."""
	if not frappe.db.exists("DocType", doctype):
		_flag(res, f"{where}: {doctype} is not a DocType. Left blank.")
		return None

	meta = frappe.get_meta(doctype)
	rule = _link_rule(settings, doctype)
	active = _active_filters(meta)

	# Step 1: exact name. A name that matches but disagrees with an identifier is flagged, and the name is kept.
	named = _names(doctype, {**active, "name": value}, limit=1)
	if named:
		_check_identifiers_agree(doctype, named[0], rule, source, active, res, where)
		return named[0]

	# Step 2: exact title, ignoring case. Several matches are ambiguous. An identifier that points elsewhere is flagged.
	titled = _title_names(doctype, meta, value, active)
	if len(titled) > 1:
		_ambiguous(res, where, doctype, f"'{value}'")
		return None
	if titled:
		_check_identifiers_agree(doctype, titled[0], rule, source, active, res, where, how="by title")
		return titled[0]

	# Step 3: identifier fields, each compared exactly. Records that differ across identifiers are ambiguous.
	hits = _identifier_hits(doctype, rule, source, active)
	for fieldname, ident_value, names in hits:
		if len(names) > 1:
			_ambiguous(res, where, doctype, f"{fieldname} '{ident_value}'")
			return None
	matched = [(fieldname, names[0]) for fieldname, _value, names in hits if names]
	if len({name for _fieldname, name in matched}) > 1:
		_flag(res, f"{where}: identifiers point to different {doctype} records. Pick one by hand.")
		return None
	if matched:
		fieldname, name = matched[0]
		_flag(res, f"{where}: {doctype} {name} matched by {fieldname}. Check it.")
		return name

	# Step 4: the Contact whose email is the sender, when the rule allows it.
	if rule.match_sender_contact and sender:
		parties = _contact_names(doctype, sender, active)
		if len(parties) > 1:
			_ambiguous(res, where, doctype, "the sender's Contact")
			return None
		if parties:
			_flag(res, f"{where}: {doctype} {parties[0]} matched by the sender's Contact. Check it.")
			return parties[0]

	# Step 5: fuzzy match on the title, when the rule allows it.
	if rule.fuzzy:
		match = _fuzzy_match(doctype, meta, value, active, settings)
		if match:
			_flag(res, f"{where}: {doctype} {match} fuzzy-matched from '{value}'. Check it.")
			return match

	# Step 6: auto-create, when the rule allows it. Step 7: otherwise the value is dropped.
	if rule.auto_create:
		return _auto_create(doctype, meta, rule, source, res, where=where, value=value)
	_flag(res, f"{where}: no {doctype} matches '{value}'. Left blank.")
	return None


def _resolve_link(
	docfield: Any,
	source: Mapping[str, Any],
	settings: Any,
	res: Resolution,
	*,
	sender: str,
	prefix: str,
) -> str | None:
	linked = docfield.options
	value = _text(source.get(docfield.fieldname))
	if not linked or not value:
		return None
	where = f"{prefix}{docfield.label or docfield.fieldname}"
	try:
		return _match_link(linked, value, settings, res, source=source, sender=sender, where=where)
	except _RuleError as exc:
		_flag(res, f"{where}: LlamaIndex Settings for {linked} is invalid ({exc}). Left blank.")
	except Exception as exc:
		_flag(res, f"{where} could not be resolved ({type(exc).__name__}). Left blank.")
	return None


def _resolve_fields(
	meta: Meta,
	values_source: Mapping[str, Any],
	link_source: Mapping[str, Any],
	settings: Any,
	res: Resolution,
	*,
	sender: str,
	prefix: str,
) -> dict[str, Any]:
	"""Copy the extracted values of the value fields, and resolve the Link fields from link_source."""
	values: dict[str, Any] = {}
	for docfield in meta.fields:
		if docfield.read_only:
			continue
		if docfield.fieldtype == "Link":
			linked_name = _resolve_link(docfield, link_source, settings, res, sender=sender, prefix=prefix)
			if linked_name:
				values[docfield.fieldname] = linked_name
		elif docfield.fieldtype in VALUE_FIELDTYPES:
			value = values_source.get(docfield.fieldname)
			if value is not None and value != "":
				values[docfield.fieldname] = value
	return values


def _resolve_rows(
	child_meta: Meta,
	rows: list[Any],
	parent: Mapping[str, Any],
	settings: Any,
	res: Resolution,
	*,
	sender: str,
) -> list[dict[str, Any]]:
	resolved: list[dict[str, Any]] = []
	for index, row in enumerate(rows, start=1):
		if not isinstance(row, Mapping):
			_flag(res, f"Line {index} was dropped: it is not an object.")
			continue
		try:
			# A Link value in a line comes from the line. The parent fills in only what the line leaves out.
			resolved.append(
				_resolve_fields(
					child_meta,
					row,
					ChainMap(row, parent),
					settings,
					res,
					sender=sender,
					prefix=f"Line {index}: ",
				)
			)
		except Exception as exc:
			_flag(res, f"Line {index} was dropped ({type(exc).__name__}). Review needed.")
	return resolved


def _resolve_into(
	target_doctype: str,
	extracted: Mapping[str, Any],
	settings: Any,
	res: Resolution,
	*,
	sender_email: str | None,
) -> None:
	if not frappe.db.exists("DocType", target_doctype):
		_flag(res, f"{target_doctype} is not a DocType. Nothing was resolved.")
		return

	meta = frappe.get_meta(target_doctype)
	sender = _sender_address(sender_email)
	res.values.update(_resolve_fields(meta, extracted, extracted, settings, res, sender=sender, prefix=""))

	for table in meta.get_table_fields():
		rows = extracted.get(table.fieldname) or []
		if not isinstance(rows, list):
			_flag(res, f"{table.label or table.fieldname} was dropped: expected a list of lines.")
			continue
		child_meta = frappe.get_meta(table.options)
		res.child_rows[table.fieldname] = _resolve_rows(
			child_meta, rows, extracted, settings, res, sender=sender
		)


def match_keys(target_doctype: str, settings: Any) -> dict[str, dict[str, dict]]:
	"""Return the extracted keys that the resolver reads to match linked records, by level.

	The key "" is the header level. Any other key is a Table fieldname of the target, and its inner dict holds the keys
	for that Table's rows. Each inner dict maps an extracted key to {"fieldtype", "label", "options"}, taken from the
	linked doctype field that the key fills. A header Link is read from the header. A child Link is read from its row
	first, and then from the header when the header holds the same key. Its key is requested at the row level only.

	Only a Link that is in the target schema at its level contributes keys, because the resolver matches a Link only when
	the Link's own value was extracted. For each such Link, the keys are the identifier fields of the linked doctype's
	rule. When the rule allows auto-create, they also include the required value fields that its default_values do not
	supply. A key already in the target schema at its level is left out. So is a key named like a writable field of that
	level, because the resolver would write the key's value into that field. Any key of a Table that is not extracted is
	left out too, because no rows arrive for it. A linked doctype whose rule JSON is invalid contributes no keys, and the
	resolver flags that rule when it reads the link. This reads metadata only and never writes.
	"""
	meta = frappe.get_meta(target_doctype)
	rules = getattr(settings, "doctype_rules", None) or []
	rule = next((row for row in rules if getattr(row, "target_doctype", None) == target_doctype), None)
	existing = schema_builder.target_properties(
		target_doctype,
		schema_builder.parse_included_fields(getattr(rule, "included_fields", None)),
		reconciliation_field=getattr(rule, "reconciliation_field", None) or None,
	)

	levels: dict[str, dict[str, dict]] = {"": {}}
	header_exclude = set(existing) | _writable_fieldnames(meta)
	for docfield in meta.fields:
		if docfield.fieldtype == "Link" and not docfield.read_only and docfield.fieldname in existing:
			_collect_link_keys(levels[""], docfield.options, settings, exclude=header_exclude)
		elif docfield.fieldtype == "Table" and docfield.fieldname in existing and docfield.options:
			row_properties = existing[docfield.fieldname]["items"]["properties"]
			child_meta = frappe.get_meta(docfield.options)
			row_exclude = set(row_properties) | _writable_fieldnames(child_meta)
			bucket = levels.setdefault(docfield.fieldname, {})
			for child_docfield in child_meta.fields:
				if (
					child_docfield.fieldtype == "Link"
					and not child_docfield.read_only
					and child_docfield.fieldname in row_properties
				):
					_collect_link_keys(bucket, child_docfield.options, settings, exclude=row_exclude)
	return {level: keys for level, keys in levels.items() if keys}


def _writable_fieldnames(meta: Meta) -> set[str]:
	"""The fieldnames of meta that are not read-only. The resolver can write a value into each of them."""
	return {docfield.fieldname for docfield in meta.fields if not docfield.read_only}


def _collect_link_keys(
	bucket: dict[str, dict],
	linked: str | None,
	settings: Any,
	*,
	exclude: set[str],
) -> None:
	"""Add to bucket the keys that resolving one Link to linked reads, except those in exclude or already in bucket."""
	if not linked:
		return
	try:
		rule = _link_rule(settings, linked)
	except _RuleError:
		return
	if not rule.identifier_fields and not rule.auto_create:
		return
	if not frappe.db.exists("DocType", linked):
		return

	linked_meta = frappe.get_meta(linked)
	fieldnames = list(rule.identifier_fields)
	if rule.auto_create:
		fieldnames += [
			docfield.fieldname
			for docfield in linked_meta.fields
			if docfield.reqd
			and docfield.fieldtype in VALUE_FIELDTYPES
			and _is_missing(docfield, rule.default_values.get(docfield.fieldname))
		]
	for fieldname in fieldnames:
		key = rule.extracted_keys.get(fieldname, fieldname)
		if key in exclude:
			continue
		spec = _key_spec(linked_meta, fieldname)
		if key not in bucket:
			bucket[key] = spec
		elif bucket[key]["fieldtype"] != spec["fieldtype"]:
			# Two linked fields read this key with different types, and one schema type cannot serve both. Text is used,
			# and each link compares the text with its own field.
			bucket[key] = {"fieldtype": "Data", "label": bucket[key]["label"], "options": None}


def _key_spec(linked_meta: Meta, fieldname: str) -> dict[str, Any]:
	"""Fieldtype, label and options of the linked field a key fills. A field that is not found has no fieldtype."""
	docfield = linked_meta.get_field(fieldname)
	if docfield is None:
		return {"fieldtype": None, "label": fieldname, "options": None}
	return {
		"fieldtype": docfield.fieldtype,
		"label": docfield.label or fieldname,
		"options": docfield.options,
	}
