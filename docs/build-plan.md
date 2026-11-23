# Build plan: llamaindex_integration

Path prefixes: `frappe/` is the Frappe package and `bench/` is the bench root. SDK paths are relative to the installed `llama_cloud_services`, `llama_cloud` and `rapidfuzz` packages. "Reported" means taken from a report and not re-read by me. The build spec is not part of this repo.

## 1. Verified facts

**Frappe**
- `enqueue` takes `job_id` and `deduplicate` (dedupe requires `job_id`). Unknown kwargs such as `log_name` are passed to the job function (frappe/utils/background_jobs.py:76-93, 119-122, 184, 273). The job must accept them.
- `execute_job` re-runs a job up to 5 times on deadlock or lock timeout (frappe/utils/background_jobs.py:275-290). The `rollback(chain=True)` I confirmed is in the InternalError/RetryBackgroundJobError branch (lines 275-276). The spec review's claim that generic exceptions take that path is not shown.
- `frappe.db.savepoint(name)` is a method that issues SAVEPOINT and returns None. Undo with `frappe.db.rollback(save_point=name)` (frappe/database/database.py:1249-1253, 1272-1283). The module-level `savepoint(catch=Exception)` context manager swallows exceptions by default (database.py:1631-1652). Its docstring warns against savepoints around filesystem writes (database.py:1272-1280).
- A savepoint rollback does not discard queued `enqueue_after_commit` jobs. A full rollback does (database.py:1249-1256; frappe/utils/background_jobs.py:205-207).
- Mail order: Communication insert (frappe/email/receive.py:748-750), reload (753), each File saved in `save_attachments_in_doc` (640-652, 756), then content rewritten and saved (757-758). So `File.after_insert` runs before the final Communication content exists.
- `save_attachments_in_doc` catches only `MaxFileSizeReachedError`, `FileAlreadyAttachedException`, `DuplicateEntryError`, and `ValidationError` (receive.py:657-670). Anything else propagates.
- `EmailAccount.receive` commits per mail. Any other exception rolls back that mail and logs an error (frappe/email/doctype/email_account/email_account.py:688-717).
- File dedupe reuses an existing file only when content_hash, is_private, and the same attached_to pair all match (frappe/core/doctype/file/file.py:545-566). It is not global by file_url.
- The File doctype has no inline flag. The Content-Id map exists only in memory during parsing (receive.py:631-633).
- `scrub` lowercases and replaces spaces and hyphens with underscores (frappe/__init__.py:846-849). So "LlamaIndex Settings" maps to folder `llamaindex_settings`.
- Test bases: `IntegrationTestCase` (frappe/tests/classes/integration_test_case.py:20, extends `UnitTestCase` at unit_test_case.py:29). `DataStewardIntegrationTestCase` is at `bench/apps/data_steward/data_steward/tests/__init__.py:40`.
- `get_doc(..., for_update=True)` exists (frappe/model/document.py:300). `filelock` exists but is per host (frappe/utils/synchronization.py:15).
- Dynamic Link validation needs the sibling doctype set before insert (reported: document.py:3022-3053, 717-718). `before_insert` runs too late to fix it.

**Bench and registry**
- Membership is driven by root `pyproject.toml` `[tool.uv.workspace].members` (bench/pyproject.toml:201-249). `sites/apps.txt` is generated. It currently lists `frappe` (line 1) and `data_steward` (line 15). `uv.lock` has zero `llama` entries (grep count 0).
- An app in apps.txt is auto-installed on the FRAPPE_SITE by `devenv up` (reported: reconcile.py:82-113). `install-app` on test_site also requires the apps.txt entry (reported: installer.py:317-318).
- The sandbox egress allowlist permits only `nominatim.openstreetmap.org` and `api.github.com`. The LlamaCloud default host is `https://api.cloud.llamaindex.ai` (`llama_cloud/environment.py:7`).

**Python**
- frappe `>=3.14,<3.15` (bench/apps/frappe/pyproject.toml:7). Root `>=3.14` (bench/pyproject.toml:7). erpnext `>=3.14` (bench/apps/erpnext/pyproject.toml:7). cloudflare_email_delivery and data_steward `>=3.14` (pyproject:5). hrms `>=3.10` (bench/apps/hrms/pyproject.toml:7).
- An internal dev note says "prod is 3.12".

