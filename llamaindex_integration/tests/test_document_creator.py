# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for services/document_creator.py: the parsed duplicate key and default values, the generic duplicate check,
the blank-key review path, the default values, the reconciliation check after insert, and the source File attachment.

Note is the fixture for the generic check and for the child defaults on its seen_by Table. ToDo is the fixture for the
blank key, the header defaults and the reconciliation check. ToDo and Note have no numeric field, so the reconciliation
cases use a test-only read-only Float Custom Field on ToDo, which the stubbed validate computes as a real total is. It
is created in setUpClass and removed in cleanup. Purchase Invoice is
the submittable ERPNext fixture. Its cases are skipped when ERPNext is not installed on the test site. Where a case only
needs to reach the insert call, the new Purchase Invoice is a MagicMock.
"""

import unittest
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.desk.doctype.todo.todo import ToDo
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase

from llamaindex_integration.services.document_creator import (
	KEY_SKIPPED_NOTE,
	MISSING_COMPUTED_TOTAL_NOTE,
	MISSING_TOTAL_NOTE,
	DuplicateDocumentError,
	TotalsMismatchError,
	_duplicate_query,
	_normalize_key_value,
	_parent_values,
	_parse_total,
	_require_computed_reconciliation_field,
	_uses_file_association,
	attach_source_file,
	create_draft_document,
	parse_default_values,
	parse_duplicate_key_fields,
)
from llamaindex_integration.services.entity_resolver import Resolution
from llamaindex_integration.tests import MINIMAL_PDF

MODULE = "llamaindex_integration.services.document_creator"
PURCHASE_INVOICE = "Purchase Invoice"
TEST_AMOUNT = "llamaindex_test_amount"
TEST_AMOUNT_CUSTOM_FIELD = f"ToDo-{TEST_AMOUNT}"


def _resolution(values=None, child_rows=None):
	return Resolution(
		values=dict(values or {}),
		child_rows=dict(child_rows or {}),
		notes=[],
		needs_review=False,
		created=[],
	)


def _fake_invoice(grand_total=0.0):
	"""A MagicMock standing in for a new Purchase Invoice. get and set read and write a real dict, so the duplicate key
	check sees the values the draft holds. insert and append stay MagicMock, so the tests can assert on them."""
	values = {"grand_total": grand_total}
	doc = MagicMock()
	doc.doctype = PURCHASE_INVOICE
	doc.name = "ACC-PINV-0001"
	doc.grand_total = grand_total
	doc.get.side_effect = values.get
	doc.set.side_effect = values.__setitem__
	doc.insert.return_value = doc
	return doc


def _field(fieldtype, read_only=0):
	return SimpleNamespace(fieldtype=fieldtype, read_only=read_only)


def _unique(prefix):
	return f"{prefix}-{frappe.generate_hash(length=8)}"


def _note(title):
	return frappe.get_doc({"doctype": "Note", "title": title}).insert(ignore_permissions=True)


def _todo(description):
	return frappe.get_doc({"doctype": "ToDo", "description": description}).insert(ignore_permissions=True)


def _remote_file(file_name):
	"""Return a File that points at a remote URL, so no bytes are written to disk."""
	return frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"file_url": f"https://files.example.test/llamaindex-test/{file_name}",
		}
	).insert(ignore_permissions=True)


def _purchase_invoice_row(name, *, docstatus, supplier, bill_no):
	"""Write a Purchase Invoice row without validation, so the duplicate lookup has a real row to find."""
	return frappe.get_doc(
		{
			"doctype": PURCHASE_INVOICE,
			"name": name,
			"docstatus": docstatus,
			"company": "Test Company",
			"supplier": supplier,
			"bill_no": bill_no,
		}
	).db_insert()


def _make_user(email):
	user = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": "Llama Test", "send_welcome_email": 0}
	)
	return user.insert(ignore_permissions=True, ignore_if_duplicate=True).name


def _remove_test_amount_field():
	if frappe.db.exists("Custom Field", TEST_AMOUNT_CUSTOM_FIELD):
		frappe.delete_doc("Custom Field", TEST_AMOUNT_CUSTOM_FIELD, ignore_permissions=True, force=True)


def _ensure_test_amount_field():
	"""Create the test-only numeric field on ToDo, read-only. Nothing else in the test suite relies on it.

	A copy left behind by an earlier run may be writable, and a writable field is refused as a reconciliation field. So any
	existing copy is deleted first, and the new one is always read-only.
	"""
	_remove_test_amount_field()
	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "ToDo",
			"fieldname": TEST_AMOUNT,
			"label": "LlamaIndex Test Amount",
			"fieldtype": "Float",
			"read_only": 1,
			"insert_after": "description",
		}
	).insert(ignore_permissions=True)


TEST_MARKER = "llz-test:"


def _marked(label):
	"""A ToDo description that carries TEST_MARKER and a unique id, so the rows a test makes can be found and deleted."""
	return f"{TEST_MARKER} {label} {uuid.uuid4().hex}"


def _delete_marked_todos():
	for name in frappe.get_all("ToDo", filters={"description": ["like", f"{TEST_MARKER}%"]}, pluck="name"):
		frappe.delete_doc("ToDo", name, ignore_permissions=True, force=True)


class _MarkedToDoTestCase(IntegrationTestCase):
	"""The ToDo rows of these tests are marked, and the marked rows are deleted before and after each test.

	Rows left by an earlier run hold the same dates and priorities as a new draft, so a duplicate key check would match them.
	"""

	def setUp(self):
		super().setUp()
		_delete_marked_todos()

	def tearDown(self):
		_delete_marked_todos()
		super().tearDown()


def _computes_test_amount(total):
	"""Patch ToDo.validate so the read-only test total holds total after save, as an ERPNext controller computes a total."""

	def validate(self):
		# ToDo.on_update reads _assignment, and the real validate sets it first, so the stub does too.
		self._assignment = None
		self.llamaindex_test_amount = total

	return patch.object(ToDo, "validate", validate)


def _file_meta(*fieldnames):
	return SimpleNamespace(has_field=lambda fieldname: fieldname in fieldnames)


class TestParseDuplicateKeyFields(unittest.TestCase):
	def test_none_and_blank_input_give_no_fields(self):
		for raw in (None, "", "   ", " , ,"):
			with self.subTest(raw=raw):
				self.assertEqual(parse_duplicate_key_fields(raw), ())

	def test_whitespace_and_blank_parts_are_dropped(self):
		self.assertEqual(parse_duplicate_key_fields(" supplier ,  bill_no ,, "), ("supplier", "bill_no"))

	def test_order_is_kept(self):
		self.assertEqual(parse_duplicate_key_fields("bill_no,supplier"), ("bill_no", "supplier"))


class TestParseDefaultValues(IntegrationTestCase):
	def test_none_and_blank_input_give_no_defaults(self):
		for raw in (None, "", "   "):
			with self.subTest(raw=raw):
				self.assertEqual(parse_default_values(raw), {})

	def test_a_json_object_is_returned_as_a_dict(self):
		self.assertEqual(
			parse_default_values('{"priority": "High", "seen_by.user": "Administrator"}'),
			{"priority": "High", "seen_by.user": "Administrator"},
		)

	def test_invalid_json_raises_a_validation_error(self):
		with self.assertRaises(frappe.ValidationError):
			parse_default_values("{priority: High")

	def test_a_json_value_that_is_not_an_object_raises_a_validation_error(self):
		for raw in ('["High"]', '"High"', "3", "null"):
			with self.subTest(raw=raw), self.assertRaises(frappe.ValidationError):
				parse_default_values(raw)


class TestDuplicateKeyOnNote(IntegrationTestCase):
	"""Note is not submittable and has no company, so this exercises the generic key path alone."""

	def test_existing_note_with_the_same_title_blocks_a_new_one(self):
		title = _unique("llama-dup")
		existing = _note(title)

		# The draft is built before the check, so the proof that nothing is written is that insert is never called.
		with (
			patch.object(Document, "insert") as insert,
			self.assertRaises(DuplicateDocumentError) as ctx,
		):
			create_draft_document("Note", {"title": title}, _resolution(), duplicate_key_fields=("title",))

		insert.assert_not_called()
		message = str(ctx.exception)
		self.assertIn("Note", message)
		self.assertIn(existing.name, message)
		self.assertIn("title", message)

	def test_key_value_is_compared_after_trimming(self):
		title = _unique("llama-trim")
		_note(title)

		with self.assertRaises(DuplicateDocumentError):
			create_draft_document(
				"Note", {"title": f"  {title}  "}, _resolution(), duplicate_key_fields=("title",)
			)

	def test_a_different_title_is_allowed(self):
		_note(_unique("llama-existing"))
		title = _unique("llama-new")

		name = create_draft_document("Note", {"title": title}, _resolution(), duplicate_key_fields=("title",))

		self.assertEqual(frappe.get_doc("Note", name).title, title)

	def test_unknown_key_field_raises_before_any_document_is_built(self):
		with (
			patch("frappe.new_doc") as new_doc,
			self.assertRaises(frappe.ValidationError),
		):
			create_draft_document(
				"Note", {"title": "x"}, _resolution(), duplicate_key_fields=("no_such_field",)
			)

		new_doc.assert_not_called()

	def test_a_table_cannot_be_a_key(self):
		with (
			patch("frappe.new_doc") as new_doc,
			self.assertRaises(frappe.ValidationError),
		):
			create_draft_document("Note", {}, _resolution(), duplicate_key_fields=("seen_by",))

		new_doc.assert_not_called()

	def test_submittable_lookup_ignores_cancelled_rows_and_plain_lookup_does_not(self):
		submittable = _duplicate_query("Note", {"title": "x"}, submittable=True).get_sql()
		plain = _duplicate_query("Note", {"title": "x"}, submittable=False).get_sql()

		self.assertIn("FOR UPDATE", submittable)
		self.assertIn("`docstatus`<>2", submittable)
		self.assertIn("FOR UPDATE", plain)
		self.assertNotIn("docstatus", plain)


class TestBlankKeyValue(_MarkedToDoTestCase):
	"""ToDo.reference_type is an optional Link with no default, so its key stays blank on a new draft.

	ToDo.date is not the key here. Its default is Today, so a new ToDo always holds a date, and the check would compare that
	date rather than skip it.
	"""

	def test_blank_key_skips_the_check_flags_review_and_still_inserts(self):
		resolution = _resolution()
		name = create_draft_document(
			"ToDo",
			{"description": _marked("Blank key")},
			resolution,
			duplicate_key_fields=("reference_type",),
		)

		self.assertTrue(resolution.needs_review)
		self.assertIn(KEY_SKIPPED_NOTE.format("reference_type"), resolution.notes)
		self.assertTrue(frappe.db.exists("ToDo", name))

	def test_blank_key_never_matches_an_existing_row_with_a_blank_key(self):
		# The existing row has no reference_type either. A blank key is never compared, so it cannot block the insert.
		frappe.get_doc({"doctype": "ToDo", "description": _marked("Existing without a reference")}).insert(
			ignore_permissions=True
		)
		resolution = _resolution()

		name = create_draft_document(
			"ToDo",
			{"description": _marked("New without a reference")},
			resolution,
			duplicate_key_fields=("reference_type",),
		)

		self.assertTrue(resolution.needs_review)
		self.assertTrue(frappe.db.exists("ToDo", name))


class TestDefaultValues(_MarkedToDoTestCase):
	def test_default_fills_an_empty_header_field(self):
		# ToDo defaults priority to Medium on its own, so High here can only come from the rule's default.
		name = create_draft_document(
			"ToDo",
			{"description": _unique("Default fill")},
			_resolution(),
			default_values={"priority": "High"},
		)

		self.assertEqual(frappe.db.get_value("ToDo", name, "priority"), "High")

	def test_extracted_value_is_kept_over_the_default(self):
		name = create_draft_document(
			"ToDo",
			{"description": _unique("Default kept"), "priority": "Low"},
			_resolution(),
			default_values={"priority": "High"},
		)

		self.assertEqual(frappe.db.get_value("ToDo", name, "priority"), "Low")

	def test_blank_extracted_value_counts_as_empty(self):
		name = create_draft_document(
			"ToDo",
			{"description": _unique("Default blank"), "priority": "   "},
			_resolution(),
			default_values={"priority": "High"},
		)

		self.assertEqual(frappe.db.get_value("ToDo", name, "priority"), "High")

	def test_text_editor_holding_only_html_counts_as_empty_for_a_default(self):
		# The resolver treats "<p><br></p>" as blank for a Text Editor, so the default must fill it too.
		name = create_draft_document(
			"ToDo",
			{"description": "<p><br></p>"},
			_resolution(),
			default_values={"description": "Follow up later"},
		)

		self.assertEqual(frappe.db.get_value("ToDo", name, "description"), "Follow up later")

	def test_default_is_applied_before_the_blank_key_check(self):
		resolution = _resolution()
		name = create_draft_document(
			"ToDo",
			{"description": _marked("Default key")},
			resolution,
			duplicate_key_fields=("date",),
			default_values={"date": "2031-02-01"},
		)

		self.assertFalse(resolution.needs_review)
		self.assertEqual(frappe.db.get_value("ToDo", name, "date"), date(2031, 2, 1))

	def test_default_on_a_duplicate_key_is_checked_against_existing_documents(self):
		title = _unique("llama-default-dup")
		_note(title)

		with (
			patch.object(Document, "insert") as insert,
			self.assertRaises(DuplicateDocumentError),
		):
			create_draft_document(
				"Note",
				{},
				_resolution(),
				duplicate_key_fields=("title",),
				default_values={"title": title},
			)

		insert.assert_not_called()

	def test_child_default_fills_only_the_rows_that_lack_the_value(self):
		owner = _make_user("llamaindex-test-seen-owner@example.test")
		resolution = _resolution(child_rows={"seen_by": [{"user": owner}, {}, {"user": "   "}]})

		name = create_draft_document(
			"Note",
			{"title": _unique("llama-child-default")},
			resolution,
			default_values={"seen_by.user": "Administrator"},
		)

		users = frappe.get_all(
			"Note Seen By",
			filters={"parent": name, "parenttype": "Note", "parentfield": "seen_by"},
			order_by="idx",
			pluck="user",
		)
		self.assertEqual(users, [owner, "Administrator", "Administrator"])

	def test_child_default_adds_no_rows(self):
		name = create_draft_document(
			"Note",
			{"title": _unique("llama-no-rows")},
			_resolution(),
			default_values={"seen_by.user": "Administrator"},
		)

		self.assertEqual(frappe.get_doc("Note", name).seen_by, [])

	def test_default_key_must_name_a_value_field_of_the_target(self):
		with (
			patch("frappe.new_doc") as new_doc,
			self.assertRaises(frappe.ValidationError),
		):
			create_draft_document(
				"Note", {"title": "x"}, _resolution(), default_values={"no_such_field": "x"}
			)

		new_doc.assert_not_called()

	def test_default_for_a_table_field_is_refused(self):
		with (
			patch("frappe.new_doc") as new_doc,
			self.assertRaises(frappe.ValidationError),
		):
			create_draft_document("Note", {"title": "x"}, _resolution(), default_values={"seen_by": "x"})

		new_doc.assert_not_called()

	def test_child_default_column_must_exist_on_the_child_doctype(self):
		with (
			patch("frappe.new_doc") as new_doc,
			self.assertRaises(frappe.ValidationError),
		):
			create_draft_document(
				"Note",
				{"title": "x"},
				_resolution(child_rows={"seen_by": [{}]}),
				default_values={"seen_by.no_such_column": "x"},
			)

		new_doc.assert_not_called()


class TestKeyFieldTypes(IntegrationTestCase):
	"""The creator refuses a key whose fieldtype cannot be a key, the same types the rule validator allows."""

	def test_text_key_is_refused_before_any_document_is_built(self):
		with (
			patch("frappe.new_doc") as new_doc,
			self.assertRaises(frappe.ValidationError),
		):
			create_draft_document("ToDo", {}, _resolution(), duplicate_key_fields=("description",))

		new_doc.assert_not_called()


class TestDateKeyValues(_MarkedToDoTestCase):
	def test_date_key_is_parsed_from_a_non_canonical_string(self):
		self.assertEqual(_normalize_key_value("Date", "2026-1-5"), date(2026, 1, 5))

	def test_blank_date_key_is_none(self):
		self.assertIsNone(_normalize_key_value("Date", "   "))

	def test_unreadable_date_key_raises_a_validation_error(self):
		with self.assertRaises(frappe.ValidationError):
			_normalize_key_value("Date", "not a date")

	def test_date_key_matches_a_stored_date_written_in_another_format(self):
		frappe.get_doc({"doctype": "ToDo", "description": _marked("Dated"), "date": "2026-01-05"}).insert(
			ignore_permissions=True
		)

		with self.assertRaises(DuplicateDocumentError):
			create_draft_document(
				"ToDo",
				{"description": _marked("Again"), "date": "2026-1-5"},
				_resolution(),
				duplicate_key_fields=("date",),
			)

	def test_blank_key_that_frappe_fills_on_insert_is_compared_with_that_value(self):
		# ToDo.priority defaults to Medium, and the draft is stored with Medium, so the lookup must use Medium as well.
		frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": _marked("Default key"),
				"date": "2026-02-03",
				"priority": "Medium",
			}
		).insert(ignore_permissions=True)

		with self.assertRaises(DuplicateDocumentError):
			create_draft_document(
				"ToDo",
				{"description": _marked("Default key again"), "date": "2026-02-03"},
				_resolution(),
				duplicate_key_fields=("date", "priority"),
			)

	def test_key_that_frappe_fills_from_a_default_is_seen_by_the_check(self):
		# The draft gets Frappe's defaults for date (Today) and priority (Medium), and neither is in the extraction. The
		# existing row holds the same two values, so the check must see the defaults to find it.
		frappe.get_doc({"doctype": "ToDo", "description": _marked("Existing with defaults")}).insert(
			ignore_permissions=True
		)

		with self.assertRaises(DuplicateDocumentError):
			create_draft_document(
				"ToDo",
				{"description": _marked("Draft with defaults")},
				_resolution(),
				duplicate_key_fields=("date", "priority"),
			)


class TestParseTotal(unittest.TestCase):
	def test_plain_and_thousands_separated_totals_are_read(self):
		self.assertEqual(_parse_total("1,234.50"), 1234.5)
		self.assertEqual(_parse_total("1,234,567"), 1234567.0)
		self.assertEqual(_parse_total("-1,234"), -1234.0)
		self.assertEqual(_parse_total(" 99.5 "), 99.5)
		self.assertEqual(_parse_total(12), 12.0)

	def test_a_comma_that_is_not_a_thousands_separator_makes_the_total_unreadable(self):
		# Stripping the comma of "1.234,50" would read it as 1.2345, so it is unreadable instead.
		for raw in ("1.234,50", "12,34", "1234,5", "1,23"):
			with self.subTest(raw=raw):
				self.assertIsNone(_parse_total(raw))


class TestReconciliationFieldGuard(unittest.TestCase):
	def _check(self, docfield):
		meta = SimpleNamespace(get_field=lambda fieldname: docfield)
		with patch("frappe.get_meta", return_value=meta):
			_require_computed_reconciliation_field("ToDo", TEST_AMOUNT)

	def test_read_only_numeric_field_is_accepted(self):
		self._check(SimpleNamespace(fieldtype="Currency", read_only=1))

	def test_writable_numeric_field_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(SimpleNamespace(fieldtype="Currency", read_only=0))

	def test_read_only_field_that_is_not_numeric_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(SimpleNamespace(fieldtype="Data", read_only=1))

	def test_missing_field_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(None)

	def test_writable_field_is_refused_by_create_draft_document_when_the_rule_is_bypassed(self):
		# A rule saved before the field became writable still reaches the creator. The creator refuses the field before any
		# document is built, so the copied value is never compared with itself.
		docfield = SimpleNamespace(fieldname=TEST_AMOUNT, fieldtype="Currency", read_only=0)
		meta = SimpleNamespace(get_field=lambda fieldname: docfield)
		with (
			patch("frappe.get_meta", return_value=meta),
			patch("frappe.new_doc") as new_doc,
			self.assertRaisesRegex(frappe.ValidationError, "must be a read-only numeric field"),
		):
			create_draft_document(
				"ToDo",
				{"description": "Writable total", TEST_AMOUNT: 100.0},
				_resolution(),
				reconciliation_field=TEST_AMOUNT,
			)

		new_doc.assert_not_called()


class TestDraftInsertAndTotals(IntegrationTestCase):
	def test_other_doctype_is_inserted_as_draft(self):
		resolution = _resolution()
		name = create_draft_document("ToDo", {"description": "Draft from test"}, resolution)

		self.assertFalse(resolution.needs_review)
		todo = frappe.get_doc("ToDo", name)
		self.assertEqual(todo.description, "Draft from test")
		self.assertEqual(todo.docstatus, 0)

	def test_insert_is_a_draft_and_never_commits_or_savepoints(self):
		with (
			patch.object(frappe.db, "commit") as commit,
			patch.object(frappe.db, "savepoint") as savepoint,
		):
			name = create_draft_document("ToDo", {"description": _unique("No commit")}, _resolution())

		commit.assert_not_called()
		savepoint.assert_not_called()
		self.assertEqual(frappe.db.get_value("ToDo", name, "docstatus"), 0)


class TestReconciliationOnToDo(IntegrationTestCase):
	"""The reconciliation value is read after insert. The test-only ToDo field stands in for a numeric total."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_amount_field()
		cls.addClassCleanup(_remove_test_amount_field)

	def test_matching_total_within_tolerance_passes(self):
		resolution = _resolution()
		with _computes_test_amount(120.0):
			name = create_draft_document(
				"ToDo",
				{"description": _unique("Totals match"), TEST_AMOUNT: 120.004},
				resolution,
				reconciliation_field=TEST_AMOUNT,
			)

		self.assertFalse(resolution.needs_review)
		self.assertEqual(frappe.db.get_value("ToDo", name, TEST_AMOUNT), 120.0)

	def test_mismatch_raises_after_insert_and_the_caller_rolls_the_insert_back(self):
		description = _unique("Totals mismatch")
		savepoint = "llama_test_totals"
		frappe.db.savepoint(savepoint)
		try:
			with (
				_computes_test_amount(120.0),
				self.assertRaises(TotalsMismatchError),
			):
				create_draft_document(
					"ToDo",
					{"description": description, TEST_AMOUNT: 100.0},
					_resolution(),
					reconciliation_field=TEST_AMOUNT,
				)
		finally:
			frappe.db.rollback(save_point=savepoint)

		self.assertFalse(frappe.db.exists("ToDo", {"description": description}))

	def test_missing_extracted_value_is_flagged_for_review(self):
		resolution = _resolution()
		with _computes_test_amount(120.0):
			name = create_draft_document(
				"ToDo",
				{"description": _unique("Totals missing")},
				resolution,
				reconciliation_field=TEST_AMOUNT,
			)

		self.assertTrue(resolution.needs_review)
		self.assertIn(MISSING_TOTAL_NOTE, resolution.notes)
		self.assertTrue(frappe.db.exists("ToDo", name))

	def test_unreadable_extracted_total_is_flagged_not_read_as_zero(self):
		# flt("N/A") is 0, and a computed total of 0 would then pass. The extracted total must read as a number.
		for extracted in ("N/A", "", "nan", "inf", True):
			with self.subTest(extracted=extracted):
				resolution = _resolution()
				savepoint = "llama_test_unreadable_total"
				frappe.db.savepoint(savepoint)
				try:
					with _computes_test_amount(0.0):
						create_draft_document(
							"ToDo",
							{"description": _unique("Totals unreadable"), TEST_AMOUNT: extracted},
							resolution,
							reconciliation_field=TEST_AMOUNT,
						)
				finally:
					frappe.db.rollback(save_point=savepoint)

				self.assertTrue(resolution.needs_review)
				self.assertIn(MISSING_TOTAL_NOTE, resolution.notes)

	def test_computed_total_that_is_not_set_fails_closed_even_when_the_extraction_reads_zero(self):
		# A computed None is not a zero, so the extracted "0" must not pass. The draft goes back to Needs Review.
		description = _unique("Totals not computed")
		resolution = _resolution()
		savepoint = "llama_test_missing_computed"
		frappe.db.savepoint(savepoint)
		try:
			with (
				_computes_test_amount(None),
				self.assertRaises(TotalsMismatchError),
			):
				create_draft_document(
					"ToDo",
					{"description": description, TEST_AMOUNT: "0"},
					resolution,
					reconciliation_field=TEST_AMOUNT,
				)
		finally:
			frappe.db.rollback(save_point=savepoint)

		self.assertIn(MISSING_COMPUTED_TOTAL_NOTE, resolution.notes)
		self.assertFalse(resolution.needs_review)
		self.assertFalse(frappe.db.exists("ToDo", {"description": description}))

	def test_extracted_total_with_thousands_separators_is_reconciled(self):
		resolution = _resolution()
		with _computes_test_amount(1234.5):
			create_draft_document(
				"ToDo",
				{"description": _unique("Totals separators"), TEST_AMOUNT: "1,234.50"},
				resolution,
				reconciliation_field=TEST_AMOUNT,
			)

		self.assertFalse(resolution.needs_review)

	def test_no_reconciliation_field_means_no_check_and_no_flag(self):
		resolution = _resolution()
		with _computes_test_amount(120.0):
			create_draft_document(
				"ToDo",
				{"description": _unique("Totals unchecked"), TEST_AMOUNT: 100.0},
				resolution,
			)

		self.assertFalse(resolution.needs_review)
		self.assertNotIn(MISSING_TOTAL_NOTE, resolution.notes)

	def test_refused_reconciliation_field_stops_the_draft_before_insert(self):
		description = _unique("Refused total")
		guard = patch(f"{MODULE}._require_computed_reconciliation_field", side_effect=frappe.ValidationError)
		with guard, self.assertRaises(frappe.ValidationError):
			create_draft_document(
				"ToDo",
				{"description": description, TEST_AMOUNT: 100.0},
				_resolution(),
				reconciliation_field=TEST_AMOUNT,
			)

		self.assertFalse(frappe.db.exists("ToDo", {"description": description}))


