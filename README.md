# InvoiceMatch

InvoiceMatch is an accounts-payable review app for text-based INR invoices. Enter a purchase order or run a bundled demo, inspect line-level findings with source excerpts, and save a decision with an audit history.

**Status** — The polished reviewer runs locally. The Phase 2 benchmark results are available. Public deployment follows local acceptance.

The benchmark will measure whether model-assisted extraction catches more discrepancies than rules-only extraction and how that affects review volume, latency, and cost. Both extractors will use the same invoice evidence, return the same typed record, and run through the same deterministic reconciliation rules.

## Run the local reviewer

```shell
docker compose up --build
```

Open [the local reviewer](http://127.0.0.1:8000/). Under **Check my invoice**, upload a text-based INR PDF and choose a saved order or select **New purchase order**. Enter the order number, supplier, item codes, quantities, and unit prices. Select **Compare invoice**, then click a finding to see its invoice evidence. If supplier or item names differ, confirm the names below the comparison and compare again. The original extraction stays in the audit history. Editing an order creates a new version and existing reviews retain the version they compared.

Select **Try a sample** to compare a bundled invoice with its included order. The **Duplicate upload** sample compares the same PDF twice and shows the existing review. Open **Sample options** to choose **Live AI** when Groq is configured, or use **Sample replay** to see recorded extraction without credentials. After saving a decision, reload the review page to see the history.

Compose binds to loopback and stores reviews in a named volume. Use `INVOICEMATCH_PORT` to select a different host port. Compose forwards `GROQ_API_KEY` from the shell or the ignored local `.env`. With a native install, launch `python -m uvicorn invoicematch.app:create_app --factory` and put the key in the server environment to enable live mode.

## Deploy with Vercel and Neon

The Vercel entry point is `asgi.py`. It serves the browser UI and API together, requires PostgreSQL, and sends session cookies over HTTPS. Vercel runs Python 3.12 in Singapore and schedules expired-record cleanup once a day. Access expires at 24 hours. **Clear session data** deletes active session data immediately.

1. Connect the repository to a Vercel project and add a Neon database in the same region. Use separate Neon branches for preview and production.
2. Install the package with `python -m pip install -e .`. Set `INVOICEMATCH_MIGRATION_URL` in your local shell to the target branch's direct Neon connection URL, then run `python -m invoicematch.migrate` once per branch. The command can be retried. New hosted sessions start in PostgreSQL. Existing local SQLite sessions remain on the local installation.
3. Set the pooled Neon connection URL as the server-only `DATABASE_URL` in each Vercel environment. Keep Neon’s TLS and channel-binding parameters in both URLs. Set `GROQ_API_KEY` for sample-only Live AI and `CRON_SECRET` to a random secret for the daily purge. The Neon integration can supply `DATABASE_URL` automatically.
4. Deploy a preview and check `/health`. From a fresh browser, run the samples, upload a custom PDF with a saved PO, and save a decision. Confirm the review survives reload and redeploy, a second browser cannot access it, and Live AI works on a bundled sample. Verify the production deployment and tag the tested Git revision.

Use a prior verified deployment in Vercel for a code rollback. The Neon data stays on the target branch during a rollback. Check the health route and the review workflow after rolling back.

### Supported invoices and limits

- Custom reviews support one full, digitally generated INR invoice against one PO, with positive whole-number quantities. PDFs can be up to 2 MiB and eight pages. The parser recognizes labels such as **SKU**, **Qty**, and **Unit rate** in a positioned table. It expects a standalone, left-aligned **Invoice total** or **Grand total**. Image-only scans and layouts outside the supported grammar surface as review cases.
- The rules compare corresponding line items at exact decimal values. They check the printed subtotal when present and validate the grand total with separately stated tax, freight, and discount amounts. Tax can use one aggregate amount, a CGST/SGST pair, or IGST. Conflicting breakdowns require review. Tax amounts come from the invoice.
- Custom PDFs run in a separate parser process with a six-second deadline. On Linux, the parser also caps CPU time and virtual memory. The reviewer processes custom invoices locally to the backend and saves extracted fields, evidence excerpts, the PO snapshot, and decisions in its database. Custom invoice text is never sent to Groq. Sessions expire after 24 hours. Use **Clear session data** to delete a session early.
- Live AI accepts only the bundled samples. The server enforces a provider deadline, suppresses concurrent repeat calls for the same review, and caps live attempts per session, per minute, and per day. Provider failure is visible in the review. Replay uses the stored sample extraction and runs without a key.

```mermaid
flowchart LR
    Browser --> API[FastAPI reviewer]
    API --> PDF[PDF evidence and saved PO version]
    PDF --> Rules[Typed extraction and exact-money checks]
    API --> Groq[Groq for bundled samples]
    Groq --> Rules
    Rules --> Store[SQLite review and decision history]
    Store --> API
```

## Verify the local workflow

In an activated Python 3.11+ virtual environment, run:

```shell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

Use `docker compose build` to verify the image. The test suite checks the matching policy, source row evidence, API boundaries, session isolation, and concurrent retries through the upload endpoint.

The sample generator runs with `python scripts/generate_demo_fixtures.py`. It produces the clean-match, price-mismatch, and ambiguous-extraction PDFs. The duplicate scenario reuses the clean-match bytes.

## Paired extraction benchmark

The held-out set contains 12 synthetic PDFs across two layouts, with six valid invoices and six discrepancies. [Frozen labels and PDF hashes](tests/fixtures/phase2/labels.json) tie the expected decisions and fields to each input. Groq receives positioned invoice text. Both arms use the same source verifier, typed extraction contract, and reconciliation policy.

```shell
python -m invoicematch.benchmark --mode rules
python -m invoicematch.benchmark --mode replay --capture benchmarks/phase2-groq.json
python -m invoicematch.benchmark --mode groq --output instance/phase2-groq-live.json
```

Rules and replay run offline. Replay checks that the recorded claims reproduce the same decisions and field scores. The live command uses `GROQ_API_KEY` from the environment or the ignored local `.env`. It sends one request per case with ten seconds between calls by default. Reported latency starts after shared PDF parsing and evidence preparation, and excludes this pacing delay. It includes extraction and reconciliation, plus the provider round trip for the model arm.

### Observed results

| Metric | Rules | Groq GPT-OSS 20B |
| --- | ---: | ---: |
| Discrepancies marked as matches | 0/6 | 0/6 |
| Valid invoices that failed to match | 0/6 | 0/6 |
| Invoices routed to review | 0/12 | 0/12 |
| Exact labeled fields | 140/140 | 140/140 |
| Technical failures | 0/12 | 0/12 |
| Median latency | 0.16 ms | 0.79 s |

Groq's nearest-rank p95 was **1.26 s**. All 12 calls reported usage, totaling 8,719 input and 2,949 output tokens. Estimated list-price cost was **$0.00144** for the run, or **$0.00012 per invoice**. The capture records the measured source commit `cd04162`, prompt version, request parameters, protocol hash, backend fingerprints, token usage, and individual decisions. [Live capture](benchmarks/phase2-groq.json) · [Groq model pricing](https://console.groq.com/docs/model/openai/gpt-oss-20b)

The two arms agreed on all 12 cases, so rules remain the default. The corpus is small and synthetic. Both arms rely on the same deterministic source verifier, which already identifies and checks the values for these layouts. These timings describe the benchmark machine and network path. Hosted UI request times require a separate measurement.

The initial prompt produced a Groq schema rejection when it returned a row object in place of the required array. The revised prompt clarifies the shape, and the benchmark counts a provider schema rejection as a technical failure. A separate regression verifies that a plausible price taken from a neighboring row on H03 routes to review.

## First release

The reviewer compares one digitally generated INR invoice against a seeded or user-entered purchase order. It checks supplier identity, line IDs, quantities, unit prices, line extensions, invoice total, and currency. Values use exact decimal arithmetic. A reviewer sees expected and observed values, source excerpts, and any evidence the system could not establish.

The tagged public release must provide an HTTPS UI that works in a fresh browser without credentials. Visitors can start with samples or save their own PO and reconcile an unseen, supported PDF. The UI exposes four fixture-replay scenarios and live model extraction on at least one approved sample. Decisions survive reload, sessions are isolated, and live mode has a server-side request budget and explicit failure feedback.

The release scope is full-order reconciliation with digitally generated INR invoices. The workflow ends at a saved review decision.

## Phases

| Phase | Exit evidence |
| --- | --- |
| 0. GitHub and design | Verify the dedicated public repository and committed docs. Freeze the decisions, fixtures, comparison criteria, proposed data model, and verification commands below. Obtain approval for the first implementation plan. |
| 1. Deterministic workflow | Run all four seeded cases through the local reviewer UI. Pass exact-money, evidence, persistence, isolation, and concurrent retry checks. Verify the one-command local run. |
| 2. Model comparison | Run a paired benchmark on held-out layouts, publish measured outcomes, and verify provider failures. Run replay tests in CI without provider credentials. |
| 3. Public release | Tag and deploy the tested revision. Verify an unseen custom PDF and saved PO, all four replay cases, a real provider-backed review, persisted decisions, session isolation, and live limits from the hosted app. Verify clean-clone instructions, CI, and deployment and rollback documentation. |
| 4. Maintenance release | Reproduce a defect, add a regression test, deploy a tagged fix, and verify the hosted workflow and recovery path. |

The independent Scrutiny agent reviews the exact candidate revision at each phase gate. A phase advances after required checks pass, material findings are resolved, and the verdict is recorded with the revision and evidence.

## Phase 0 design

### Matching policy and fixture outcomes

The reference order is `PO-001` from supplier `SUP-001`, in INR. It has item `A-10` (10 × ₹100.00) and item `B-20` (2 × ₹50.00), for a total of ₹1,100.00. Order lines have unique IDs. In-scope invoice quantities are positive integers and monetary fields are nonnegative amounts with at most two decimal places. A match requires a grounded invoice number and supplier ID, and one instance of every order line with matching quantity, price, and currency. Check each displayed line total against its own quantity × unit price and the displayed invoice total against the sum of the computed line totals.

| Case | Labeled input | Expected behavior |
| --- | --- | --- |
| Clean match | Invoice `INV-DEMO-01` identifies `SUP-001`, both reference lines, and the ₹1,100.00 total. | Match with cited source evidence. |
| Unit price mismatch | `A-10` is ₹120.00 per unit, with consistent line and invoice totals of ₹1,200.00 and ₹1,300.00 respectively. | Mismatch showing ₹100.00 expected and ₹120.00 observed for the unit price. |
| Quantity or line-set mismatch | A complete, readable table establishes a different quantity, an extra line, or an omitted order line. | Mismatch showing the affected line ID and expected and observed values. |
| Other grounded mismatch | The source proves a different supplier or a displayed total that disagrees with the available line arithmetic. | Mismatch with the relevant supplier or arithmetic finding. |
| Ambiguous or malformed extraction | A required field is missing, two values conflict, a line ID repeats, the extracted price came from another row, or the extraction response is malformed. | Needs review. Cite what can be verified and identify the missing or invalid evidence. |
| Identical retry | The same PDF bytes, order, session, and mode are submitted twice, including simultaneously. | Return the same review ID. Persist one review and preserve its saved decision. |
| Possible duplicate | A second PDF has different bytes but the same verified supplier ID and invoice number in the same session. | Needs review with a possible duplicate finding. Preserve both submissions. |
| Conflicting order selection | The same PDF is paired with a different known order in the same session. | Needs review on the new submission. Preserve the original decision and show both order IDs. |
| Input boundaries | Unsupported currency, a valid PDF with unreadable text, malformed PDF bytes, an oversized upload, or an unknown order. | Route unsupported currency and unreadable text to review. Reject malformed PDFs, oversized uploads, and unknown orders before reconciliation. |
| Provider failure | A live extraction call times out or returns an error. | Needs review with a provider failure reason. An exhausted budget prevents the call and shows live mode as unavailable. |

Validate input boundaries before creating review records for the submission. Reuse an existing exact retry before classifying a new submission. For new items, uncertain evidence or a possible duplicate or order conflict takes precedence over a match or mismatch. A source pass identifies the table boundaries and row spans from PDF text positions independently of either extractor. Verify coverage of every source row and ground material values in their row or header. A missing field in incomplete extraction is uncertainty. A line absent from a verified complete table is a discrepancy. Treat unprovable table completeness as uncertainty.

### Proposed data model and write behavior

| Record | Proposed fields and constraints |
| --- | --- |
| Purchase order and lines | Order ID, supplier ID, currency, exact monetary values, and line IDs unique within the order. |
| Extraction | Invoice number, supplier ID, currency, totals, and typed lines. Material fields carry a page reference and row or header evidence. |
| Review | Session, PDF SHA-256, order ID, mode, extraction result, verdict, and findings. The session, hash, order, and mode form a unique key. Same-document order conflicts and grounded invoice identity collisions are looked up within the session. |
| Decision event | Review ID, decision, optional note, and timestamp. Append the event and any reviewer state change in the same transaction. |

Use SQLite with a database uniqueness constraint to make retries safe under concurrency. Run extraction outside the write transaction. Use a bounded wait to acquire the write lock before rereading the retry key, checking for other conflicts, and inserting the review. Resolve a uniqueness conflict by loading the winning review. Store monetary values without binary floating-point conversion. Partition public reads and writes by the current server-validated session.

### Evaluation criteria

Use two development layouts for the reference and robustness cases above. Reserve two different layout families for the paired benchmark. `H01`–`H06` use a compact table. `H07`–`H12` use wrapped descriptions and a different column order. Default supplier, currency, quantities, and prices come from `PO-001`. Each invoice number is its case ID. The printed line and invoice totals follow the arithmetic unless the case specifies a different printed amount. Commit the generated PDFs and their field labels before tuning either candidate.

| ID | Variation | Expected decision |
| --- | --- | --- |
| H01 | Reference values. | Match |
| H02 | Reference values with the two rows reversed. | Match |
| H03 | Reference values with the unit price from the neighboring row positioned close to the other line's description. | Match |
| H04 | `A-10` unit price ₹120.00. | Mismatch |
| H05 | `A-10` quantity 9. | Mismatch |
| H06 | `B-20` omitted from a complete table. | Mismatch |
| H07 | Reference values. | Match |
| H08 | Reference values with a non-line-item ₹125.00 in a note. | Match |
| H09 | Reference values with repeated table headers across two pages and one line per page. | Match |
| H10 | `A-10` unit price ₹105.00 and `B-20` unit price ₹25.00, preserving the grand total. | Mismatch |
| H11 | Supplier `SUP-002`. | Mismatch |
| H12 | Printed grand total ₹1,150.00 with reference line values. | Mismatch |

Run both candidates on the same 12 PDFs with the same normalizer and policy. Report discrepancies marked as matches out of six discrepant invoices. Report valid invoices that failed to match out of six valid invoices, broken down by mismatch, needs review, and unavailable verdict. Count all technical failures, including failures routed to review. Also report total reviews, exact matches out of labeled fields, a case-level disagreement, versions, latency, and model cost. Prefer the model as the default only when both runs complete all 12 cases without technical failures and the model passes fewer discrepant invoices with no increase in valid non-matches. Publish results either way and disclose the small synthetic sample. Use separate robustness cases for ambiguous extraction, retries, and failure behavior. Report replay outcomes separately from benchmark results.

### First implementation plan

1. Add the Python package, dependency manifest, and test harness. Implement typed records, the reference fixtures, exact-money reconciliation, and evidence-driven decisions. Verify the decision table through domain tests.
2. Add PDF input validation and evidence extraction, then implement SQLite review persistence, session isolation, and transactional retries. Verify malformed inputs and concurrent submission against persisted state.
3. Wire the local FastAPI reviewer workflow and a minimal functional UI. Run the four seeded scenarios through the normal entry point and verify saved decisions after reload. Final visual and interaction design follows later user direction.
4. Add Docker and CI verification. Run a clean one-command start, tests, lint, formatting, type checking, and the build. Record the commands and request Phase 1 scrutiny.

The proposed layout uses `src/invoicematch/` for the app, `tests/` for checks, and `tests/fixtures/` for synthetic labeled inputs. Proposed verification commands are `python -m pytest`, `python -m ruff check .`, `python -m ruff format --check .`, `python -m mypy src`, `docker compose build`, and `docker compose up --build`. These become runnable as the corresponding files and dependencies land.

## Release deliverables

- Public reviewer UI linked from this README, with release tags and browser verification evidence.
- Tested source, synthetic fixtures, CI, and a reproducible one-command local run.
- Architecture and limitations, a reproducible benchmark with results, and a deployment and rollback runbook.
- Two or three portfolio bullets backed by a measured result or verified release.