**SDK (wheels, read statically)**
- `llama-cloud-services` 0.6.94 emits a DeprecationWarning at import pointing to `llama-cloud>=1.0` (`llama_cloud_services/__init__.py:15-19`, verified). Its metadata requires `llama-cloud==0.1.46`, `llama-index-core>=0.12.0`, and `pydantic!=2.10,>=2.8` (reported METADATA:8-17).
- Classify: `LlamaClassify.from_api_key(api_key, ...)` (classifier/client.py:59-73, reported). `aclassify` accepts str/Path inputs (classifier/client.py:167-190, verified). Rules are `ClassifierRule(type, description)` (client.py:77, reported).
- Extract: `LlamaExtract.extract(data_schema, config, files)` (`extract/extract.py:999-1004`, verified). `config` is required. `ExtractConfig(extraction_target=ExtractTarget.PER_DOC, extraction_mode=ExtractMode.MULTIMODAL)` is the pattern at extract.py:49-52 (verified). Default `max_timeout` is 2000 s (reported extract.py:527-530). Passing `api_key` explicitly avoids the env fallback (reported extract.py:568-571).
- Split: no split client in 0.6.94 (reported grep). Raw `llama_cloud` beta `create_split_job` and `get_split_job` exist (reported `llama_cloud/resources/beta/client.py:5376, 5436`).
- rapidfuzz: `extractOne` defaults to `scorer=WRatio` (`rapidfuzz/process_py.py:25`, verified). It returns a tuple or None when below `score_cutoff` (reported process_py.py:196).
- Indel-ratio arithmetic, computed by LCS: "Widget 10mm" vs "Widget 100mm" = 95.65. "Warehouse A" vs "Warehouse B" = 90.91. Both are above the spec's threshold of 85.

**Disagreements and which evidence wins**
- Fuzzy scorer: spec review calls WRatio unverified and computes 88.9. The wheel source confirms WRatio (process_py.py:25), so source wins. The review's arithmetic is wrong, since the Indel ratio is 95.65. The false-accept conclusion still holds.
- Python: spec review B7 says every app requires `>=3.14`. hrms says `>=3.10`. The pyproject pins outrank the internal dev note, so the prod-3.12 claim is unresolved.
- Retry path: spec review says generic exceptions roll back with `chain=True`. Only the InternalError branch shows that (background_jobs.py:275-276).
- File dedupe: frappe-hooks says duplicates are reused "by file_url". file.py:545-566 limits reuse to the same attached target, so the code wins.

## 2. Spec corrections

1. **§2 `llama-cloud-services>=0.6.0` for LlamaParse, Classify, Split, Extract.** Reality: no LlamaParse or Split client; the package is deprecated and pins `llama-cloud==0.1.46`. Change: pin `llama-cloud-services==0.6.94`, import only behind the seam (section 3.2), and drop Split.
2. **§2 `pydantic>=2.0.0`.** Reality: the SDK requires `>=2.8,!=2.10`. Change: remove the line and let the SDK pull pydantic, or pin `pydantic>=2.8,!=2.10`.
3. **§2 `rapidfuzz>=3.0.0`.** Reality: erpnext requires `~=3.14.3` (bench/apps/erpnext/pyproject.toml:14). Change: `rapidfuzz>=3.14.3,<4`.
4. **§2 "Python 3.14+".** Reality: frappe caps at `<3.15`. Change: `requires-python = ">=3.14"` (section 3.5).
5. **§3 is empty.** Change: use the tree in section 4.
6. **§6 Step 1 `timeout=600`.** Reality: LlamaExtract defaults to 2000 s. Change: job `timeout=1500`, extract `max_timeout_s=900`, and `job_id` plus `deduplicate=True` (section 3.3).
7. **§6 Step 1 hook body.** Reality: `ValidationError` is swallowed, and any other exception rolls back the whole mail (receive.py:657-670; email_account.py:688-717). Change: wrap the whole hook body in `try/except Exception`, log the file name only, and never re-raise.
8. **§6 Step 1 hook decides inline status.** Reality: Content-Id references are unknown at `after_insert` (receive.py:748-758). Change: the hook only guards, creates the log, and enqueues. The job checks inline status against the final Communication content.
9. **§6 Step 3.2 classify rules as dicts.** Reality: typed `ClassifierRule` objects (classifier/client.py:77). Change: build `ClassifierRule` inside the seam.
10. **§6 Step 3.2 "LlamaParse Classify".** Reality: `LlamaClassify.classify(rules, files)` returns per-file `item.result`, which can be None. Change: None maps to Needs Review.
11. **§6 Step 3.1 LlamaParse Split.** Reality: no client; needs raw beta API and a separate upload. Change: remove from v1, drop `enable_packet_splitting`, and drop child-log logic.
12. **§6 Step 3.3 LlamaExtract "with file path and schema".** Reality: `extract(data_schema=..., config=..., files=...)`, and `config` is required (extract.py:999-1003). Change: pass the ExtractConfig pattern from extract.py:49-52. Store `run.data`. Serialize with `.json()`, not `model_dump()`, because the models use pydantic.v1 (reported extract_run.py:15-20).
13. **§6 Step 2 `included_fields: list[str] | None`.** Reality: §4.2 defines it as Small Text. Change: parse the comma string in `parse_included_fields`.
14. **§6 Step 2.2 excludes read-only fields, but the default includes `grand_total` and `net_total`.** Reality: both are read-only (reported purchase_invoice.json). Change: exclude read-only fields from extraction and use totals only for reconciliation.
15. **§6 Step 2.3 Table recursion with no guard; Dynamic Link typed as string.** Reality: Dynamic Link needs a sibling doctype (reported document.py:3049-3053). Change: depth cap of 2, skip Dynamic Link, Attach, and Password.
16. **§4.2 `auto_submit` and §6 Step 5.4 submit.** Reality: fuzzy matches are accepted silently, and the wrong item can post (95.65 example above). Change: remove `auto_submit` from v1. Documents are always Draft.
17. **§6 Step 4.1 tax ID lookup.** Reality: an empty value becomes an `IS NULL` match (reported query.py:705-730). Change: skip the lookup when the value is empty.
18. **§6 Step 4.2 Match 2 part number.** Reality: not scoped to the resolved supplier. Change: filter `Item Supplier` by supplier.
19. **§6 Step 4.3 validates four Link types.** Reality: `credit_to` (reqd), `expense_account`, `taxes_and_charges`, and `project` are unchecked (reported). Change: validate every Link against its options.
20. **§6 Step 5.1 duplicate guard on supplier and bill_no.** Reality: no company, no lock, and an empty bill_no matches `IS NULL`. Change: `frappe.db.exists("Purchase Invoice", {"company", "supplier", "bill_no", "docstatus": ("!=", 2)})` under `filelock`. Empty bill_no goes to Needs Review.
21. **§6 Step 5.2 no totals reconciliation.** Change: compare the extracted `grand_total` to the computed one. A mismatch beyond tolerance goes to Needs Review.
22. **§6 Step 5.3 "duplicate or link" the File.** Reality: `attached_to_*` is a single target (reported file.json). Change: insert a new File with the same `file_url` attached to the new document. The hook guard (`attached_to_doctype != "Communication"`) must stop re-entry.
23. **§7 `frappe.db.savepoint("llama_doc_creation")`.** Reality: a bare method, with no rollback or release. Change: `sp=...; frappe.db.savepoint(sp)`, then on exception `frappe.db.rollback(save_point=sp)` and re-raise. Write Failed only after rollback. Do not write files inside the savepoint.
24. **§4.3 status list and fields.** Reality: no Ignored state and no retry counter. Change: add `Ignored` status, `retry_count` (Int), and `content_hash` (Data).
25. **§4.1 `enabled` defaults to 1 with a reqd key.** Change: default 0, so mail does not fail before the key is set.
26. **§4.1 `default_expense_account`** is never used (spec review N4). Change: remove it.
27. **§4.1/§6 Step 4 auto-create.** Reality: `Supplier.supplier_type`, `Item.item_group`, and `stock_uom` are reqd (reported). Change: add `default_supplier_type` and `default_supplier_group` settings. Auto-create stays off by default.

