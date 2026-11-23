# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for services/entity_resolver.py (spec section 8, item 2).

Covered: exact name, exact title ignoring case, identifiers through field_map with a flag when the name and an
identifier disagree, the sender's Contact only when the rule allows it, fuzzy matching only when allowed and only
on a title that is not prose, the row cap, auto-create with default_values, auto-create refusing when a required
value is missing, the default with no rule, and a child Link read from its line before the parent.

The fixtures are two Users, a Contact on them, and the core doctypes that link to User and Role: ToDo and Note as
the target doctypes. Nothing links to Note or ToDo, so ToDo is used only as the target and its Text Editor title is
checked through _fuzzy_title_allowed. Pure best_fuzzy_match and parse_fieldnames cases use plain unittest. The
ERPNext Supplier case is skipped when Supplier or Purchase Invoice is absent. TestMatchKeys covers match_keys on a fake
target, with real Note, ToDo and Role as the linked doctypes. Savepoint rollback is owned by tasks.py and is tested in
test_pipeline.py.
"""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.core.doctype.role.role import Role
from frappe.tests import IntegrationTestCase
from rapidfuzz import fuzz

from llamaindex_integration.services import entity_resolver
from llamaindex_integration.services.entity_resolver import (
	FUZZY_ROW_CAP,
	Resolution,
	_fuzzy_title_allowed,
	_match_link,
	best_fuzzy_match,
	match_keys,
	parse_fieldnames,
	resolve_entities,
)

PERSON_EMAIL = "resolver.person@example.com"
PERSON_TITLE = "Resolver Person Alpha"
PERSON_USERNAME = "resolver_person"
OTHER_EMAIL = "resolver.other@example.com"
TWIN_EMAILS = ("resolver.twin1@example.com", "resolver.twin2@example.com")
CONTACT_EMAIL = "resolver.billing@vendor.example.com"
SHARED_CONTACT_EMAIL = "resolver.shared@vendor.example.com"
SUPPLIER_GROUP = "Resolver Test Group"


class TestBestFuzzyMatch(unittest.TestCase):
	def test_returns_key_of_exact_match(self):
		choices = {"SUP-0001": "Acme Supplies", "SUP-0002": "Zenith Industries"}
		self.assertEqual(best_fuzzy_match("Acme Supplies", choices, 85), "SUP-0001")

	def test_match_ignores_case(self):
		self.assertEqual(best_fuzzy_match("ACME SUPPLIES", {"SUP-0001": "Acme Supplies"}, 85), "SUP-0001")

	def test_below_threshold_returns_none(self):
		self.assertIsNone(best_fuzzy_match("Acme", {"SUP-0002": "Zenith Industries"}, 85))

	def test_empty_choices_return_none(self):
		self.assertIsNone(best_fuzzy_match("Acme", {}, 85))

	def test_widget_10mm_does_not_match_widget_100mm(self):
		# The raw score clears the threshold, so only the digit-token rule rejects this match.
		self.assertGreaterEqual(fuzz.WRatio("Widget 10mm", "Widget 100mm"), 85)
		self.assertIsNone(best_fuzzy_match("Widget 10mm", {"ITEM-0100": "Widget 100mm"}, 85))

	def test_widget_10mm_matches_itself_not_widget_100mm(self):
		choices = {"ITEM-0010": "Widget 10mm", "ITEM-0100": "Widget 100mm"}
		self.assertEqual(best_fuzzy_match("Widget 10mm", choices, 85), "ITEM-0010")

	def test_warehouse_a_does_not_match_warehouse_b(self):
		# Both are above the threshold, so the single-letter token rule is what rejects this match.
		self.assertGreaterEqual(fuzz.WRatio("Warehouse A", "Warehouse B"), 85)
		self.assertIsNone(best_fuzzy_match("Warehouse A", {"WH-B": "Warehouse B"}, 85))

	def test_warehouse_a_matches_warehouse_a(self):
		choices = {"WH-A": "Warehouse A", "WH-B": "Warehouse B"}
		self.assertEqual(best_fuzzy_match("Warehouse A", choices, 85), "WH-A")


class TestParseFieldnames(unittest.TestCase):
	def test_strips_drops_blanks_and_keeps_order(self):
		self.assertEqual(parse_fieldnames(" tax_id, ,email ,,username "), ("tax_id", "email", "username"))

	def test_none_and_blank_give_no_fieldnames(self):
		self.assertEqual(parse_fieldnames(None), ())
		self.assertEqual(parse_fieldnames("  "), ())


def _settings(*rules):
	return SimpleNamespace(fuzzy_match_threshold=85, entity_matches=list(rules))


def _rule(link_doctype, **overrides):
	values = {
		"link_doctype": link_doctype,
		"identifier_fields": "",
		"fuzzy": 0,
		"auto_create": 0,
		"match_sender_contact": 0,
		"field_map": "",
		"default_values": "",
	}
	values.update(overrides)
	return SimpleNamespace(**values)


def _make_user(email: str, first_name: str, username: str) -> str:
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first_name,
			"username": username,
			"send_welcome_email": 0,
		}
	).insert(ignore_permissions=True)
	return user.name


def _make_contact(email: str, users: list[str]) -> str:
	"""A Contact with this email, Dynamic Linked to each User."""
	contact = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": "Resolver Test Contact",
			"email_ids": [{"email_id": email, "is_primary": 1}],
			"links": [{"link_doctype": "User", "link_name": user} for user in users],
		}
	).insert(ignore_permissions=True)
	return contact.name


class TestEntityResolver(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		# Fixtures are not committed, so IntegrationTestCase rolls them back when the class finishes.
		cls.person = _make_user(PERSON_EMAIL, PERSON_TITLE, PERSON_USERNAME)
		cls.other = _make_user(OTHER_EMAIL, "Resolver Other Beta", "resolver_other")
		for index, email in enumerate(TWIN_EMAILS, start=1):
			_make_user(email, "Resolver Twin Delta", f"resolver_twin{index}")
		_make_contact(CONTACT_EMAIL, [cls.person])
		_make_contact(SHARED_CONTACT_EMAIL, [cls.person, cls.other])

	def test_exact_name_match_needs_no_review(self):
		res = resolve_entities("ToDo", {"allocated_to": PERSON_EMAIL}, _settings())
		self.assertEqual(res.values.get("allocated_to"), self.person)
		self.assertFalse(res.needs_review)

	def test_exact_title_match_ignores_case(self):
		res = resolve_entities("ToDo", {"allocated_to": PERSON_TITLE.upper()}, _settings())
		self.assertEqual(res.values.get("allocated_to"), self.person)
		self.assertFalse(res.needs_review)

	def test_title_match_whose_identifier_points_elsewhere_is_flagged_and_title_is_kept(self):
		rules = _settings(
			_rule("User", identifier_fields="username", field_map=json.dumps({"login_name": "username"}))
		)
		res = resolve_entities("ToDo", {"allocated_to": PERSON_TITLE, "login_name": "resolver_other"}, rules)
		self.assertEqual(res.values.get("allocated_to"), self.person)
		self.assertTrue(res.needs_review)
		self.assertTrue(any("matched by title" in note and "points to" in note for note in res.notes))

	def test_title_match_that_agrees_with_its_identifier_needs_no_review(self):
		rules = _settings(
			_rule("User", identifier_fields="username", field_map=json.dumps({"login_name": "username"}))
		)
		res = resolve_entities("ToDo", {"allocated_to": PERSON_TITLE, "login_name": PERSON_USERNAME}, rules)
		self.assertEqual(res.values.get("allocated_to"), self.person)
		self.assertFalse(res.needs_review)

	def test_ambiguous_title_is_flagged_and_left_blank(self):
		res = resolve_entities("ToDo", {"allocated_to": "Resolver Twin Delta"}, _settings())
		self.assertNotIn("allocated_to", res.values)
		self.assertTrue(res.needs_review)
		self.assertTrue(any("several User records" in note for note in res.notes))

	def test_identifier_match_reads_the_extracted_key_from_field_map(self):
		rules = _settings(
			_rule("User", identifier_fields="username", field_map=json.dumps({"login_name": "username"}))
		)
		extracted = {"allocated_to": "Unknown Login Zz", "login_name": PERSON_USERNAME}
		res = resolve_entities("ToDo", extracted, rules)
		self.assertEqual(res.values.get("allocated_to"), self.person)
		self.assertTrue(res.needs_review)
		self.assertTrue(any("matched by username" in note for note in res.notes))

	def test_identifier_disagreeing_with_the_name_is_flagged_and_name_is_kept(self):
		rules = _settings(
			_rule("User", identifier_fields="username", field_map=json.dumps({"login_name": "username"}))
		)
		extracted = {"allocated_to": OTHER_EMAIL, "login_name": PERSON_USERNAME}
		res = resolve_entities("ToDo", extracted, rules)
		self.assertEqual(res.values.get("allocated_to"), self.other)
		self.assertTrue(res.needs_review)
		self.assertTrue(any("points to" in note for note in res.notes))

	def test_sender_contact_is_used_only_when_the_rule_allows_it(self):
		sender = f"Vendor Billing <{CONTACT_EMAIL.upper()}>"
		extracted = {"allocated_to": "Unrelated Person Qq"}

		res = resolve_entities(
			"ToDo", extracted, _settings(_rule("User", match_sender_contact=1)), sender_email=sender
		)
		self.assertEqual(res.values.get("allocated_to"), self.person)
		self.assertTrue(res.needs_review)
		self.assertTrue(any("sender's Contact" in note for note in res.notes))

		res = resolve_entities("ToDo", extracted, _settings(), sender_email=sender)
		self.assertNotIn("allocated_to", res.values)
		self.assertTrue(res.needs_review)

	def test_sender_contact_linked_to_two_users_is_ambiguous(self):
		rules = _settings(_rule("User", match_sender_contact=1))
		res = resolve_entities(
			"ToDo", {"allocated_to": "Unrelated Person Qq"}, rules, sender_email=SHARED_CONTACT_EMAIL
		)
		self.assertNotIn("allocated_to", res.values)
		self.assertTrue(res.needs_review)
		self.assertTrue(any("several User records" in note for note in res.notes))

	def test_fuzzy_match_is_used_only_when_the_rule_allows_it(self):
		extracted = {"allocated_to": "Resolver Person Alph"}

		res = resolve_entities("ToDo", extracted, _settings())
		self.assertNotIn("allocated_to", res.values)
		self.assertTrue(res.needs_review)

		res = resolve_entities("ToDo", extracted, _settings(_rule("User", fuzzy=1)))
		self.assertEqual(res.values.get("allocated_to"), self.person)
		self.assertTrue(res.needs_review)
		self.assertTrue(any("fuzzy-matched" in note for note in res.notes))

	def test_fuzzy_title_is_refused_when_the_title_is_prose(self):
		self.assertFalse(_fuzzy_title_allowed(frappe.get_meta("ToDo")))  # title is description, a Text Editor
		self.assertTrue(_fuzzy_title_allowed(frappe.get_meta("Note")))  # title is a Data field
		self.assertTrue(_fuzzy_title_allowed(frappe.get_meta("User")))  # title is full_name, a Data field

	def test_fuzzy_match_is_skipped_above_the_row_cap(self):
		rules = _settings(_rule("User", fuzzy=1))
		extracted = {"allocated_to": "Resolver Person Alph"}

		with mock.patch.object(entity_resolver, "_active_count", return_value=FUZZY_ROW_CAP + 1):
			res = resolve_entities("ToDo", extracted, rules)
		self.assertNotIn("allocated_to", res.values)
		self.assertTrue(res.needs_review)

		with mock.patch.object(entity_resolver, "_active_count", return_value=FUZZY_ROW_CAP):
			res = resolve_entities("ToDo", extracted, rules)
		self.assertEqual(res.values.get("allocated_to"), self.person)

	def test_auto_create_uses_default_values_and_mapped_required_values(self):
		rules = _settings(
			_rule(
				"Role",
				auto_create=1,
				field_map=json.dumps({"role": "role_name"}),
				default_values=json.dumps({"disabled": 1}),
			)
		)
		res = resolve_entities("ToDo", {"role": "Resolver Auto Role Qq"}, rules)
		self.assertEqual(res.values.get("role"), "Resolver Auto Role Qq")
		self.assertIn(("Role", "Resolver Auto Role Qq"), res.created)
		self.assertEqual(frappe.db.get_value("Role", "Resolver Auto Role Qq", "disabled"), 1)
		self.assertTrue(res.needs_review)
		self.assertTrue(any("created Role" in note for note in res.notes))

	def test_auto_create_refuses_when_a_required_value_is_missing(self):
		res = resolve_entities(
			"ToDo", {"role": "Resolver Refused Role Qq"}, _settings(_rule("Role", auto_create=1))
		)
		self.assertNotIn("role", res.values)
		self.assertEqual(res.created, [])
		self.assertFalse(frappe.db.exists("Role", "Resolver Refused Role Qq"))
		self.assertIn("Cannot create Role: no value for Role Name", res.notes)
		self.assertTrue(res.needs_review)

	def test_auto_create_that_fails_before_the_write_leaves_the_value_blank(self):
		rules = _settings(
			_rule(
				"Role",
				auto_create=1,
				field_map=json.dumps({"role": "role_name"}),
				default_values=json.dumps({"disabled": 1}),
			)
		)
		with mock.patch.object(Role, "validate", side_effect=frappe.ValidationError("refused")):
			res = resolve_entities("ToDo", {"role": "Resolver Refused Before Write Qq"}, rules)
		self.assertNotIn("role", res.values)
		self.assertEqual(res.created, [])
		self.assertFalse(res.write_failed)
		self.assertTrue(res.needs_review)
		self.assertTrue(any("could not create Role" in note for note in res.notes))
		self.assertFalse(frappe.db.exists("Role", "Resolver Refused Before Write Qq"))

	def test_auto_create_that_fails_after_the_write_sets_write_failed(self):
		# after_insert runs after the row is written, so the row is in the savepoint and the draft must roll back.
		rules = _settings(
			_rule(
				"Role",
				auto_create=1,
				field_map=json.dumps({"role": "role_name"}),
				default_values=json.dumps({"disabled": 1}),
			)
		)
		with mock.patch.object(Role, "after_insert", side_effect=frappe.ValidationError("late"), create=True):
			res = resolve_entities("ToDo", {"role": "Resolver Failed After Write Qq"}, rules)
		self.assertTrue(res.write_failed)
		self.assertEqual(res.created, [])
		self.assertTrue(res.needs_review)
		self.assertTrue(frappe.db.exists("Role", "Resolver Failed After Write Qq"))

	def test_without_a_rule_only_exact_name_and_title_match(self):
		res = resolve_entities("ToDo", {"allocated_to": PERSON_TITLE}, _settings())
		self.assertEqual(res.values.get("allocated_to"), self.person)

		res = resolve_entities("ToDo", {"allocated_to": "Resolver Person Alph"}, _settings())
		self.assertNotIn("allocated_to", res.values)

		res = resolve_entities(
			"ToDo",
			{"allocated_to": "Unknown Login Zz", "login_name": PERSON_USERNAME},
			_settings(),
		)
		self.assertNotIn("allocated_to", res.values)

		res = resolve_entities(
			"ToDo", {"allocated_to": "Unknown Login Zz"}, _settings(), sender_email=CONTACT_EMAIL
		)
		self.assertNotIn("allocated_to", res.values)

		res = resolve_entities("ToDo", {"role": "Resolver Default Role Qq"}, _settings())
		self.assertNotIn("role", res.values)
		self.assertFalse(frappe.db.exists("Role", "Resolver Default Role Qq"))

	def test_child_link_is_read_from_its_line_before_the_parent(self):
		extracted = {
			"title": "Resolver Note Gamma",
			"user": OTHER_EMAIL,
			"seen_by": [{"user": PERSON_EMAIL}, {}, {"user": "Unknown Person Zz"}],
		}
		res = resolve_entities("Note", extracted, _settings())
		rows = res.child_rows["seen_by"]
		self.assertEqual(res.values.get("title"), "Resolver Note Gamma")
		# The line's own value wins over the parent's.
		self.assertEqual(rows[0]["user"], self.person)
		# A line without the key falls back to the parent's value.
		self.assertEqual(rows[1]["user"], self.other)
		# A line that has a value matching nothing does not fall back to the parent's value.
		self.assertNotIn("user", rows[2])
		self.assertTrue(res.needs_review)

	def test_erpnext_supplier_name_match_ignores_disabled_suppliers(self):
		if not (frappe.db.exists("DocType", "Supplier") and frappe.db.exists("DocType", "Purchase Invoice")):
			self.skipTest("ERPNext is not installed")
		frappe.get_doc(
			{
				"doctype": "Supplier Group",
				"supplier_group_name": SUPPLIER_GROUP,
				"parent_supplier_group": "All Supplier Groups",
			}
		).insert(ignore_if_duplicate=True)
		active = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "Resolver Test Supplier",
				"supplier_type": "Company",
				"supplier_group": SUPPLIER_GROUP,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "Resolver Retired Supplier",
				"supplier_type": "Company",
				"supplier_group": SUPPLIER_GROUP,
				"disabled": 1,
			}
		).insert(ignore_permissions=True)

		res = resolve_entities("Purchase Invoice", {"supplier": active.name}, _settings())
		self.assertEqual(res.values.get("supplier"), active.name)
		self.assertFalse(res.needs_review)

		res = resolve_entities("Purchase Invoice", {"supplier": "Resolver Retired Supplier"}, _settings())
		self.assertNotIn("supplier", res.values)
		self.assertTrue(res.needs_review)

	def test_resolve_entities_never_raises_for_unknown_doctype(self):
		res = resolve_entities("No Such DocType Zz", {"supplier": "Some Name"}, _settings())
		self.assertIsInstance(res, Resolution)
		self.assertTrue(res.needs_review)


# The target of the match_keys cases is a fake DocType, so each case controls which Links and Table it has. Its linked
# doctypes are the real Note, ToDo and Role, so their fields and required values are the real ones.
MATCH_TARGET = "Resolver Match Target"
MATCH_LINE = "Resolver Match Line"
MATCH_NOTE_TITLE = "Resolver Match Note"
_REAL_GET_META = frappe.get_meta


def _fake_field(fieldname, fieldtype="Data", *, label=None, options=None, reqd=0, read_only=0):
	return SimpleNamespace(
		fieldname=fieldname,
		fieldtype=fieldtype,
		label=label or fieldname,
		options=options,
		reqd=reqd,
		read_only=read_only,
	)


def _fake_doctype(*fields):
	return SimpleNamespace(fields=list(fields), is_submittable=0)


# The Links are required, so the schema holds them, and a match key is requested only for a Link in the schema. tax_id
# and priority_code are writable and not required, so they are not in the schema and share a name with an identifier.
FAKE_MATCH_METAS = {
	MATCH_TARGET: _fake_doctype(
		_fake_field("title", reqd=1, label="Title"),
		_fake_field("note_ref", "Link", options="Note", reqd=1, label="Note"),
		_fake_field("note_alt", "Link", options="Note", reqd=1, label="Alternate Note"),
		_fake_field("role_ref", "Link", options="Role", reqd=1, label="Role"),
		_fake_field("tax_id", label="Tax ID"),
		_fake_field("lines", "Table", options=MATCH_LINE, reqd=1, label="Lines"),
	),
	MATCH_LINE: _fake_doctype(
		_fake_field("line_text", reqd=1, label="Line Text"),
		_fake_field("task_ref", "Link", options="ToDo", reqd=1, label="Task"),
		_fake_field("priority_code", label="Priority Code"),
	),
}


def _match_test_meta(doctype, *args, **kwargs):
	if doctype in FAKE_MATCH_METAS:
		return FAKE_MATCH_METAS[doctype]
	return _REAL_GET_META(doctype, *args, **kwargs)


class TestMatchKeys(IntegrationTestCase):
	"""The keys that match_keys requests for the Links of the fake target and of its child line."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.note = frappe.get_doc({"doctype": "Note", "title": MATCH_NOTE_TITLE}).insert(
			ignore_permissions=True
		)

	def setUp(self):
		super().setUp()
		patcher = mock.patch("frappe.get_meta", side_effect=_match_test_meta)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_header_identifier_key_is_requested_at_the_header_once(self):
		# note_ref and note_alt both link to Note, so the identifier key is listed once.
		settings = _settings(
			_rule("Note", identifier_fields="title", field_map=json.dumps({"note_code": "title"}))
		)
		self.assertEqual(
			match_keys(MATCH_TARGET, settings),
			{"": {"note_code": {"fieldtype": "Data", "label": "Title", "options": None}}},
		)

	def test_auto_create_key_for_a_child_link_is_requested_at_the_row_level_only(self):
		# task_ref is a Link on the line, so ToDo's required description is read from the line's own key.
		settings = _settings(_rule("ToDo", auto_create=1, field_map=json.dumps({"task_text": "description"})))
		self.assertEqual(
			match_keys(MATCH_TARGET, settings),
			{"lines": {"task_text": {"fieldtype": "Text Editor", "label": "Description", "options": None}}},
		)

	def test_key_supplied_by_default_values_is_not_requested(self):
		with_default = _settings(
			_rule(
				"Role",
				auto_create=1,
				field_map=json.dumps({"role_label": "role_name"}),
				default_values=json.dumps({"role_name": "Fixed Role"}),
			)
		)
		self.assertEqual(match_keys(MATCH_TARGET, with_default), {})

		without_default = _settings(
			_rule("Role", auto_create=1, field_map=json.dumps({"role_label": "role_name"}))
		)
		self.assertEqual(
			match_keys(MATCH_TARGET, without_default),
			{"": {"role_label": {"fieldtype": "Data", "label": "Role Name", "options": None}}},
		)

	def test_key_already_in_the_target_schema_is_not_requested(self):
		# title is a required header field, and line_text is a required column of the line, so neither is requested.
		settings = _settings(
			_rule("Note", identifier_fields="title", field_map=json.dumps({"title": "title"})),
			_rule("ToDo", auto_create=1, field_map=json.dumps({"line_text": "description"})),
		)
		self.assertEqual(match_keys(MATCH_TARGET, settings), {})

	def test_key_named_like_a_writable_header_field_is_not_requested(self):
		# tax_id is writable and not in the schema, so a key of that name would be written into it.
		settings = _settings(
			_rule("Note", identifier_fields="title", field_map=json.dumps({"tax_id": "title"}))
		)
		self.assertEqual(match_keys(MATCH_TARGET, settings), {})

	def test_key_named_like_a_writable_child_field_is_not_requested(self):
		# priority_code is a writable column of the line, so a row-level key of that name would be written into it.
		settings = _settings(
			_rule("ToDo", identifier_fields="priority", field_map=json.dumps({"priority_code": "priority"}))
		)
		self.assertEqual(match_keys(MATCH_TARGET, settings), {})

	def test_link_outside_the_target_schema_requests_no_key(self):
		# A Link that is not extracted has no value for the resolver to match, so its identifier key is not requested.
		note_rule = _rule("Note", identifier_fields="title", field_map=json.dumps({"note_code": "title"}))

		def settings_for(included_fields):
			target_rule = SimpleNamespace(
				target_doctype=MATCH_TARGET, included_fields=included_fields, reconciliation_field=""
			)
			return SimpleNamespace(
				fuzzy_match_threshold=85, entity_matches=[note_rule], doctype_rules=[target_rule]
			)

		self.assertEqual(match_keys(MATCH_TARGET, settings_for("title")), {})
		self.assertEqual(
			match_keys(MATCH_TARGET, settings_for("title, note_ref")),
			{"": {"note_code": {"fieldtype": "Data", "label": "Title", "options": None}}},
		)

	def test_key_read_by_two_linked_types_is_requested_as_text(self):
		# Note reads its identifier as Datetime and Role reads its identifier as Data, under one key. One schema type
		# cannot serve both, so the key is typed as text.
		settings = _settings(
			_rule(
				"Note",
				identifier_fields="expire_notification_on",
				field_map=json.dumps({"code": "expire_notification_on"}),
			),
			_rule("Role", identifier_fields="role_name", field_map=json.dumps({"code": "role_name"})),
		)
		requested = match_keys(MATCH_TARGET, settings)
		self.assertEqual(list(requested[""]), ["code"])
		self.assertEqual(requested[""]["code"]["fieldtype"], "Data")

	def test_invalid_rule_json_contributes_no_keys_and_does_not_raise(self):
		settings = _settings(_rule("Note", identifier_fields="title", field_map="{not json"))
		self.assertEqual(match_keys(MATCH_TARGET, settings), {})

	def test_requested_key_is_the_one_the_resolver_reads(self):
		settings = _settings(
			_rule("Note", identifier_fields="title", field_map=json.dumps({"note_code": "title"}))
		)
		key = next(iter(match_keys(MATCH_TARGET, settings)[""]))
		res = Resolution()
		name = _match_link(
			"Note",
			"Unknown Note Qq",
			settings,
			res,
			source={key: MATCH_NOTE_TITLE},
			sender="",
			where="Alternate Note",
		)
		self.assertEqual(name, self.note.name)
		self.assertTrue(res.needs_review)
