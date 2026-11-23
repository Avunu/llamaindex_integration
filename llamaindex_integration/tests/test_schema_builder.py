# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for services/schema_builder.py (spec section 8, item 1) and the LlamaIndex Settings validate().

The pure helpers and the fake-metadata cases are site-free. The Note and ToDo cases read the real DocType metadata.
The Purchase Invoice cases are skipped when ERPNext is not installed on the test site. The settings validation cases
call the real frappe.throw, so they need the test site.
"""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from llamaindex_integration.llamaindex_integration.doctype.llamaindex_settings.llamaindex_settings import (
	LlamaIndexSettings,
)
from llamaindex_integration.services.schema_builder import (
	MATCH_KEY_DESCRIPTION,
	RECONCILIATION_DESCRIPTION,
	SchemaError,
	build_json_schema_for_doctype,
	fieldtype_to_schema,
	parse_included_fields,
)


class TestParseIncludedFields(unittest.TestCase):
	def test_blank_values_return_none(self):
		for raw in (None, "", "   ", " , \n,"):
			with self.subTest(raw=raw):
				self.assertIsNone(parse_included_fields(raw))

	def test_splits_on_commas_and_newlines_and_strips_whitespace(self):
		self.assertEqual(
			parse_included_fields(" supplier, bill_no ,\nitems "),
			["supplier", "bill_no", "items"],
		)

	def test_duplicates_are_removed_and_order_is_kept(self):
		self.assertEqual(parse_included_fields("items,supplier,items"), ["items", "supplier"])


class TestFieldtypeToSchema(unittest.TestCase):
	def test_text_and_link_map_to_string_with_label_description(self):
		for fieldtype in ("Data", "Small Text", "Text", "Link"):
			with self.subTest(fieldtype=fieldtype):
				self.assertEqual(
					fieldtype_to_schema(fieldtype, "Supplier", "Supplier"),
					{"type": "string", "description": "Supplier"},
				)

	def test_select_with_options_adds_options_to_description(self):
		self.assertEqual(
			fieldtype_to_schema("Select", "Mode", "Cash\nCard"),
			{"type": "string", "description": "Mode (Options: Cash\nCard)"},
		)

	def test_select_without_options_uses_label_only(self):
		for options in (None, ""):
			with self.subTest(options=options):
				self.assertEqual(
					fieldtype_to_schema("Select", "Mode", options),
					{"type": "string", "description": "Mode"},
				)

	def test_numeric_and_check_types(self):
		self.assertEqual(fieldtype_to_schema("Int", "Qty", None), {"type": "integer"})
		for fieldtype in ("Float", "Currency", "Percent"):
			with self.subTest(fieldtype=fieldtype):
				self.assertEqual(fieldtype_to_schema(fieldtype, "Amount", "currency"), {"type": "number"})
		self.assertEqual(fieldtype_to_schema("Check", "Paid", None), {"type": "boolean"})

	def test_date_uses_iso_format(self):
		self.assertEqual(
			fieldtype_to_schema("Date", "Posting Date", None),
			{"type": "string", "format": "date", "description": "YYYY-MM-DD"},
		)

	def test_unsupported_fieldtypes_return_none(self):
		unsupported = (
			"Section Break",
			"Column Break",
			"Tab Break",
			"HTML",
			"Button",
			"Fold",
			"Dynamic Link",
			"Attach",
			"Attach Image",
			"Password",
			"Table",
		)
		for fieldtype in unsupported:
			with self.subTest(fieldtype=fieldtype):
				self.assertIsNone(fieldtype_to_schema(fieldtype, "Label", "Options"))

	def test_long_text_text_editor_datetime_and_long_int_are_extracted(self):
		# The ToDo pipeline extracts its description, a Text Editor, and the resolver copies these value fieldtypes.
		self.assertEqual(
			fieldtype_to_schema("Long Text", "Notes", None), {"type": "string", "description": "Notes"}
		)
		self.assertEqual(
			fieldtype_to_schema("Text Editor", "Description", None),
			{"type": "string", "description": "Description"},
		)
		self.assertEqual(
			fieldtype_to_schema("Datetime", "Due", None),
			{"type": "string", "format": "date-time", "description": "YYYY-MM-DD HH:MM:SS"},
		)
		self.assertEqual(fieldtype_to_schema("Long Int", "Pages", None), {"type": "integer"})


def _field(fieldname, fieldtype="Data", *, label=None, options=None, reqd=0, read_only=0):
	return SimpleNamespace(
		fieldname=fieldname,
		fieldtype=fieldtype,
		label=label or fieldname,
		options=options,
		reqd=reqd,
		read_only=read_only,
	)


def _meta(*fields, submittable=0):
	return SimpleNamespace(fields=list(fields), is_submittable=submittable)


# Fake DocTypes. Each one covers a rule of the default selection: required fields only, a required Table with its own
# required columns, a Table that is not required, the system fields that are always excluded, the workflow fields that
# are excluded for a submittable target only, and the fieldtypes that are never extracted.
FAKE_METAS = {
	"Fake Submittable": _meta(
		_field("supplier", "Link", reqd=1, options="Supplier"),
		_field("posting_date", "Date", reqd=1),
		_field("amount", "Currency", reqd=1),
		_field("bill_no"),
		_field("total", "Currency", read_only=1),
		_field("status", "Select", reqd=1, options="Draft\nSubmitted"),
		_field("docstatus", "Int", reqd=1),
		_field("amended_from", "Link", reqd=1, options="Fake Submittable"),
		_field("naming_series", "Select", reqd=1, options="FS-.####"),
		_field("attachment", "Attach", reqd=1),
		_field("secret", "Password", reqd=1),
		_field("linked", "Dynamic Link", reqd=1, options="linked_doctype"),
		_field("action", "Button", reqd=1),
		_field("layout", "Section Break", reqd=1),
		_field("lines", "Table", reqd=1, options="Fake Line"),
		_field("extras", "Table", options="Fake Line"),
		submittable=1,
	),
	"Fake Plain": _meta(
		_field("supplier", "Link", reqd=1, options="Supplier"),
		_field("status", "Select", reqd=1, options="Open\nClosed"),
		_field("amended_from", "Link", reqd=1, options="Fake Plain"),
		_field("amount", "Currency"),
		_field("total", "Currency", read_only=1),
		_field("pages", "Long Int"),
	),
	"Fake Line": _meta(
		_field("item_code", "Link", reqd=1, options="Item"),
		_field("item_name"),
		_field("qty", "Float", reqd=1),
		_field("rate", "Currency", reqd=1),
		_field("note"),
		_field("amount", "Currency", read_only=1),
		_field("sub_lines", "Table", reqd=1, options="Fake Sub Line"),
	),
	"Fake Sub Line": _meta(
		_field("code", "Data", reqd=1),
		_field("detail"),
		_field("deeper", "Table", reqd=1, options="Fake Leaf"),
	),
	"Fake Leaf": _meta(_field("x", "Data", reqd=1)),
}


def _fake_get_meta(doctype, *args, **kwargs):
	return FAKE_METAS[doctype]


class TestFakeMetaSchemas(unittest.TestCase):
	def setUp(self):
		self.enterContext(mock.patch("frappe.get_meta", side_effect=_fake_get_meta))

	def test_default_selection_keeps_the_required_fields_of_a_submittable_target(self):
		schema = build_json_schema_for_doctype("Fake Submittable")
		self.assertEqual(set(schema["properties"]), {"supplier", "posting_date", "amount", "lines"})
		json.dumps(schema)

	def test_default_selection_includes_a_required_table_with_its_required_columns(self):
		# No included_fields: the required Table lines is an array of objects, with its own required columns. The
		# required sub_lines Table is one level down, and its required deeper Table is past MAX_TABLE_DEPTH.
		props = build_json_schema_for_doctype("Fake Submittable")["properties"]
		self.assertEqual(props["lines"]["type"], "array")
		row_props = props["lines"]["items"]["properties"]
		self.assertEqual(set(row_props), {"item_code", "qty", "rate", "sub_lines"})
		self.assertEqual(set(row_props["sub_lines"]["items"]["properties"]), {"code"})

	def test_default_selection_leaves_out_a_table_that_is_not_required(self):
		props = build_json_schema_for_doctype("Fake Submittable")["properties"]
		self.assertNotIn("extras", props)

	def test_long_int_reconciliation_field_is_an_integer_property(self):
		# The rule validator accepts Long Int as a reconciliation field, so the schema must extract it.
		props = build_json_schema_for_doctype("Fake Plain", reconciliation_field="pages")["properties"]
		self.assertEqual(props["pages"], {"type": "integer", "description": RECONCILIATION_DESCRIPTION})

	def test_status_and_docstatus_are_excluded_only_for_submittable_targets(self):
		self.assertEqual(
			set(build_json_schema_for_doctype("Fake Plain")["properties"]), {"supplier", "status"}
		)
		self.assertEqual(
			set(build_json_schema_for_doctype("Fake Plain", ["status"])["properties"]), {"status"}
		)
		self.assertEqual(
			set(
				build_json_schema_for_doctype("Fake Submittable", ["supplier", "status", "docstatus"])[
					"properties"
				]
			),
			{"supplier"},
		)

	def test_amended_from_and_naming_series_are_always_excluded(self):
		props = build_json_schema_for_doctype("Fake Plain", ["supplier", "amended_from"])["properties"]
		self.assertEqual(set(props), {"supplier"})
		props = build_json_schema_for_doctype("Fake Submittable", ["supplier", "naming_series"])["properties"]
		self.assertEqual(set(props), {"supplier"})

	def test_listed_fields_restrict_header_and_listed_table_columns(self):
		props = build_json_schema_for_doctype("Fake Submittable", ["supplier", "lines", "item_code", "qty"])[
			"properties"
		]
		self.assertEqual(set(props), {"supplier", "lines"})
		self.assertEqual(set(props["lines"]["items"]["properties"]), {"item_code", "qty"})

	def test_listed_table_without_listed_columns_gets_its_required_columns(self):
		props = build_json_schema_for_doctype("Fake Submittable", ["lines"])["properties"]
		self.assertEqual(set(props), {"lines"})
		self.assertEqual(
			set(props["lines"]["items"]["properties"]), {"item_code", "qty", "rate", "sub_lines"}
		)

	def test_table_recursion_stops_at_depth_two(self):
		props = build_json_schema_for_doctype("Fake Submittable", ["lines", "sub_lines", "deeper", "code"])[
			"properties"
		]
		row_props = props["lines"]["items"]["properties"]
		self.assertEqual(set(row_props), {"sub_lines"})
		self.assertEqual(set(row_props["sub_lines"]["items"]["properties"]), {"code"})

	def test_listed_read_only_and_system_fields_are_dropped(self):
		props = build_json_schema_for_doctype(
			"Fake Submittable", ["supplier", "total", "status", "amended_from"]
		)["properties"]
		self.assertEqual(set(props), {"supplier"})

	def test_row_field_without_listed_table_is_rejected(self):
		with self.assertRaises(SchemaError):
			build_json_schema_for_doctype("Fake Submittable", ["supplier", "item_code"])

	def test_unknown_listed_field_is_rejected(self):
		with self.assertRaises(SchemaError):
			build_json_schema_for_doctype("Fake Submittable", ["supplier", "no_such_field"])

	def test_only_read_only_fields_leave_an_empty_schema(self):
		with self.assertRaises(SchemaError):
			build_json_schema_for_doctype("Fake Plain", ["total"])

	def test_empty_list_means_the_default_set(self):
		self.assertEqual(
			build_json_schema_for_doctype("Fake Submittable", []),
			build_json_schema_for_doctype("Fake Submittable"),
		)

	def test_numeric_reconciliation_field_is_added_for_reconciliation_only(self):
		props = build_json_schema_for_doctype("Fake Plain", reconciliation_field="amount")["properties"]
		self.assertEqual(set(props), {"supplier", "status", "amount"})
		self.assertEqual(props["amount"], {"type": "number", "description": RECONCILIATION_DESCRIPTION})
		self.assertIn("reconciliation", RECONCILIATION_DESCRIPTION)

	def test_read_only_numeric_reconciliation_field_is_added(self):
		props = build_json_schema_for_doctype("Fake Plain", reconciliation_field="total")["properties"]
		self.assertEqual(props["total"]["description"], RECONCILIATION_DESCRIPTION)

	def test_reconciliation_field_is_added_next_to_listed_fields(self):
		props = build_json_schema_for_doctype("Fake Plain", ["supplier"], reconciliation_field="amount")[
			"properties"
		]
		self.assertEqual(set(props), {"supplier", "amount"})
		self.assertEqual(props["amount"]["description"], RECONCILIATION_DESCRIPTION)

	def test_missing_or_non_numeric_reconciliation_field_is_ignored(self):
		default = build_json_schema_for_doctype("Fake Plain")["properties"]
		for fieldname in ("supplier", "status", "no_such_field", "item_code"):
			with self.subTest(fieldname=fieldname):
				props = build_json_schema_for_doctype("Fake Plain", reconciliation_field=fieldname)[
					"properties"
				]
				self.assertEqual(props, default)

	def test_no_reconciliation_field_adds_nothing(self):
		props = build_json_schema_for_doctype("Fake Plain", reconciliation_field=None)["properties"]
		self.assertNotIn("amount", props)
		self.assertNotIn("total", props)

	def test_schema_with_a_reconciliation_field_is_json_serialisable(self):
		json.dumps(build_json_schema_for_doctype("Fake Submittable", reconciliation_field="amount"))

	def test_header_match_key_is_added_at_the_header(self):
		spec = {"fieldtype": "Data", "label": "Login Name", "options": None}
		props = build_json_schema_for_doctype("Fake Plain", match_keys={"": {"login_name": spec}})[
			"properties"
		]
		self.assertEqual(
			props["login_name"],
			{"type": "string", "description": MATCH_KEY_DESCRIPTION.format(label="Login Name")},
		)
		self.assertEqual(set(props), {"supplier", "status", "login_name"})

	def test_child_match_key_is_added_to_the_row_schema_and_not_the_header(self):
		# lines is listed with no columns, so it gets its required columns, and the match key joins them in the row.
		spec = {"fieldtype": "Data", "label": "Part Name", "options": None}
		props = build_json_schema_for_doctype(
			"Fake Submittable", ["lines"], match_keys={"lines": {"part_name": spec}}
		)["properties"]
		self.assertEqual(set(props), {"lines"})
		self.assertEqual(
			set(props["lines"]["items"]["properties"]),
			{"item_code", "qty", "rate", "sub_lines", "part_name"},
		)

	def test_match_key_already_in_the_schema_is_not_duplicated(self):
		default = build_json_schema_for_doctype("Fake Plain")["properties"]
		spec = {"fieldtype": "Data", "label": "Other Label", "options": None}
		props = build_json_schema_for_doctype("Fake Plain", match_keys={"": {"supplier": spec}})["properties"]
		self.assertEqual(props["supplier"], default["supplier"])
		self.assertEqual(set(props), set(default))

	def test_child_match_key_already_in_the_row_is_not_duplicated(self):
		default = build_json_schema_for_doctype("Fake Submittable", ["lines"])["properties"]
		spec = {"fieldtype": "Currency", "label": "Rate", "options": None}
		props = build_json_schema_for_doctype(
			"Fake Submittable", ["lines"], match_keys={"lines": {"rate": spec}}
		)["properties"]
		self.assertEqual(props["lines"], default["lines"])

	def test_match_key_types_follow_the_fieldtype_and_fall_back_to_string(self):
		match_keys = {
			"": {
				"count_key": {"fieldtype": "Int", "label": "Count", "options": None},
				"date_key": {"fieldtype": "Date", "label": "Date", "options": None},
				"unknown_key": {"fieldtype": None, "label": "Unknown", "options": None},
			}
		}
		props = build_json_schema_for_doctype("Fake Plain", match_keys=match_keys)["properties"]
		self.assertEqual(
			props["count_key"],
			{"type": "integer", "description": MATCH_KEY_DESCRIPTION.format(label="Count")},
		)
		self.assertEqual(props["date_key"]["type"], "string")
		self.assertEqual(props["date_key"]["format"], "date")
		self.assertEqual(
			props["unknown_key"],
			{"type": "string", "description": MATCH_KEY_DESCRIPTION.format(label="Unknown")},
		)

	def test_match_key_for_a_table_that_is_not_extracted_is_ignored(self):
		spec = {"fieldtype": "Data", "label": "Code", "options": None}
		props = build_json_schema_for_doctype("Fake Submittable", match_keys={"extras": {"code_key": spec}})[
			"properties"
		]
		self.assertNotIn("extras", props)
		self.assertNotIn("code_key", props)

	def test_match_key_below_the_first_table_level_is_ignored(self):
		# sub_lines is a Table inside lines. Keys are added only at the header and in the rows of a header Table.
		spec = {"fieldtype": "Data", "label": "Code", "options": None}
		props = build_json_schema_for_doctype(
			"Fake Submittable", match_keys={"sub_lines": {"code_key": spec}}
		)["properties"]
		self.assertNotIn("code_key", props)
		self.assertNotIn("code_key", props["lines"]["items"]["properties"])
		self.assertNotIn("sub_lines", props)

	def test_no_match_keys_leaves_the_schema_unchanged(self):
		default = build_json_schema_for_doctype("Fake Plain")
		self.assertEqual(build_json_schema_for_doctype("Fake Plain", match_keys={}), default)
		self.assertEqual(build_json_schema_for_doctype("Fake Plain", match_keys=None), default)


class TestNoteAndToDoSchema(IntegrationTestCase):
	"""Note has no status, no numeric field and one required field, so it shows the default and the listed selection."""

	def test_default_schema_is_the_required_title_only(self):
		props = build_json_schema_for_doctype("Note")["properties"]
		self.assertEqual(set(props), {"title"})
		self.assertEqual(props["title"], {"type": "string", "description": "Title"})

	def test_listed_fields_are_the_only_fields_extracted(self):
		props = build_json_schema_for_doctype("Note", ["public", "title"])["properties"]
		self.assertEqual(
			props,
			{
				"public": {"type": "boolean"},
				"title": {"type": "string", "description": "Title"},
			},
		)

	def test_listed_table_on_note_is_extracted_through_its_listed_column(self):
		props = build_json_schema_for_doctype("Note", ["seen_by", "user"])["properties"]
		self.assertEqual(set(props), {"seen_by"})
		self.assertEqual(set(props["seen_by"]["items"]["properties"]), {"user"})

	def test_listed_unsupported_fields_leave_nothing_to_extract(self):
		# seen_by_section is a Section Break, which is never extracted.
		with self.assertRaises(SchemaError):
			build_json_schema_for_doctype("Note", ["seen_by_section"])

	def test_non_numeric_reconciliation_field_on_note_is_ignored(self):
		props = build_json_schema_for_doctype("Note", reconciliation_field="title")["properties"]
		self.assertEqual(set(props), {"title"})
		self.assertEqual(props["title"], {"type": "string", "description": "Title"})

	def test_status_is_kept_on_a_target_that_is_not_submittable(self):
		props = build_json_schema_for_doctype("ToDo", ["status"])["properties"]
		self.assertEqual(set(props), {"status"})
		self.assertEqual(props["status"]["type"], "string")

	def test_match_key_keeps_the_select_options_and_the_date_format(self):
		props = build_json_schema_for_doctype(
			"ToDo",
			["description"],
			match_keys={
				"": {
					"level": {"fieldtype": "Select", "label": "Priority", "options": "Low\nHigh"},
					"day": {"fieldtype": "Date", "label": "Due", "options": None},
				}
			},
		)["properties"]
		self.assertIn("Low\nHigh", props["level"]["description"])
		self.assertIn("Used to match a record", props["level"]["description"])
		self.assertIn("YYYY-MM-DD", props["day"]["description"])
		self.assertEqual(props["day"]["format"], "date")

	def test_unknown_doctype_raises(self):
		with self.assertRaises(frappe.DoesNotExistError):
			build_json_schema_for_doctype("Llama Schema Test Missing Doctype")


class TestPurchaseInvoiceSchema(IntegrationTestCase):
	"""Purchase Invoice is the real ERPNext target for the Table and totals rules. Skipped without ERPNext."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise unittest.SkipTest("ERPNext is not installed on the test site")

	def test_default_schema_includes_the_required_items_table(self):
		# items is required on Purchase Invoice, so it is extracted with no included_fields.
		schema = build_json_schema_for_doctype("Purchase Invoice")
		self.assertEqual(schema["type"], "object")
		props = schema["properties"]
		self.assertEqual(props["items"]["type"], "array")
		self.assertTrue({"qty", "rate"} <= set(props["items"]["items"]["properties"]))
		self.assertNotIn("grand_total", props)
		self.assertNotIn("amended_from", props)
		json.dumps(schema)

	def test_listed_items_table_gets_only_listed_columns(self):
		props = build_json_schema_for_doctype(
			"Purchase Invoice", ["supplier", "bill_no", "items", "item_code", "qty"]
		)["properties"]
		self.assertEqual(set(props), {"supplier", "bill_no", "items"})
		self.assertEqual(set(props["items"]["items"]["properties"]), {"item_code", "qty"})

	def test_row_fields_without_listed_items_table_are_rejected(self):
		with self.assertRaises(SchemaError):
			build_json_schema_for_doctype("Purchase Invoice", ["supplier", "item_code", "qty"])

	def test_unknown_listed_field_is_rejected(self):
		with self.assertRaises(SchemaError):
			build_json_schema_for_doctype("Purchase Invoice", ["supplier", "no_such_field"])

	def test_grand_total_is_added_only_when_it_is_the_reconciliation_field(self):
		without = build_json_schema_for_doctype("Purchase Invoice", ["supplier"])["properties"]
		self.assertNotIn("grand_total", without)

		props = build_json_schema_for_doctype(
			"Purchase Invoice", ["supplier"], reconciliation_field="grand_total"
		)["properties"]
		self.assertEqual(set(props), {"supplier", "grand_total"})
		self.assertEqual(props["grand_total"], {"type": "number", "description": RECONCILIATION_DESCRIPTION})

	def test_status_is_excluded_on_a_submittable_target(self):
		props = build_json_schema_for_doctype("Purchase Invoice", ["supplier", "status"])["properties"]
		self.assertEqual(set(props), {"supplier"})