## 3. Decisions where the spec is silent

**3.1 Location and registration.** Create `bench/apps/llamaindex_integration` as a git submodule, matching the convention that every `apps/<app>` is one. Scaffold with the bench app generator, which runs upstream `bench new-app --no-git`, then add-app, sync-registry, uv sync, and direnv reload (reported new_app.py:42-55). Upstream prompts interactively and calls api.github.com for licenses (reported boilerplate.py:34-115). Pass answers explicitly and verify. Registration writes the root workspace members and source. Do not run `devenv up` until test_site passes, because apps.txt drives auto-install on the production site.

**3.2 SDK seam.** The only lazy import of `llama_cloud_services` is `_load_sdk(api_key)` in `services/llama_client.py`. No other module imports it. Unit tests patch `_load_sdk` with fakes, so the deprecation warning and llama-index-core never load in tests. Public functions return app-owned dataclasses, so SDK types never leave the module. Egress is blocked by the sandbox allowlist anyway. Add the LlamaCloud host only for a live check.

**3.3 Idempotency.**
- The log is the unit of work. Enqueue with `job_id=f"llama-log-{name}-a{retry_count}"` and `deduplicate=True` (background_jobs.py:89-90, 119-122). The attempt number makes ids distinct, because a STARTED job with the same id would block its own retry (`is_job_enqueued` counts STARTED as active, background_jobs.py:676-677). The exact dedupe condition is unverified.
- Checkpoint after each stage: classification fields, then extracted data. Commit after each. A retry skips stages that already have output, so LlamaCloud is not billed twice.
- The Purchase Invoice insert and `created_docname` update share one transaction, committed once. Retry refuses when `created_docname` is set, in both api.py and tasks.py.
- Auto-retry: once, only for `LlamaTransientError`, with the attempt counter incremented. Catch everything in the job and never re-raise, so `execute_job` does not re-run it.

**3.4 Logging and redaction.**
- The API key stays a Password field, read with `get_password` at call time. It is never logged or included in exception text. Keep SDK and httpx loggers at WARNING.
- Structured log lines carry log name, stage, status, doctype, counts, durations, and exception class only. No file bytes, extracted values, tax IDs, bank data, sender address, or subject.
- `error_traceback` holds the traceback, truncated to 8 KB, with request URLs and bodies stripped. Do not pass payloads to `frappe.log_error`.
- `raw_extracted_json` is kept (spec). Permission is System Manager only. Retention is open (question 3).

