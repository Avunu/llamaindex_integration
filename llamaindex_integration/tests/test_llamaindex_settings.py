# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for LlamaIndex Settings and its Llama Entity Match rows. Validation runs in memory, so nothing is written.
The save test writes the Single, and the test transaction rolls that write back. Tests that need a submittable
transaction DocType use ERPNext as a fixture, and they skip when ERPNext is not installed. The entity match tests use
Note and ToDo, which are core Frappe DocTypes, so they run without ERPNext."""

import frappe
from frappe.tests import IntegrationTestCase


def _make_settings(**values):
	"""An unsaved LlamaIndex Settings. Only validate() runs, so nothing is written."""
	return frappe.get_doc({"doctype": "LlamaIndex Settings", "allowed_extensions": "pdf", **values})


def _make_match(**values):
	"""An unsaved Llama Entity Match row. Only validate_entity_match() runs, so nothing is written."""
	return frappe.get_doc({"doctype": "Llama Entity Match", **values})


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


class TestLlamaIndexSettingsValidate(IntegrationTestCase):
	def test_threshold_above_100_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "between 0 and 100"):
			_make_settings(fuzzy_match_threshold=101).validate()

	def test_negative_threshold_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "between 0 and 100"):
			_make_settings(fuzzy_match_threshold=-1).validate()

	def test_enabled_app_without_key_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "LlamaCloud API Key"):
			_make_settings(enabled=1).validate()

	def test_enabled_app_without_rule_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "at least one Document Rule"):
			_make_settings(enabled=1, llama_cloud_api_key="test-key").validate()

	def test_disabled_app_without_key_passes(self):
		_make_settings().validate()

	def test_duplicate_entity_match_row_is_refused(self):
		settings = _make_settings(entity_matches=[{"link_doctype": "Note"}, {"link_doctype": "Note"}])
		with self.assertRaisesRegex(frappe.ValidationError, "more than one Entity Match row"):
			settings.validate()

	def test_invalid_entity_match_row_is_refused_on_settings_save(self):
		# Frappe does not run validate() on child rows, so the Settings validate must reach the row itself.
		settings = _make_settings(entity_matches=[{"link_doctype": "ToDo", "fuzzy": 1}])
		with self.assertRaisesRegex(frappe.ValidationError, "description is Text Editor"):
			settings.validate()


class TestLlamaIndexSettingsWithERPNext(_ERPNextTestCase):
	def test_unknown_default_values_key_on_a_rule_row_is_refused(self):
		settings = _make_settings(
			doctype_rules=[
				_rule_row(target_doctype="Purchase Invoice", default_values='{"no_such_field": 1}')
			]
		)
		with self.assertRaisesRegex(
			frappe.ValidationError, "Default value key no_such_field is not a field of Purchase Invoice"
		):
			settings.validate()

	def test_enabled_app_with_key_and_rule_passes(self):
		settings = _make_settings(
			enabled=1,
			llama_cloud_api_key="test-key",
			doctype_rules=[_rule_row(target_doctype="Purchase Invoice")],
		)
		settings.validate()


class TestLlamaEntityMatchValidate(IntegrationTestCase):
	def test_unknown_link_doctype_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "No Such Llama DocType is not a DocType"):
			_make_match(link_doctype="No Such Llama DocType").validate()

	def test_blank_link_doctype_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Choose the DocType"):
			_make_match().validate()

	def test_child_table_link_doctype_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Has Role is a child table or a single"):
			_make_match(link_doctype="Has Role").validate()

	def test_identifier_without_index_is_refused(self):
		# title on Note has no index, so an exact lookup on it would scan the whole table.
		with self.assertRaisesRegex(
			frappe.ValidationError, "Identifier field title on Note needs Search Index"
		):
			_make_match(link_doctype="Note", identifier_fields="title").validate()

	def test_unknown_identifier_is_refused(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "Identifier field no_such_field does not exist on Note"
		):
			_make_match(link_doctype="Note", identifier_fields="no_such_field").validate()

	def test_indexed_identifier_passes(self):
		_make_match(link_doctype="Note", identifier_fields="expire_notification_on").validate()

	def test_fuzzy_on_a_text_editor_title_is_refused(self):
		# The title of ToDo is description, a Text Editor, so fuzzy matching would compare whole paragraphs.
		with self.assertRaisesRegex(frappe.ValidationError, "title field description is Text Editor"):
			_make_match(link_doctype="ToDo", fuzzy=1).validate()

	def test_fuzzy_on_a_short_title_passes(self):
		_make_match(link_doctype="Note", fuzzy=1).validate()

	def test_field_map_value_that_is_not_a_field_is_refused(self):
		# The value is the linked field, so it must be a field of Note.
		with self.assertRaisesRegex(
			frappe.ValidationError, "Field Map value no_such_field is not a field of Note"
		):
			_make_match(link_doctype="Note", field_map='{"vendor": "no_such_field"}').validate()

	def test_field_map_key_is_an_extracted_key_and_needs_no_field_of_the_linked_doctype(self):
		_make_match(link_doctype="Note", field_map='{"vendor_name": "title"}').validate()

	def test_field_map_with_the_linked_field_as_its_key_is_refused(self):
		# Reversed, the value would be an extracted key. is_public is not a field of Note, so the row is refused.
		with self.assertRaisesRegex(
			frappe.ValidationError, "Field Map value is_public is not a field of Note"
		):
			_make_match(link_doctype="Note", field_map='{"public": "is_public"}').validate()

	def test_field_map_key_with_spaces_around_it_is_refused(self):
		# The extracted key is matched exactly, so " vendor_name " would never match the extracted field.
		with self.assertRaisesRegex(frappe.ValidationError, "has spaces around it"):
			_make_match(link_doctype="Note", field_map='{" vendor_name ": "title"}').validate()

	def test_two_keys_filling_one_linked_field_are_refused(self):
		# Two keys for one field would make the later key silently win.
		with self.assertRaisesRegex(frappe.ValidationError, "more than one key"):
			_make_match(
				link_doctype="Note", field_map='{"vendor_name": "title", "vendor": "title"}'
			).validate()

	def test_field_map_that_is_not_an_object_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Field Map must be a JSON object"):
			_make_match(link_doctype="Note", field_map='["title"]').validate()

	def test_default_values_that_is_invalid_json_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Default Values must be a JSON object"):
			_make_match(link_doctype="Note", default_values="{public").validate()

	def test_default_values_key_that_is_not_a_field_is_refused(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "Default Values key no_such_field is not a field of Note"
		):
			_make_match(link_doctype="Note", default_values='{"no_such_field": 1}').validate()

	def test_valid_row_passes(self):
		_make_match(
			link_doctype="Note",
			identifier_fields="expire_notification_on",
			fuzzy=1,
			match_sender_contact=1,
			auto_create=1,
			field_map='{"is_public": "public"}',
			default_values='{"public": 1}',
		).validate()


class TestLlamaIndexSettingsSave(IntegrationTestCase):
	def test_entity_match_for_note_saves_without_erpnext(self):
		settings = frappe.get_single("LlamaIndex Settings")
		# The Single is shared, and an earlier test can leave a rule on it. A save validates every row, so start with none.
		settings.enabled = 0
		settings.doctype_rules = []
		settings.entity_matches = []
		settings.llama_cloud_api_key = "test-key"
		settings.monitored_email_account = self._make_inbox()
		settings.allowed_extensions = "pdf"
		settings.append("entity_matches", {"link_doctype": "Note", "fuzzy": 1, "match_sender_contact": 1})
		settings.save()

		saved = frappe.get_single("LlamaIndex Settings")
		self.assertEqual([row.link_doctype for row in saved.entity_matches], ["Note"])

	def _make_inbox(self) -> str:
		suffix = frappe.generate_hash(length=8)
		account = frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_account_name": f"Llama Settings Inbox {suffix}",
				"email_id": f"llama-settings-{suffix}@example.com",
			}
		).insert(ignore_permissions=True)
		return account.name
