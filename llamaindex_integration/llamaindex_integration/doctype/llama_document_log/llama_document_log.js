// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

frappe.ui.form.on("Llama Document Log", {
	refresh(frm) {
		if (frm.is_new() || !["Failed", "Needs Review"].includes(frm.doc.status)) {
			return;
		}
		frm.add_custom_button(__("Retry Ingestion"), () => {
			frappe.call({
				method: "llamaindex_integration.api.retry_document_log",
				args: { log_name: frm.doc.name },
				freeze: true,
				callback() {
					frm.reload_doc();
				},
			});
		});
	},
});