**3.5 Python pin.** `requires-python = ">=3.14"`, matching root, erpnext, cloudflare, and data_steward. Frappe's `<3.15` caps the bench anyway. The spec review's `<3.15` addition is redundant but harmless.

**3.6 Savepoint.** Use the explicit method pattern from correction 23. Do not use the module-level context manager, because it swallows exceptions.

**3.7 Inline images.** The job checks whether the File's `file_url` or unique URL appears in the final `Communication.content` (receive.py:757, reported). If it does, status is Ignored. Also filter by extension and size.

**3.8 Split.** Out of v1. It needs a second raw client (constructor unverified), a file upload, and child logs that have no source File (spec review S10).

**3.9 Auto-submit.** Removed in v1 (correction 16).

**3.10 Naming.** `app_title` and modules.txt entry are "LlamaIndex Integration". The module folder is `llamaindex_integration`. Doctype folders are `llamaindex_settings`, `llama_doctype_rule`, and `llama_document_log`. Classes are `LlamaIndexSettings`, `LlamaDocTypeRule`, and `LlamaDocumentLog`. JSON `module` is "LlamaIndex Integration".

**3.11 Tests.** Pure modules get site-free tests (`python -m unittest`). Doc-backed tests use `IntegrationTestCase` on test_site via `bench --site test_site run-tests --app llamaindex_integration`. Never use FRAPPE_SITE.

**3.12 SDK version.** Stay on 0.6.94 behind the seam. Revisit if the maintainer requires llama-cloud>=1.0 (question 4).

**3.13 Duplicate guard uses a Supplier row lock (replaces the filelock in 2.20).** A host filelock is released before tasks.py commits, and a plain read under REPEATABLE READ cannot see another job's commit that came after the job's first read. So the job takes `get_value("Supplier", ..., for_update=True)`, held until commit, and then runs the duplicate lookup as a locking read (`for_update=True`). Empty supplier or bill_no skips both.

**3.14 Attempts.** A job carries the attempt it was enqueued for. A job whose attempt is no longer the log's retry_count returns at once, so a Retry clicked while an automatic retry is queued does not run both. A residual window remains: a job that passed this check before a Retry commits can make one LlamaCloud call before its next checkpoint, and its stale save is then rejected.

**3.15 Stale logs.** `fail_stale_logs` runs every 15 minutes (hooks.py). A non-terminal log unchanged for 2 x JOB_TIMEOUT_S, whose RQ job is neither queued nor started, becomes Failed with a retry message. This covers a killed worker and a Queued log whose enqueue failed at commit.

**3.16 Fuzzy-accepted matches set Needs Review (deliberate).** Spec 6.4 says "accept if score >= threshold". This build keeps the match and creates the draft, but sets needs_review, so a person confirms it. That is the stricter reading of correction 16, and test_fuzzy_match_above_threshold_is_noted_and_flagged pins it.

**3.17 Stored extraction.** raw_extracted_json holds run.data (correction 12). The ExtractRun envelope written by earlier runs is unwrapped on retry. On retry the extracted data is also filtered to the top-level keys of the schema.

**3.18 Totals and MandatoryError.** An extraction with no grand_total is flagged Needs Review, because nothing was reconciled. A MandatoryError on insert (for example an unmatched supplier that was left blank) rolls back to Needs Review, with the resolver notes and the reason. Records created inside the rolled-back savepoint are named in the notes as not kept.

**3.19 Stored text.** Tracebacks keep the tail (the raising frame and the exception line are the last lines). The configured LlamaCloud key is replaced with `<redacted>`. SDK exceptions are not chained into the app errors, so a response body does not reach error_traceback.

**3.20 Document Rule targets.** A Llama DocType Rule target must be a submittable DocType. Singles, child tables and masters such as User are refused, because drafts are inserted with ignore_permissions.

## 4. File tree

```
apps/llamaindex_integration/
  pyproject.toml  README.md  license.txt  .editorconfig  .gitignore
  .pre-commit-config.yaml   (frappe-nix managed; do not hand-edit)
  llamaindex_integration/
    __init__.py            (__version__ with x-release-please markers)
    hooks.py  modules.txt  patches.txt  patches/__init__.py
    api.py  tasks.py
    services/
      __init__.py  email_ingestion.py  schema_builder.py  llama_client.py
      entity_resolver.py  document_creator.py
    llamaindex_integration/
      __init__.py
      doctype/
        llamaindex_settings/   {__init__.py, llamaindex_settings.json, llamaindex_settings.py}
        llama_doctype_rule/    {__init__.py, llama_doctype_rule.json, llama_doctype_rule.py}
        llama_document_log/    {__init__.py, llama_document_log.json, llama_document_log.py, llama_document_log.js}
    tests/
      __init__.py  test_schema_builder.py  test_llama_client.py  test_entity_resolver.py
      test_document_creator.py  test_email_ingestion.py  test_tasks.py  test_api.py  test_pipeline.py
```

