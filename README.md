<!-- Copyright (c) 2026, Avunu LLC and contributors
For license information, please see license.txt-->

# LlamaIndex Integration

Ingest documents from a monitored email account into ERPNext. An attachment that arrives on the account is classified and extracted with LlamaCloud, the extracted values are mapped onto a Draft document, and the source file is attached to that document for side-by-side review.

Status: scaffold. The pipeline modules are stubs. The design is in [docs/build-plan.md](docs/build-plan.md).

## Setup

1. Install the app on a site: `bench --site <site> install-app llamaindex_integration`.
2. Open **LlamaIndex Settings**, save the LlamaCloud API key, choose the monitored Email Account, then tick **Enabled**. It is off until you do.
3. Add a **Document Rules** row for each target DocType, with a classification description.

## Behavior

- Documents are always created as Draft. Nothing is submitted in v1.
- Packet splitting is not implemented in v1.
- **Llama Document Log** records each ingested file, its state and the document it created. Failed and Needs Review logs can be retried from the form.
- Settings and logs are System Manager only. Logs hold raw personal and financial data.

## Development

- Tests run on the test site only: `bench --site test_site run-tests --app llamaindex_integration`.
- Lint with the ruff on PATH: `ruff check` and `ruff format --check` from the app directory.