class TestReconciliationFieldMustBeComputed(IntegrationTestCase):
	"""A writable reconciliation field is refused by the rule. The creator copies no read-only value, so a writable one
	would be copied from the extraction and then compared with itself. Skipped when ERPNext is not installed."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise unittest.SkipTest("ERPNext is not installed on the test site")

	def test_writable_reconciliation_field_is_refused_by_the_rule(self):
		# paid_amount is a writable Currency field of Purchase Invoice, so the rule refuses it.
		rule = frappe.get_doc(
			{
				"doctype": "Llama DocType Rule",
				"target_doctype": PURCHASE_INVOICE,
				"classification_description": "Vendor invoice",
				"reconciliation_field": "paid_amount",
			}
		)
		with self.assertRaisesRegex(
			frappe.ValidationError, "Reconciliation Field paid_amount .* must be a read-only"
		):
			rule.validate()


class TestParentValues(IntegrationTestCase):
	def test_only_value_fieldtypes_are_copied_from_extracted_data(self):
		fields = {
			"bill_no": _field("Data"),
			"logo": _field("Attach"),
			"api_secret": _field("Password"),
			"linked_record": _field("Dynamic Link"),
			"is_return": _field("Check", read_only=1),
		}
		extracted = {
			"bill_no": "INV-9",
			"logo": "logo.png",
			"api_secret": "value",
			"linked_record": "ITEM-1",
			"is_return": 1,
		}
		with patch("frappe.get_meta", return_value=SimpleNamespace(get_field=fields.get)):
			values = _parent_values("ToDo", extracted, _resolution())

		self.assertEqual(values, {"bill_no": "INV-9"})


class TestAttachSourceFilePlainBranch(IntegrationTestCase):
	"""The plain branch: File.create_attachment_copy makes a new File. The cloud branch is forced off here."""

	def setUp(self):
		super().setUp()
		self.enterContext(patch(f"{MODULE}._uses_file_association", return_value=False))

	def test_public_remote_file_is_attached_and_source_is_left_alone(self):
		# A remote URL keeps the test off the disk: validation does not touch a remote file.
		source = _remote_file("llamaindex-test-invoice.pdf")
		todo = _todo("Attachment target")

		new_name = attach_source_file(source.name, "ToDo", todo.name)

		self.assertNotEqual(new_name, source.name)
		attached = frappe.get_doc("File", new_name)
		self.assertEqual(attached.file_url, source.file_url)
		self.assertEqual(attached.attached_to_doctype, "ToDo")
		self.assertEqual(attached.attached_to_name, todo.name)
		self.assertFalse(frappe.db.get_value("File", source.name, "attached_to_doctype"))

	def test_private_file_owned_by_another_user_attaches_while_the_job_user_cannot_read_it(self):
		self.addCleanup(frappe.set_user, "Administrator")
		owner = _make_user("llamaindex-test-owner@example.test")
		job_user = _make_user("llamaindex-test-job@example.test")

		frappe.set_user(owner)
		source = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{_unique('llama-private')}.pdf",
				"content": MINIMAL_PDF,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		frappe.set_user("Administrator")
		todo = _todo("Attachment target")

		frappe.set_user(job_user)
		# The precondition: the job user cannot read the source, so only the Administrator copy can succeed.
		self.assertFalse(frappe.has_permission("File", "read", doc=source, user=job_user))

		new_name = attach_source_file(source.name, "ToDo", todo.name)

		self.assertEqual(frappe.session.user, job_user)
		self.assertNotEqual(new_name, source.name)
		attached = frappe.get_doc("File", new_name)
		self.assertEqual(attached.file_url, source.file_url)
		self.assertEqual(attached.attached_to_doctype, "ToDo")
		self.assertEqual(attached.attached_to_name, todo.name)

	def test_missing_source_file_raises_and_restores_the_user(self):
		self.addCleanup(frappe.set_user, "Administrator")
		job_user = _make_user("llamaindex-test-job-missing@example.test")
		frappe.set_user(job_user)

		with self.assertRaises(frappe.DoesNotExistError):
			attach_source_file("llamaindex-test-no-such-file", "ToDo", "llamaindex-test-no-such-todo")

		self.assertEqual(frappe.session.user, job_user)

	def test_failed_copy_still_restores_the_job_user(self):
		self.addCleanup(frappe.set_user, "Administrator")
		job_user = _make_user("llamaindex-test-job-copy@example.test")
		source = _remote_file("llamaindex-test-copy-fails.pdf")
		todo = _todo("Attachment target")

		frappe.set_user(job_user)
		with (
			patch(
				"frappe.core.doctype.file.file.File.create_attachment_copy",
				side_effect=frappe.PermissionError,
			),
			self.assertRaises(frappe.PermissionError),
		):
			attach_source_file(source.name, "ToDo", todo.name)

		# The copy runs as Administrator, so a failed copy must still hand the session back to the job user.
		self.assertEqual(frappe.session.user, job_user)


class TestAttachSourceFileBranches(IntegrationTestCase):
	"""The branch choice and the cloud dispatch, checked without cloud_storage."""

	def test_the_cloud_branch_needs_cloud_storage_and_the_association_field(self):
		with (
			patch("frappe.get_installed_apps", return_value=["frappe"]),
			patch("frappe.get_meta", return_value=_file_meta("file_association")),
		):
			self.assertFalse(_uses_file_association())

		with (
			patch("frappe.get_installed_apps", return_value=["frappe", "cloud_storage"]),
			patch("frappe.get_meta", return_value=_file_meta("file_name")),
		):
			self.assertFalse(_uses_file_association())

		with (
			patch("frappe.get_installed_apps", return_value=["frappe", "cloud_storage"]),
			patch("frappe.get_meta", return_value=_file_meta("file_association")),
		):
			self.assertTrue(_uses_file_association())

	def test_cloud_branch_links_the_source_and_creates_no_copy(self):
		source = _remote_file("llamaindex-test-branch.pdf")
		todo = _todo("Branch target")
		previous_user = frappe.session.user

		with (
			patch(f"{MODULE}._uses_file_association", return_value=True),
			patch(f"{MODULE}._add_file_association") as add_association,
			patch("frappe.core.doctype.file.file.File.create_attachment_copy") as copy,
		):
			result = attach_source_file(source.name, "ToDo", todo.name)

		self.assertEqual(result, source.name)
		add_association.assert_called_once_with(source.name, "ToDo", todo.name)
		copy.assert_not_called()
		self.assertEqual(frappe.session.user, previous_user)

	def test_cloud_branch_restores_the_job_user_when_the_link_fails(self):
		self.addCleanup(frappe.set_user, "Administrator")
		source = _remote_file("llamaindex-test-link-fails.pdf")
		todo = _todo("Branch target")
		job_user = _make_user("llamaindex-test-job-link@example.test")

		frappe.set_user(job_user)
		with (
			patch(f"{MODULE}._uses_file_association", return_value=True),
			patch(f"{MODULE}._add_file_association", side_effect=frappe.ValidationError),
			self.assertRaises(frappe.ValidationError),
		):
			attach_source_file(source.name, "ToDo", todo.name)

		self.assertEqual(frappe.session.user, job_user)


class TestFileAssociationWithCloudStorage(IntegrationTestCase):
	"""Runs only when cloud_storage is installed on the test site, because it adds the File Association table."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "cloud_storage" not in frappe.get_installed_apps():
			raise unittest.SkipTest("cloud_storage is not installed on the test site")

	def test_two_attaches_add_one_association_row_and_no_file(self):
		source = _remote_file("llamaindex-test-association.pdf")
		todo = _todo("Association target")
		file_count = frappe.db.count("File")

		self.assertEqual(attach_source_file(source.name, "ToDo", todo.name), source.name)
		self.assertEqual(attach_source_file(source.name, "ToDo", todo.name), source.name)

		self.assertEqual(frappe.db.count("File"), file_count)
		rows = frappe.get_all(
			"File Association",
			filters={
				"parent": source.name,
				"parenttype": "File",
				"parentfield": "file_association",
				"link_doctype": "ToDo",
				"link_name": todo.name,
			},
			pluck="name",
		)
		self.assertEqual(len(rows), 1)

	def test_each_target_gets_its_own_association_row(self):
		source = _remote_file("llamaindex-test-two-targets.pdf")
		first = _todo("First association target")
		second = _todo("Second association target")

		attach_source_file(source.name, "ToDo", first.name)
		attach_source_file(source.name, "ToDo", second.name)

		links = frappe.get_all(
			"File Association",
			filters={"parent": source.name, "parenttype": "File", "parentfield": "file_association"},
			pluck="link_name",
		)
		self.assertCountEqual(links, [first.name, second.name])