## 5. Interface contract

**`services/llama_client.py`** (owner: SDK workstream)
- `@dataclass(frozen=True) class Rule: doctype: str; description: str`
- `@dataclass(frozen=True) class Classification: doctype: str | None; confidence: float; reasoning: str`
- `@dataclass(frozen=True) class ExtractionResult: data: dict[str, Any]; raw_json: str`
- `class LlamaTransientError(Exception)`: timeouts, httpx transport errors, ApiError 408/425/429/5xx, extract poll timeout.
- `class LlamaPermanentError(Exception)`: other 4xx, classify job ERROR, extract run ERROR, no run returned.
- `def classify(api_key: str, rules: Sequence[Rule], file_path: str) -> Classification`. Raises the two errors above.
- `def extract(api_key: str, schema: dict[str, Any], file_path: str, *, max_timeout_s: int = 900) -> ExtractionResult`. Raises the two errors above.
- `def _load_sdk(api_key: str) -> Any`. The only function that imports `llama_cloud_services`.

**`services/schema_builder.py`** (owner: Schema workstream)
- `def build_json_schema_for_doctype(target_doctype: str, included_fields: list[str] | None = None) -> dict[str, Any]`. Raises `frappe.DoesNotExistError`, or `SchemaError(ValueError)` for an unknown field or an empty schema.
- `def parse_included_fields(raw: str | None) -> list[str] | None`
- `def fieldtype_to_schema(fieldtype: str, label: str, options: str | None) -> dict[str, Any] | None`. Pure. None means unsupported.

**`services/entity_resolver.py`** (owner: Resolver workstream)
- `@dataclass class Resolution: values: dict[str, Any]; child_rows: dict[str, list[dict[str, Any]]]; notes: list[str]; needs_review: bool; created: list[tuple[str, str]]`
- `def best_fuzzy_match(query: str, choices: Mapping[str, str], threshold: int) -> str | None`. Pure. Returns the key. Rejects a match whose digit tokens differ.
- `def resolve_party(party_doctype: str, name: str | None, tax_id: str | None, settings: Any, res: Resolution) -> str | None`. Appends notes. Never raises.
- `def resolve_entities(target_doctype: str, extracted: dict[str, Any], settings: Any) -> Resolution`. May auto-create masters when enabled. Called inside the creator's savepoint.

**`services/document_creator.py`** (owner: Creator workstream)
- `class DuplicateDocumentError(frappe.ValidationError)`, `class TotalsMismatchError(frappe.ValidationError)`
- `def create_draft_document(target_doctype: str, extracted: dict[str, Any], resolution: Resolution, settings: Any) -> str`. Opens the savepoint, inserts with `ignore_permissions`, does not commit. Returns the name. Raises the errors above, or `frappe.ValidationError` from insert.
- `def attach_source_file(source_file: str, target_doctype: str, target_name: str) -> str`. Returns the new File name.

**`services/email_ingestion.py`** (owner: Ingestion workstream)
- `def handle_incoming_email_attachment(doc: Any, method: str | None = None) -> None`. Never raises.
- `def is_candidate_file(file_doc: Any, settings: Any) -> bool`

**`tasks.py`** (owner: Pipeline workstream)
- `def enqueue_processing(log_name: str, attempt: int = 0) -> str`. Returns the job id.
- `def process_document_log(log_name: str) -> None`. Catches everything. Writes a terminal status. Does not re-raise.

**`api.py`** (owner: Ingestion workstream)
- `@frappe.whitelist(methods=["POST"]) def retry_document_log(log_name: str) -> str`. Raises `frappe.PermissionError`, or `frappe.ValidationError` when `created_docname` is set or the status is not Failed or Needs Review.

**Doctype controllers** (owner: Doctype workstream)
- `class LlamaIndexSettings(Document)` with `validate()`, plus module function `get_settings() -> LlamaIndexSettings`, which calls `frappe.get_single`.
- `class LlamaDocumentLog(Document)` with `set_status(status: str, *, error: str | None = None) -> None`. The caller commits.

**Call graph:** hooks.py → email_ingestion. email_ingestion → get_settings, enqueue_processing. api → enqueue_processing. tasks → get_settings, llama_client.classify and extract, schema_builder, entity_resolver, document_creator. document_creator → entity_resolver (types). Only llama_client touches the SDK.

## 6. Build order and ownership

- **Step 0 (Lead, sequential):** scaffold via the bench app generator. Owns `pyproject.toml`, `modules.txt`, `hooks.py` skeleton, `README.md`, and the git submodule. Registration happens here.
- **Step 1 (parallel, after Step 0):**
  - Doctype workstream owns `doctype/**`. Its JSON field names are needed by Steps 2 and 3.
  - SDK workstream owns `services/llama_client.py` and `tests/test_llama_client.py`.
  - Schema workstream owns `services/schema_builder.py` and `tests/test_schema_builder.py`. It needs no new doctypes.
