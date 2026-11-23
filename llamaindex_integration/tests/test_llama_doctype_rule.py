# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for the Llama DocType Rule: the target, the duplicate key fields, the reconciliation field and the default
values. Drafts are inserted with ignore_permissions, so only submittable transaction DocTypes may be targets. Rules
are built with frappe.get_doc and validated in memory, so no rule rows are written. Tests that need a submittable
transaction DocType use ERPNext as a fixture, and they skip when ERPNext is not installed."""

import frappe
from frappe.tests import IntegrationTestCase


def _make_rule(
	target: str,
	duplicate_key_fields: str = "",
	included_fields: str = "",
	**values,
):
	return frappe.get_doc(
		{
			"doctype": "Llama DocType Rule",
			"target_doctype": target,
			"classification_description": "Vendor invoice or bill",
			"duplicate_key_fields": duplicate_key_fields,
			"included_fields": included_fields,
			**values,
		}
	)


def _make_settings(*rules: dict):
	"""An unsaved LlamaIndex Settings with the given rule rows. Only validate() runs, so nothing is written."""
	return frappe.get_doc(
		{
			"doctype": "LlamaIndex Settings",
			"allowed_extensions": "pdf",
			"doctype_rules": list(rules),
		}
	)


def _rule_row(**values):
	# Every key is set here, so the row does not depend on which fields the test site's DocType metadata has.
	row = {
		"doctype": "Llama DocType Rule",
		"classification_description": "Bill",
		"duplicate_key_fields": "",
		"included_fields": "",
	}
	return {**row, **values}


class _ERPNextTestCase(IntegrationTestCase):
	"""Base for tests that need a submittable transaction DocType, which only ERPNext provides here."""

	def setUp(self):
		if "erpnext" not in frappe.get_installed_apps():
			self.skipTest(
				"ERPNext is not installed, so there is no submittable transaction DocType to target"
			)
		super().setUp()


class TestLlamaDocTypeRuleTarget(IntegrationTestCase):
	def test_master_doctype_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_make_rule("User").validate()

	def test_non_submittable_doctype_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_make_rule("ToDo").validate()

	def test_non_submittable_target_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_make_rule("Note", "title").validate()

	def test_invalid_default_values_are_refused_even_for_a_blank_target(self):
		# A blank target returns before the submittable gate, so only the parse can refuse this row. User would fail
		# the gate with a different message, so it could not show the parse runs first.
		with self.assertRaisesRegex(frappe.ValidationError, "Default Values must be a JSON object"):
			_make_rule("", default_values="[1]").validate()


class TestLlamaDocTypeRuleFields(IntegrationTestCase):
	def test_duplicate_key_fields_is_optional_small_text(self):
		field = frappe.get_meta("Llama DocType Rule").get_field("duplicate_key_fields")
		self.assertEqual(field.fieldtype, "Small Text")
		self.assertFalse(field.reqd)

	def test_default_values_is_a_json_code_field(self):
		field = frappe.get_meta("Llama DocType Rule").get_field("default_values")
		self.assertEqual((field.fieldtype, field.options), ("Code", "JSON"))

	def test_reconciliation_field_is_optional_data(self):
		field = frappe.get_meta("Llama DocType Rule").get_field("reconciliation_field")
		self.assertEqual(field.fieldtype, "Data")
		self.assertFalse(field.reqd)


class TestLlamaDocTypeRuleDuplicateKeys(_ERPNextTestCase):
	def test_valid_parent_key_on_submittable_target_passes(self):
		_make_rule("Purchase Invoice", "supplier,bill_no").validate()

	def test_date_field_is_an_allowed_key(self):
		_make_rule("Purchase Invoice", "supplier,posting_date").validate()

	def test_blank_entries_and_whitespace_are_ignored(self):
		_make_rule("Purchase Invoice", " supplier , , bill_no ").validate()

	def test_blank_key_skips_the_check(self):
		_make_rule("Purchase Invoice", "").validate()

	def test_unknown_field_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "no_such_field.*Purchase Invoice"):
			_make_rule("Purchase Invoice", "supplier,no_such_field").validate()

	def test_child_table_field_is_refused(self):
		# item_code is a field of the Purchase Invoice Item child table, not a parent field.
		with self.assertRaisesRegex(frappe.ValidationError, "item_code.*Purchase Invoice"):
			_make_rule("Purchase Invoice", "item_code").validate()

	def test_table_field_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "items on Purchase Invoice has fieldtype Table"):
			_make_rule("Purchase Invoice", "items").validate()

	def test_read_only_key_is_refused(self):
		# grand_total is read only on Purchase Invoice, so extraction never fills it and the key would be blank.
		with self.assertRaisesRegex(frappe.ValidationError, "grand_total.*read only"):
			_make_rule("Purchase Invoice", "supplier,grand_total").validate()

	def test_key_without_search_index_is_refused(self):
		# company has no search index, and the key lookup would lock the whole table without one.
		with self.assertRaisesRegex(frappe.ValidationError, "company.*Search Index"):
			_make_rule("Purchase Invoice", "company").validate()

	def test_text_key_is_refused(self):
		# Frappe refuses a search index on Small Text, so a text key could never be indexed.
		with self.assertRaisesRegex(frappe.ValidationError, "remarks.*Small Text"):
			_make_rule("Purchase Invoice", "remarks").validate()

	def test_included_field_missing_from_the_target_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "no_such_field"):
			_make_rule("Purchase Invoice", included_fields="supplier, no_such_field").validate()

	def test_included_field_on_the_target_passes(self):
		_make_rule("Purchase Invoice", included_fields="supplier, bill_no").validate()


class TestLlamaDocTypeRuleDefaultValues(_ERPNextTestCase):
	def test_default_values_that_is_not_an_object_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_make_rule("Purchase Invoice", default_values='["remarks"]').validate()

	def test_default_values_that_is_invalid_json_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_make_rule("Purchase Invoice", default_values="{remarks").validate()

	def test_blank_default_values_pass(self):
		_make_rule("Purchase Invoice", default_values="").validate()

	def test_parent_key_passes(self):
		_make_rule("Purchase Invoice", default_values='{"remarks": "Imported"}').validate()

	def test_child_key_passes(self):
		_make_rule("Purchase Invoice", default_values='{"items.item_code": "STOCK"}').validate()

	def test_unknown_parent_key_is_refused(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "Default value key no_such_field is not a field of Purchase Invoice"
		):
			_make_rule("Purchase Invoice", default_values='{"no_such_field": 1}').validate()

	def test_table_key_is_refused(self):
		# items holds child rows, so a default cannot be set on the Table itself.
		with self.assertRaisesRegex(
			frappe.ValidationError, "Default value key items is not a field of Purchase Invoice"
		):
			_make_rule("Purchase Invoice", default_values='{"items": []}').validate()

	def test_unknown_child_key_is_refused(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "Default value key items.no_such_field is not a field of Purchase Invoice"
		):
			_make_rule("Purchase Invoice", default_values='{"items.no_such_field": 1}').validate()

	def test_child_key_on_a_non_table_field_is_refused(self):
		# supplier is a Link, not a Table, so it holds no child rows.
		with self.assertRaisesRegex(
			frappe.ValidationError, "Default value key supplier.item_code is not a field of Purchase Invoice"
		):
			_make_rule("Purchase Invoice", default_values='{"supplier.item_code": "X"}').validate()


class TestLlamaDocTypeRuleReconciliation(_ERPNextTestCase):
	def test_numeric_reconciliation_field_passes(self):
		# grand_total is a read-only Currency header field on Purchase Invoice, which is a valid total to check.
		_make_rule("Purchase Invoice", reconciliation_field="grand_total").validate()

	def test_writable_numeric_reconciliation_field_is_refused(self):
		# paid_amount is a writable Currency field. Its value would be copied from the extraction and then compared with
		# itself, so the check would never fail.
		with self.assertRaisesRegex(
			frappe.ValidationError, "Reconciliation Field paid_amount on Purchase Invoice must be a read-only"
		):
			_make_rule("Purchase Invoice", reconciliation_field="paid_amount").validate()

	def test_blank_reconciliation_field_passes(self):
		_make_rule("Purchase Invoice", reconciliation_field="").validate()

	def test_link_reconciliation_field_is_refused(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "Reconciliation Field supplier on Purchase Invoice has fieldtype Link"
		):
			_make_rule("Purchase Invoice", reconciliation_field="supplier").validate()

	def test_table_reconciliation_field_is_refused(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "Reconciliation Field items on Purchase Invoice has fieldtype Table"
		):
			_make_rule("Purchase Invoice", reconciliation_field="items").validate()

	def test_text_reconciliation_field_is_refused(self):
		with self.assertRaisesRegex(
			frappe.ValidationError,
			"Reconciliation Field remarks on Purchase Invoice has fieldtype Small Text",
		):
			_make_rule("Purchase Invoice", reconciliation_field="remarks").validate()

	def test_unknown_reconciliation_field_is_refused(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "Reconciliation Field no_such_field does not exist on Purchase Invoice"
		):
			_make_rule("Purchase Invoice", reconciliation_field="no_such_field").validate()


class TestLlamaIndexSettingsChecksItsRules(IntegrationTestCase):
	"""Frappe does not run validate() on child rows, so a bad rule must be refused by the Settings validate."""

	def test_master_target_row_is_refused_on_settings_save(self):
		settings = _make_settings(_rule_row(target_doctype="User"))
		with self.assertRaisesRegex(frappe.ValidationError, "User is not a submittable"):
			settings.validate()


class TestLlamaIndexSettingsChecksItsRulesWithERPNext(_ERPNextTestCase):
	def test_unknown_key_row_is_refused_on_settings_save(self):
		settings = _make_settings(
			_rule_row(target_doctype="Purchase Invoice", duplicate_key_fields="bill_nno")
		)
		with self.assertRaisesRegex(frappe.ValidationError, "bill_nno.*Purchase Invoice"):
			settings.validate()

	def test_table_key_row_is_refused_on_settings_save(self):
		settings = _make_settings(_rule_row(target_doctype="Purchase Invoice", duplicate_key_fields="items"))
		with self.assertRaisesRegex(frappe.ValidationError, "items on Purchase Invoice has fieldtype Table"):
			settings.validate()

	def test_bad_included_field_row_is_refused_on_settings_save(self):
		settings = _make_settings(
			_rule_row(target_doctype="Purchase Invoice", included_fields="no_such_field")
		)
		with self.assertRaisesRegex(frappe.ValidationError, "no_such_field"):
			settings.validate()

	def test_bad_default_values_row_is_refused_on_settings_save(self):
		settings = _make_settings(
			_rule_row(target_doctype="Purchase Invoice", default_values='{"no_such_field": 1}')
		)
		with self.assertRaisesRegex(frappe.ValidationError, "no_such_field is not a field"):
			settings.validate()

	def test_valid_rows_pass_on_settings_save(self):
		settings = _make_settings(
			_rule_row(
				target_doctype="Purchase Invoice",
				duplicate_key_fields="supplier,bill_no",
				included_fields="supplier",
				default_values='{"remarks": "Imported"}',
				reconciliation_field="grand_total",
			)
		)
		settings.validate()