SETTINGS_DEFAULTS = {
	"enabled": 0,
	"fuzzy_match_threshold": 85,
	"min_classification_confidence": 0.8,
	"allowed_extensions": "pdf,png,jpg,jpeg,docx,xlsx",
	"doctype_rules": [],
}


def _rule_row(target):
	"""Return a Llama DocType Rule row as a dict. Settings.validate checks each row's target and keys."""
	return {"doctype": "Llama DocType Rule", "target_doctype": target}


ONE_RULE = [_rule_row("Purchase Invoice")]


def _settings(**values):
	"""Return an unsaved LlamaIndex Settings, built with frappe.get_doc so every field and child table is set.

	Only validate() runs, so nothing is written.
	"""
	return frappe.get_doc({"doctype": "LlamaIndex Settings", **SETTINGS_DEFAULTS, **values})


def _with_api_key(value):
	"""Patch get_password so the LlamaCloud key reads as value, or as missing when value is None."""
	return mock.patch.object(LlamaIndexSettings, "get_password", return_value=value)


class TestLlamaIndexSettingsValidate(IntegrationTestCase):
	def test_fuzzy_match_threshold_accepts_zero_and_one_hundred(self):
		for threshold in (0, 100):
			with self.subTest(threshold=threshold):
				_settings(fuzzy_match_threshold=threshold).validate()

	def test_fuzzy_match_threshold_outside_zero_to_one_hundred_is_rejected(self):
		for threshold in (-1, 101):
			with self.subTest(threshold=threshold), self.assertRaises(frappe.ValidationError):
				_settings(fuzzy_match_threshold=threshold).validate()

	def test_min_classification_confidence_accepts_zero_and_one(self):
		for confidence in (0, 1, 0.8):
			with self.subTest(confidence=confidence):
				_settings(min_classification_confidence=confidence).validate()

	def test_min_classification_confidence_outside_zero_to_one_is_rejected(self):
		for confidence in (-0.01, 1.01):
			with self.subTest(confidence=confidence), self.assertRaises(frappe.ValidationError):
				_settings(min_classification_confidence=confidence).validate()

	def test_allowed_extensions_are_lowercased_and_stripped(self):
		doc = _settings(allowed_extensions=" PDF, .Png ,jpg ,,PDF")
		doc.validate()
		self.assertEqual(doc.allowed_extensions, "pdf,png,jpg")

	def test_allowed_extensions_keep_the_default_list_unchanged(self):
		doc = _settings()
		doc.validate()
		self.assertEqual(doc.allowed_extensions, "pdf,png,jpg,jpeg,docx,xlsx")

	def test_allowed_extensions_with_a_dot_or_space_inside_are_rejected(self):
		for raw in ("p.df", "pdf png", "pdf,a.b"):
			with self.subTest(raw=raw), self.assertRaises(frappe.ValidationError):
				_settings(allowed_extensions=raw).validate()

	def test_empty_allowed_extensions_are_rejected(self):
		for raw in (None, "", " , ,"):
			with self.subTest(raw=raw), self.assertRaises(frappe.ValidationError):
				_settings(allowed_extensions=raw).validate()

	def test_enabled_requires_the_api_key(self):
		with _with_api_key(None) as get_password, self.assertRaises(frappe.ValidationError) as ctx:
			_settings(enabled=1, doctype_rules=ONE_RULE).validate()
		get_password.assert_called_once_with("llama_cloud_api_key", raise_exception=False)
		self.assertIn("API Key", str(ctx.exception))

	def test_enabled_requires_at_least_one_doctype_rule(self):
		with _with_api_key("test-key"), self.assertRaises(frappe.ValidationError) as ctx:
			_settings(enabled=1, doctype_rules=[]).validate()
		self.assertIn("Document Rule", str(ctx.exception))

	def test_enabled_with_key_and_a_rule_passes(self):
		with _with_api_key("test-key"):
			_settings(enabled=1, doctype_rules=ONE_RULE).validate()

	def test_disabled_skips_the_key_and_rule_requirements(self):
		with _with_api_key(None):
			_settings(enabled=0, doctype_rules=[]).validate()

	def test_error_messages_never_contain_the_api_key(self):
		secret = "test-key"
		with _with_api_key(secret), self.assertRaises(frappe.ValidationError) as ctx:
			_settings(enabled=1, doctype_rules=[]).validate()
		self.assertNotIn(secret, str(ctx.exception))
		with _with_api_key(secret), self.assertRaises(frappe.ValidationError) as ctx:
			_settings(enabled=1, doctype_rules=ONE_RULE, fuzzy_match_threshold=101).validate()
		self.assertNotIn(secret, str(ctx.exception))
