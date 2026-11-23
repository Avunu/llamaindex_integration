# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Integration tests (spec section 8, item 3): mock LlamaCloud classify and extract, then run the real pipeline.

Only llama_client is mocked. Settings, the Llama Document Log, the llama-create lock, schema building, entity
resolution, document creation and the source File attachment all run for real.

TestTodoPipeline runs on any site, with a ToDo target and no ERPNext. ToDo is not submittable, and rule
validation refuses a target that is not submittable. So that test saves its rule with validation off. The
pipeline never checks that gate, so the run itself is the same as for any other target.

TestVendorInvoicePipeline needs ERPNext for its Purchase Invoice target. It is skipped when ERPNext is absent.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from llamaindex_integration import tasks
from llamaindex_integration.services import llama_client
from llamaindex_integration.tests import MINIMAL_PDF

LOG = "Llama Document Log"
EMAIL_ID = "llama-pipeline-test@example.com"

CLASSIFY_PATH = "llamaindex_integration.services.llama_client.classify"
EXTRACT_PATH = "llamaindex_integration.services.llama_client.extract"

# The ERPNext-free target. The duplicate key is sender, a Data field, which is an allowed key type. The
# description is Text Editor, which is not.
TODO_RULE = {
	"target_doctype": "ToDo",
	"classification_description": "A request for someone to follow up on a message from a vendor",
	"included_fields": "description,sender",
	"duplicate_key_fields": "sender",
	"default_values": json.dumps({"priority": "High"}),
}

# The ERPNext target. The company comes from default_values, not from a settings default.
COMPANY = "Llama Pipeline Test Co"
COMPANY_ABBR = "LPTC"
SUPPLIER_NAME = "Llama Pipeline Vendor"
ITEM_CODE = "LLT-PIPELINE-WIDGET"
PRICE_LIST = "Llama Pipeline Buying USD"
RULE_DESCRIPTION = "An invoice or bill sent by a vendor requesting payment for goods or services"
RULE_INCLUDED_FIELDS = (
	"supplier,bill_no,bill_date,posting_date,currency,buying_price_list,items,item_code,qty,rate,grand_total"
)
RULE_DUPLICATE_KEY_FIELDS = "supplier,bill_no"
INVOICE_RULE = {
	"target_doctype": "Purchase Invoice",
	"classification_description": RULE_DESCRIPTION,
	"included_fields": RULE_INCLUDED_FIELDS,
	"duplicate_key_fields": RULE_DUPLICATE_KEY_FIELDS,
	"default_values": json.dumps({"company": COMPANY}),
}

SAMPLE_INVOICE = {
	"supplier": SUPPLIER_NAME,
	"bill_date": "2026-11-20",
	"posting_date": "2026-11-23",
	# The site default currency is INR, so the invoice names the company's USD explicitly.
	"currency": "USD",
	"buying_price_list": PRICE_LIST,
	"items": [{"item_code": ITEM_CODE, "qty": 2, "rate": 50.0}],
	"grand_total": 100.0,
}


def _unique_pdf() -> bytes:
	"""A valid PDF whose bytes are unique to this call, so committed logs from earlier runs do not share a
	content_hash with this one and trigger the file-level duplicate check.
	"""
	return MINIMAL_PDF + f"% llama-test {frappe.generate_hash(length=16)}\n".encode()


def _unique_text(prefix: str) -> str:
	return f"{prefix} {frappe.generate_hash(length=8)}"


def _unique_email() -> str:
	return f"task-{frappe.generate_hash(length=8)}@vendor.example.com"


def _bill_no() -> str:
	return f"LLT-{frappe.generate_hash(length=8)}"


def _classification(doctype: str) -> llama_client.Classification:
	return llama_client.Classification(doctype=doctype, confidence=0.97, reasoning="Rule wording matches")


def _cloud_storage_attaches_by_association() -> bool:
	"""The same condition as the cloud branch of document_creator.attach_source_file."""
	return "cloud_storage" in frappe.get_installed_apps() and frappe.get_meta("File").has_field(
		"file_association"
	)


def _ensure_company():
	if not frappe.db.exists("Company", COMPANY):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": COMPANY,
				"abbr": COMPANY_ABBR,
				"default_currency": "USD",
				"country": "United States",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": "Standard",
			}
		).insert(ignore_permissions=True)


def _ensure_supplier():
	name = frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER_NAME}, "name")
	if not name:
		name = (
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": SUPPLIER_NAME, "supplier_type": "Company"}
			)
			.insert(ignore_permissions=True)
			.name
		)
	return name


