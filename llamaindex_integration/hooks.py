# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

from . import __version__

app_name = "llamaindex_integration"
app_title = "LlamaIndex Integration"
app_publisher = "Avunu LLC"
app_description = (
	"Ingest documents from a monitored email account into ERPNext as draft transactions, using LlamaCloud"
)
app_email = "mail@avu.nu"
app_license = "MIT"
app_version = __version__

# Hook File, not Communication: Frappe inserts the Communication first and attaches each File
# right after, so the File is the first document that carries the attachment.
doc_events = {
	"File": {
		"after_insert": "llamaindex_integration.services.email_ingestion.handle_incoming_email_attachment",
	}
}

# Fail logs that stopped without a terminal status, so Retry Ingestion works (tasks.fail_stale_logs).
scheduler_events = {
	"cron": {
		"*/15 * * * *": ["llamaindex_integration.tasks.fail_stale_logs"],
	},
}