- **Step 2 (parallel, after Step 1 doctype JSON):**
  - Resolver/Creator workstream owns `entity_resolver.py`, `document_creator.py`, and their tests.
  - Ingestion workstream owns `services/email_ingestion.py`, `api.py`, `hooks.py`, and their tests.
- **Step 3 (after Steps 1 and 2):** Pipeline workstream owns `tasks.py` and `tests/test_pipeline.py`.
- **Step 4 (Lead, sequential):** resolve the lock in a scratch copy first (`uv lock --dry-run`; flag existence unverified). If it conflicts, add an override in root `[tool.uv] override-dependencies` (reported pyproject.toml:42-69). Then `update-deps`, commit `uv.lock` and `sites/apps.json`, `bench --site test_site install-app`, and `bench --site test_site run-tests --app llamaindex_integration`.
- **Tooling:** use the PATH `ruff`, not `uv run --project tools`. The latter re-syncs the bench env (internal dev note; reported gate.py:217).

## 7. Questions for the maintainer

No question blocks writing the code. The following block specific steps, and each has a default.

1. **Apps.txt and the production site (blocks the devenv step only).** Registration makes the next `devenv up` install the app on the production site. Default: do not run `devenv up` until test_site passes.
2. **Submodule remote (blocks the submodule commit only).** Which GitHub owner and repo? Default: local git repo, submodule added later.
3. **Third-party data and PII (blocks enabling in production only).** Approve sending vendor and customer documents to LlamaCloud, and storing extracted data in `raw_extracted_json`, plus retention. Default: build it, ship `enabled=0`, System Manager read only.
4. **Deprecated SDK (changes the `_load_sdk` body only).** Accept `llama-cloud-services` 0.6.94, or move to `llama-cloud>=1.0`, whose API I have not inspected? Default: 0.6.94.
5. **Packet splitting in v1 (changes scope).** Default: no.
6. **Prod Python (blocks deploy only).** An internal note says prod is 3.12, but frappe requires 3.14. Default: 3.14.
7. **Contact match without review (spec 6.4, step 3).** A sender Contact match is accepted silently even when the invoice names another party, so the draft can go to the Contact's supplier. The spec puts Contact in the exact-match tier, and tests pin this. Decide whether to flag a Contact match whose name differs from the extracted name.
8. **Auto-created masters (spec 6.4, fallback).** A supplier or item created from an OCR-garbled name is noted but not flagged. The spec asks only for a note. Decide whether creation should also set Needs Review.
9. **Ignored status (spec 7).** The small-image rule (png, jpg, jpeg, gif under 15 KB) and the file_url-in-body rule set Ignored, which has no Retry. A small real scan is dropped. Decide whether Ignored should be retryable or should route to Needs Review.
10. **Duplicate guard for other target types (spec 6.5.1).** Only Purchase Invoice is guarded, as the spec says. Decide whether Sales Order or Expense Claim rules need their own check.
11. **Sender allowlist and daily cap (spec 4.1).** The monitored account is the only trust boundary, as the spec says. Decide whether to add a sender allowlist or a cap on logs per day, which means new settings.
12. **Savepoint rollback and queued jobs (frappe).** A savepoint rollback keeps `enqueue_after_commit` jobs queued by a rolled-back insert (section 1). Such a job would then act on a document that no longer exists. The fix belongs in frappe or needs a per-doctype rule.

## Lead overrides

- Standalone git repository, not a submodule of the bench repository. No commits.
- enable_packet_splitting: keep the field, default 0, with a description saying packet splitting is not implemented in v1. No split code.
- auto_submit: keep the field, default 0. In v1 nothing is ever submitted; documents are always Draft.
- default_expense_account: keep the field. It is unused in v1.
- enabled: default 0 instead of 1, so no job runs before the API key is set.
- LlamaIndex Settings also gets: default_supplier_type (Link Supplier Type), default_supplier_group (Link Supplier Group), default_customer_group (Link Customer Group), default_territory (Link Territory).
- Llama Document Log also gets: status option Ignored; retry_count (Int, default 0); content_hash (Data).
- Savepoint ownership: tasks.py alone opens and rolls back the savepoint named llama_doc_creation. It wraps resolve_entities, create_draft_document, and attach_source_file. Service functions never open, release, or commit savepoints. The classification and extraction checkpoints are committed before the savepoint opens.
- Read-only fields are excluded from extraction. grand_total is still compared with the computed total in document_creator.
- Table recursion is capped at depth 2. Dynamic Link, Attach, Attach Image, and Password fields are skipped.

## Generic duplicate guard, attachment, and lock (v2)

This replaces the Purchase Invoice-only guard in correction 20 and section 3.13. Those entries stay as written.

**Why ERPNext is no longer needed for the guard.** The earlier guard checked `supplier` and `bill_no` on Purchase Invoice, so ERPNext was a prerequisite for the duplicate check. The key is now configured per rule, and the guard, lock, and attach code paths name no ERPNext DocType or field. ERPNext appears only in tests, as fixtures. Any submittable DocType can be a rule target with its own key.