class TestPurchaseInvoiceCreation(IntegrationTestCase):
	"""Purchase Invoice is the submittable ERPNext fixture. Skipped when ERPNext is not installed on the test site."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise unittest.SkipTest("ERPNext is not installed on the test site")

	def test_cancelled_document_does_not_block(self):
		supplier, bill_no = _unique("SUP"), _unique("INV")
		_purchase_invoice_row(
			_unique("llama-pinv-cancelled"), docstatus=2, supplier=supplier, bill_no=bill_no
		)
		fake = _fake_invoice()

		with patch("frappe.new_doc", return_value=fake):
			name = create_draft_document(
				PURCHASE_INVOICE,
				{"bill_no": bill_no},
				_resolution({"supplier": supplier}),
				duplicate_key_fields=("supplier", "bill_no"),
			)

		self.assertEqual(name, "ACC-PINV-0001")
		fake.insert.assert_called_once_with(ignore_permissions=True)

	def test_draft_and_submitted_documents_block(self):
		for docstatus in (0, 1):
			with self.subTest(docstatus=docstatus):
				supplier, bill_no = _unique("SUP"), _unique("INV")
				existing = _unique(f"llama-pinv-{docstatus}")
				_purchase_invoice_row(existing, docstatus=docstatus, supplier=supplier, bill_no=bill_no)
				fake = _fake_invoice()

				with (
					patch("frappe.new_doc", return_value=fake),
					self.assertRaises(DuplicateDocumentError) as ctx,
				):
					create_draft_document(
						PURCHASE_INVOICE,
						{"bill_no": bill_no},
						_resolution({"supplier": supplier}),
						duplicate_key_fields=("supplier", "bill_no"),
					)

				self.assertIn(existing, str(ctx.exception))
				fake.insert.assert_not_called()

	def test_resolved_company_is_written_and_none_is_invented(self):
		fake = _fake_invoice()
		with patch("frappe.new_doc", return_value=fake):
			create_draft_document(PURCHASE_INVOICE, {}, _resolution({"company": "Resolved Company"}))
		fake.set.assert_any_call("company", "Resolved Company")

		fake = _fake_invoice()
		with patch("frappe.new_doc", return_value=fake):
			create_draft_document(PURCHASE_INVOICE, {}, _resolution())
		self.assertNotIn("company", [c.args[0] for c in fake.set.call_args_list])

	def test_link_fields_come_only_from_resolver_and_unknown_keys_are_dropped(self):
		fake = _fake_invoice()
		extracted = {
			"bill_no": "INV-9",
			"supplier": "Raw Supplier Name",
			"not_a_field": "ignored",
			"items": [{"item_code": "RAW"}],
		}
		resolution = _resolution({"supplier": "SUP-1", "currency": "EUR"})
		with patch("frappe.new_doc", return_value=fake):
			create_draft_document(PURCHASE_INVOICE, extracted, resolution)

		set_values = {c.args[0]: c.args[1] for c in fake.set.call_args_list}
		self.assertEqual(set_values["supplier"], "SUP-1")
		self.assertEqual(set_values["currency"], "EUR")
		self.assertEqual(set_values["bill_no"], "INV-9")
		self.assertNotIn("not_a_field", set_values)
		self.assertNotIn("items", set_values)

	def test_child_rows_are_appended_with_known_child_fields_only(self):
		fake = _fake_invoice()
		resolution = _resolution(
			{"supplier": "SUP-1"},
			{"items": [{"item_code": "ITEM-1", "qty": 2, "not_a_field": 1}]},
		)
		with patch("frappe.new_doc", return_value=fake):
			create_draft_document(PURCHASE_INVOICE, {}, resolution)

		fake.append.assert_called_once_with("items", {"item_code": "ITEM-1", "qty": 2})

	def test_child_rows_on_a_non_table_field_raise_before_insert(self):
		fake = _fake_invoice()
		resolution = _resolution(child_rows={"supplier": [{"item_code": "ITEM-1"}]})
		with (
			patch("frappe.new_doc", return_value=fake),
			self.assertRaises(frappe.ValidationError),
		):
			create_draft_document(PURCHASE_INVOICE, {}, resolution)

		fake.insert.assert_not_called()

	def test_totals_within_one_cent_are_accepted(self):
		fake = _fake_invoice(grand_total=100.004)
		with patch("frappe.new_doc", return_value=fake):
			create_draft_document(
				PURCHASE_INVOICE,
				{"grand_total": 100.0},
				_resolution(),
				reconciliation_field="grand_total",
			)

		fake.insert.assert_called_once_with(ignore_permissions=True)

	def test_totals_mismatch_raises_after_the_insert(self):
		fake = _fake_invoice(grand_total=120.0)
		with (
			patch("frappe.new_doc", return_value=fake),
			self.assertRaises(TotalsMismatchError),
		):
			create_draft_document(
				PURCHASE_INVOICE,
				{"grand_total": 100.0},
				_resolution(),
				reconciliation_field="grand_total",
			)

		fake.insert.assert_called_once_with(ignore_permissions=True)

	def test_missing_extracted_total_is_flagged_for_review(self):
		fake = _fake_invoice(grand_total=120.0)
		resolution = _resolution()
		with patch("frappe.new_doc", return_value=fake):
			create_draft_document(PURCHASE_INVOICE, {}, resolution, reconciliation_field="grand_total")

		# Nothing was reconciled, so the draft goes to review rather than to Completed.
		self.assertTrue(resolution.needs_review)
		self.assertIn(MISSING_TOTAL_NOTE, resolution.notes)
		fake.insert.assert_called_once_with(ignore_permissions=True)

	def test_no_reconciliation_field_means_no_check_and_no_flag(self):
		fake = _fake_invoice(grand_total=120.0)
		resolution = _resolution()
		with patch("frappe.new_doc", return_value=fake):
			create_draft_document(PURCHASE_INVOICE, {"grand_total": 100.0}, resolution)

		self.assertFalse(resolution.needs_review)
		self.assertNotIn(MISSING_TOTAL_NOTE, resolution.notes)
		fake.insert.assert_called_once_with(ignore_permissions=True)

	def test_thousands_separator_in_the_extracted_total_is_compared(self):
		fake = _fake_invoice(grand_total=1234.5)
		with patch("frappe.new_doc", return_value=fake):
			create_draft_document(
				PURCHASE_INVOICE,
				{"grand_total": "1,234.50"},
				_resolution(),
				reconciliation_field="grand_total",
			)

		fake.insert.assert_called_once_with(ignore_permissions=True)
