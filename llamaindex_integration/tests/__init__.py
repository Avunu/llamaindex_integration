# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

# A valid one-page PDF. Frappe parses every .pdf upload with pypdf (File.check_content), so a stub body
# such as b"%PDF-1.4 test" fails at insert.
MINIMAL_PDF = (
	b"%PDF-1.4\n"
	b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
	b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
	b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
	b"xref\n0 4\n"
	b"0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
	b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n186\n%%EOF\n"
)