**Design.** Every owner implements this contract.
1. **Rule config.** `Llama DocType Rule.duplicate_key_fields` is a Small Text, optional field. It holds comma-separated parent fieldnames of the target, for example `supplier,bill_no`. Blank means no key check. `services/document_creator.parse_duplicate_key_fields(raw)` strips whitespace, drops blanks, and keeps order. The rule controller checks each name against the target meta. The field must exist as a parent field, and its fieldtype must be one of Data, Link, Select, Int, Float, Currency, Percent, or Date. Text fields are out, because Frappe refuses a search index on them. Table fields fail. The field must not be read only, and it must have Search Index on or be unique, so the key lookup does not lock the whole table. The target must stay submittable, and `included_fields` must name fields of the target. Frappe never runs `validate()` on a child row, so `LlamaIndexSettings.validate()` calls the rule check for each of its rows on every save.
2. **Key check.** `create_draft_document(target_doctype, extracted, resolution, settings, *, duplicate_key_fields=())` builds the parent values after resolution. Empty key tuple skips the check. If any key value is empty, the check is skipped, a note names the empty field, `resolution.needs_review` is set, and the insert proceeds. Otherwise a locking Query Builder read (`for_update`) looks for an existing document with the same key values. Cancelled rows (docstatus 2) are ignored, because targets are submittable. A match raises `DuplicateDocumentError`, which names the doctype, the existing name, and the key fields. Company is set only when the target has a company field and `settings.default_company` is set. The insert uses `ignore_permissions=True` and does not commit. The Purchase Invoice code (`_lock_supplier`, `_raise_if_duplicate`, and `PURCHASE_INVOICE`) is removed.
3. **Attachment.** `attach_source_file(source_file, target_doctype, target_name)` runs as Administrator and restores the previous user with `frappe.set_user`. It calls `create_attachment_copy(ignore_permissions=True)`. A job running as a user with no read rights on a private source file can therefore still attach the original to the created document.
4. **Lock.** `services/locks.py` provides `LockTimeoutError` and the `create_lock(name, *, ttl_s=300, wait_s=120, poll_s=0.5)` context manager. It uses `frappe.cache()`, a `redis.Redis` subclass. The key is `llama-lock:<site>:<name>`, so sites on one bench do not block each other. The value is a random token, and the set uses `nx=True, ex=ttl_s`. It polls until acquired and raises `LockTimeoutError` after `wait_s`. Release is a Lua compare-and-delete, registered with `register_script`, so a lock is freed only by its own token. A release error is logged and never raised, and it never masks the caller's exception.
5. **Job flow (tasks.py, stage 5).** If `log.content_hash` is empty, copy the source File `content_hash` into it. Then take two locks before the savepoint opens, and hold them until after the final commit. First, if `content_hash` is set, `create_lock("llama-file:" + content_hash, ttl_s=JOB_TIMEOUT_S)`, which serializes every log for one file whatever its doctype. Second, `create_lock("llama-create:" + target_doctype, ttl_s=JOB_TIMEOUT_S)`. Every job takes the file lock before any doctype lock, so no job waits for a file lock while it holds a doctype lock. The TTL equals the job timeout, so a key cannot expire while its live holder is still running.
   - File-level duplicate: another Llama Document Log, not this one, with the same `content_hash`, status Completed or Needs Review, and a `created_docname` whose document exists and is not cancelled. A match raises `DuplicateDocumentError`, and the existing handler sets Needs Review.
   - Savepoint `llama_doc_creation`: `resolve_entities(..., sender_email=log.sender_email)`, then `create_draft_document(..., duplicate_key_fields=parse_duplicate_key_fields(rule.duplicate_key_fields))`, then `attach_source_file`. Release the savepoint, then commit.
   - Both locks are released in a `finally` block after the commit, on every path. The doctype lock is released first. `LockTimeoutError` is transient, with one automatic retry under the same policy as `LlamaTransientError`.
   - The rule comes from the existing rule lookup for the detected doctype. No DB read happens between the previous checkpoint commit and taking the lock.

**Decision: rule targets stay submittable.** The insert runs with `ignore_permissions`, so a rule target must be a transaction DocType. Section 3.20 already requires this, and it stays in force. Because targets are submittable, the guard always ignores cancelled rows.

**Status.** The rule DocType and its validation are in place. The controller imports `parse_duplicate_key_fields` from `services/document_creator.py`, so the import resolves only once that function is exported. The guard, lock, attach, and job-flow code belong to their own owners.

## v3: ERPNext-free matching and cloud_storage attachment

This section supersedes the ERPNext-specific entries in the lead overrides and in v2 where they conflict. Those entries stay as written.

**No ERPNext dependency.** Production code names no ERPNext DocType or field: Company, Supplier, Customer, Item, Item Group, UOM, Purchase Invoice, bill_no, or grand_total. The app runs on a Frappe site without ERPNext. Tests use ERPNext only as a fixture, in the one class that skips when ERPNext is absent. Schema building, entity matching and attachment all read the target's own metadata.

