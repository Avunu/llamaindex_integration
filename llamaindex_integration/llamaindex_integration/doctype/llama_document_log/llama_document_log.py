# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

from frappe.model.document import Document

STATUSES = (
	"Queued",
	"Classifying",
	"Extracting",
	"Resolving Entities",
	"Completed",
	"Needs Review",
	"Failed",
	"Ignored",
)


class LlamaDocumentLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		classification_confidence: DF.Float
		classification_reasoning: DF.SmallText
		content_hash: DF.Data
		created_docname: DF.DynamicLink
		created_doctype: DF.Link
		detected_doctype: DF.Link
		email_subject: DF.Data
		error_traceback: DF.Code
		naming_series: DF.Literal["LLAMA-.YYYY.-.#####"]
		raw_extracted_json: DF.Code
		resolution_notes: DF.SmallText
		retry_count: DF.Int
		sender_email: DF.Data
		source_communication: DF.Link
		source_file: DF.Link
		status: DF.Literal[
			"Queued",
			"Classifying",
			"Extracting",
			"Resolving Entities",
			"Completed",
			"Needs Review",
			"Failed",
			"Ignored",
		]
	# end: auto-generated types

	def set_status(self, status: str, *, error: str | None = None) -> None:
		"""Set the state and, when given, the error text. The caller commits."""
		if status not in STATUSES:
			raise ValueError(f"Unknown Llama Document Log status: {status}")
		self.status = status
		if error is not None:
			self.error_traceback = error