def _ensure_item():
	if not frappe.db.exists("Item", ITEM_CODE):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM_CODE,
				"item_name": "Llama pipeline widget",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_purchase_item": 1,
			}
		).insert(ignore_permissions=True)
	return ITEM_CODE


def _ensure_price_list():
	"""A buying price list in the company's currency. The site's default list is in INR, and ERPNext then
	needs an exchange rate to build a USD draft.
	"""
	if not frappe.db.exists("Price List", PRICE_LIST):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": PRICE_LIST,
				"currency": "USD",
				"buying": 1,
				"selling": 0,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	return PRICE_LIST


def _ensure_email_account():
	name = frappe.db.get_value("Email Account", {"email_id": EMAIL_ID}, "name")
	if not name:
		name = (
			frappe.get_doc(
				{
					"doctype": "Email Account",
					"email_account_name": "Llama Pipeline Test",
					"email_id": EMAIL_ID,
					"enable_incoming": 0,
					"enable_outgoing": 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
	return name


def _configure_settings(email_account: str, rule: dict, *, bypass_rule_validation: bool) -> None:
	settings = frappe.get_single("LlamaIndex Settings")
	settings.enabled = 1
	settings.llama_cloud_api_key = "test-key"
	settings.monitored_email_account = email_account
	settings.doctype_rules = []
	settings.append("doctype_rules", dict(rule))
	# Rule validation refuses a target that is not submittable, so it refuses ToDo. Only the ERPNext-free run
	# sets this flag, to store its rule. The pipeline does not check the gate.
	settings.flags.ignore_validate = bypass_rule_validation
	settings.save(ignore_permissions=True)


def _restore_settings() -> None:
	"""Put the shared LlamaIndex Settings back to disabled, with no rules and no entity matches.

	The monitored inbox is left as it is. The field is mandatory, so it cannot be cleared, and a committed test inbox is
	harmless while the app is disabled.
	"""
	settings = frappe.get_single("LlamaIndex Settings")
	settings.enabled = 0
	settings.doctype_rules = []
	settings.entity_matches = []
	settings.save(ignore_permissions=True)


class _PipelineCase(IntegrationTestCase):
	"""Mocks the LlamaCloud classify and extract calls, then runs the real pipeline for one log.

	A subclass sets `rule` and `bypass_rule_validation`.
	"""

	def setUp(self):
		super().setUp()
		self.classify = self.enterContext(
			patch(CLASSIFY_PATH, return_value=_classification(self.rule["target_doctype"]))
		)
		self.extract = self.enterContext(patch(EXTRACT_PATH))
		_configure_settings(
			_ensure_email_account(), self.rule, bypass_rule_validation=self.bypass_rule_validation
		)
		frappe.db.commit()

	def tearDown(self):
		# setUp commits its rule and enables the shared Single, and that commit outlives the test. Undo it here, so no
		# later test loads this rule.
		_restore_settings()
		frappe.db.commit()
		super().tearDown()

	def _run_document(self, data: dict, *, content: bytes | None = None) -> frappe._dict:
		"""Queue a log for a File with the given bytes (unique bytes by default), run the real pipeline, and
		return the reloaded log.
		"""
		self.extract.return_value = llama_client.ExtractionResult(data=data, raw_json=json.dumps(data))
		source = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"llama-pipeline-{frappe.generate_hash(length=8)}.pdf",
				"content": content or _unique_pdf(),
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		log = frappe.get_doc(
			{
				"doctype": LOG,
				"status": "Queued",
				"source_file": source.name,
				"sender_email": "billing@vendor.example.com",
				"email_subject": "Document",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		tasks.process_document_log(log.name)

		return frappe.get_doc(LOG, log.name)

	def _assert_source_file_attached(self, log, doctype: str, docname: str) -> None:
		if _cloud_storage_attaches_by_association():
			# cloud_storage keeps one File Association row on the source File and makes no copy.
			self.assertEqual(
				frappe.db.count(
					"File Association",
					{
						"parent": log.source_file,
						"parenttype": "File",
						"parentfield": "file_association",
						"link_doctype": doctype,
						"link_name": docname,
					},
				),
				1,
			)
			return
		# Without cloud_storage, the creator attaches a copy of the source File to the document.
		source_url = frappe.db.get_value("File", log.source_file, "file_url")
		self.assertTrue(
			frappe.db.exists(
				"File",
				{"attached_to_doctype": doctype, "attached_to_name": docname, "file_url": source_url},
			)
		)


class TestTodoPipeline(_PipelineCase):
	"""ERPNext-free: runs on any site. The target is ToDo, which is not submittable (see the module docstring)."""

	rule = TODO_RULE
	bypass_rule_validation = True

	def test_task_becomes_draft_todo_with_default_values_and_source_file(self):
		data = {"description": _unique_text("Follow up on the invoice"), "sender": _unique_email()}

		log = self._run_document(data)

		self.assertEqual(log.status, "Completed", log.error_traceback)
		self.assertEqual(log.created_doctype, "ToDo")
		self.classify.assert_called_once()
		self.extract.assert_called_once()
		self.assertEqual(json.loads(log.raw_extracted_json), data)

		todo = frappe.get_doc("ToDo", log.created_docname)
		self.assertEqual(todo.sender, data["sender"])
		self.assertEqual(todo.priority, "High")
		self._assert_source_file_attached(log, "ToDo", todo.name)

	def test_second_task_from_the_same_sender_goes_to_review(self):
		sender = _unique_email()
		first = self._run_document({"description": _unique_text("First task"), "sender": sender})
		self.assertEqual(first.status, "Completed", first.error_traceback)

		second = self._run_document({"description": _unique_text("Second task"), "sender": sender})

		self.assertEqual(second.status, "Needs Review", second.error_traceback)
		self.assertFalse(second.created_docname)
		self.assertIn(first.created_docname, second.resolution_notes)
		self.assertEqual(frappe.db.count("ToDo", {"sender": sender}), 1)


class TestVendorInvoicePipeline(_PipelineCase):
	"""Needs ERPNext for the Purchase Invoice target. Skipped when ERPNext is not installed."""

	rule = INVOICE_RULE
	bypass_rule_validation = False

	def setUp(self):
		if "erpnext" not in frappe.get_installed_apps():
			self.skipTest("ERPNext is not installed on this site")
		super().setUp()
		_ensure_company()
		self.supplier = _ensure_supplier()
		_ensure_price_list()
		self.item = _ensure_item()
		frappe.db.commit()

	def test_vendor_invoice_becomes_draft_purchase_invoice_with_source_file(self):
		invoice = {**SAMPLE_INVOICE, "bill_no": _bill_no()}

		log = self._run_document(invoice)

		self.assertEqual(log.status, "Completed", log.error_traceback)
		self.assertEqual(log.created_doctype, "Purchase Invoice")
		self.classify.assert_called_once()
		self.extract.assert_called_once()
		self.assertEqual(json.loads(log.raw_extracted_json), invoice)

		invoice_doc = frappe.get_doc("Purchase Invoice", log.created_docname)
		self.assertEqual(invoice_doc.docstatus, 0)
		self.assertEqual(invoice_doc.company, COMPANY)
		self.assertEqual(invoice_doc.supplier, self.supplier)
		self.assertEqual(invoice_doc.bill_no, invoice["bill_no"])
		self.assertEqual(len(invoice_doc.items), 1)
		self.assertEqual(invoice_doc.items[0].item_code, self.item)
		self.assertEqual(invoice_doc.items[0].qty, 2)
		self._assert_source_file_attached(log, "Purchase Invoice", invoice_doc.name)

	def test_second_invoice_with_the_same_supplier_and_bill_no_goes_to_review(self):
		bill_no = _bill_no()
		first = self._run_document({**SAMPLE_INVOICE, "bill_no": bill_no})
		self.assertEqual(first.status, "Completed", first.error_traceback)

		second = self._run_document({**SAMPLE_INVOICE, "bill_no": bill_no})

		self.assertEqual(second.status, "Needs Review", second.error_traceback)
		self.assertFalse(second.created_docname)
		self.assertIn(first.created_docname, second.resolution_notes)
		self.assertEqual(
			frappe.db.count(
				"Purchase Invoice",
				{"supplier": self.supplier, "bill_no": bill_no, "docstatus": ["!=", 2]},
			),
			1,
		)

	def test_cancelled_invoice_does_not_block_the_same_file_or_bill_no(self):
		content = _unique_pdf()
		bill_no = _bill_no()
		first = self._run_document({**SAMPLE_INVOICE, "bill_no": bill_no}, content=content)
		self.assertEqual(first.status, "Completed", first.error_traceback)
		frappe.db.set_value("Purchase Invoice", first.created_docname, "docstatus", 2)
		frappe.db.commit()

		second = self._run_document({**SAMPLE_INVOICE, "bill_no": bill_no}, content=content)

		self.assertEqual(second.status, "Completed", second.error_traceback)
		self.assertNotEqual(second.created_docname, first.created_docname)
		self.assertEqual(frappe.db.get_value("Purchase Invoice", second.created_docname, "docstatus"), 0)