**Llama Entity Match (new child table).** One row per linked doctype, held in `LlamaIndex Settings.entity_matches`. Fields:
- `link_doctype` (Link to DocType, required).
- `identifier_fields` (Small Text): comma-separated fields matched exactly. Each must exist and be indexed or unique.
- `fuzzy` (Check, default 0): fuzzy matching on the title field. It requires a title field that is not Text Editor, Text or Long Text.
- `auto_create` (Check, default 0).
- `match_sender_contact` (Check, default 0): match a Contact whose email is the sender, then its Dynamic Link to `link_doctype`.
- `field_map` (Code, JSON object): maps each extracted key to the linked field it fills. The object key is the extracted key and the value is a field of `link_doctype`, so `{"uom": "stock_uom"}` fills `stock_uom` from the extracted `uom`. A linked field that is not a value is filled from the extracted key of the same name.
- `default_values` (Code, JSON object): values for auto-created records.

A Link field with no row for its doctype uses exact name and exact title only. There is no fuzzy match, no contact match and no auto-create. The full order is in the resolver contract: exact name, exact title, identifier fields, sender Contact, fuzzy, auto-create, then drop the value and flag review.

**Llama DocType Rule.** Adds two fields. `default_values` (Code, JSON object) keys are target fieldnames, or `table.fieldname` for child rows. `reconciliation_field` (Data, blank means none) must name a numeric, non-Table, non-Link field of the target. It is added to the extraction schema as a header property described as for reconciliation only. The existing `duplicate_key_fields` and the submittable gate on `target_doctype` stay.

**Removed settings.** `default_company`, `auto_create_missing_parties`, `auto_create_missing_items`, `default_item_group`, `default_uom`, `default_expense_account`, `default_supplier_type`, `default_supplier_group`, `default_customer_group`, `default_territory`, and the Fallback Defaults section are gone. This supersedes the lead overrides that added `default_supplier_type`, `default_supplier_group`, `default_customer_group` and `default_territory`, and the override that kept `default_expense_account`. A rule that needs a company sets it in its `default_values`, for example `{"company": "<name>"}`. Settings keep `enabled`, `llama_cloud_api_key`, `monitored_email_account`, `allowed_extensions`, `min_classification_confidence`, `enable_packet_splitting`, `fuzzy_match_threshold`, `doctype_rules`, and the new `entity_matches`.

**No migration patch.** The app has never been installed on a production site, so no stored data holds the removed fields. The removed fields are dropped by the doctype JSON alone.

**Attachment through cloud_storage.** When `cloud_storage` is installed and `File` has a `file_association` field, `attach_source_file` inserts one File Association child row on the source File. The row carries `parent`, `parenttype` "File", `parentfield` "file_association", `link_doctype`, `link_name`, `user`, `timestamp` and `idx`. It uses `db_insert`. It does not save the File, does not create a File row, and copies nothing. A second call for the same target adds no row. Without cloud_storage, the plain branch calls `create_attachment_copy(ignore_permissions=True)`, as in v2.

Side effects of the association path:
- The source File is shared. Each target holds one association row to the same stored file, and no copy exists.
- The row is written inside the `llama_doc_creation` savepoint. A savepoint rollback removes it, and so does the full rollback on a duplicate, a totals mismatch or a MandatoryError.
- Whether the row is removed when the target is deleted or cancelled depends on cloud_storage's own handling (`File.remove_file_association`). This was not tested here.

**Permission gap on private files.** `attach_source_file` runs as Administrator, so the link is written whatever rights the job's user has. The source File keeps `is_private`. A user who can read the created document may therefore reach the private source file through that document's attachments. This holds only if Frappe's File access check honours the attached document's permissions. That check was not tested. The plain branch has the same gap, through `ignore_permissions`.

**Decision: rule targets keep the submittable gate.** `LlamaDocTypeRule` still refuses a target that is not submittable, as v2 decided. A site without ERPNext therefore needs a custom submittable DocType before any rule can be saved. ToDo and Note cannot be saved as rule targets. The ERPNext-free pipeline test saves its ToDo rule with `settings.flags.ignore_validate`, and the pipeline does not check the gate. The email ingestion fixtures do the same. Open for the maintainer: whether the gate should stay, given that any number of DocTypes must be able to be a target.

**Code and tests.** `tasks.py` passes `reconciliation_field` to the schema build. It passes `duplicate_key_fields`, `default_values` and `reconciliation_field` to the creator, and it no longer passes settings to the creator. `default_values` is parsed before the create lock is taken, so an invalid value fails the log without a lock or a document. The lock, classification, extraction and checkpoint logic is unchanged. Tests: `test_tasks` uses the new call shapes and covers the rule's `default_values` and `reconciliation_field`. `test_pipeline` runs the ToDo end-to-end on any site and skips the Purchase Invoice class without ERPNext. `test_email_ingestion` no longer sets `default_company`. `test_llama_client` uses ToDo fixtures. `test_api` needed no change.
