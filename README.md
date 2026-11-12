# Airwallex Payout Status Migration Lab — FastAPI + Gradio

**Scope:** The PayrollCo interview challenge: legacy `2022-12-13` vs target `2024-04-30` payouts. This is intentionally **not** a full payroll application. It demonstrates two *independent sandbox payouts*, their API response statuses, funding visibility, the `payment.*` webhook event family, and the operational advantage of `SENT` + `PAID` over `DISPATCHED` alone.

The application uses actual Airwallex sandbox endpoints for validation, creation, status retrieval, and sandbox status transition. It **never** calls a production endpoint. It does **not** pretend fixture events are real Airwallex deliveries, and it does **not** silently use fixtures after an API error. No actual Airwallex calls have been made by the project author without your sandbox credentials; you must verify historical version and permissions in your account.

## 1. Run locally

```bash
cd airwallex_payout_flow_lab
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```dotenv
AIRWALLEX_CLIENT_ID=<your_sandbox_client_id>
AIRWALLEX_API_KEY=<your_sandbox_api_key>
# Optional only for enabled Airwallex platform/connected accounts:
AIRWALLEX_CONNECTED_ACCOUNT_ID=
```

Do not paste credentials into chat, Gradio fields, source files, or git. If your existing credentials live in your **previous** lab directory, copy your own `.env` into **this new project** or inject those two environment variables in your terminal. Credentials are not included in this ZIP.

```bash
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Open **http://127.0.0.1:8000/ui/** for Gradio and **http://127.0.0.1:8000/docs** for FastAPI. Read-only diagnostic endpoint: `http://127.0.0.1:8000/api/observations`.

## 2. Prerequisites in your Airwallex sandbox

- Have a sandbox account and **Payouts** permission with a client ID and API key.
- Have at least one **saved sandbox beneficiary**. You can list them in the app, or create one in the sandbox web app. The starter JSON uses its `beneficiary_id`; the beneficiary's payment corridor and funding method dictate valid currencies, method, payer and required fields.
- Use **small test amounts** (the starter limits to 100 units) and synthetic test data only.
- If you operate a platform with connected accounts, add `AIRWALLEX_CONNECTED_ACCOUNT_ID` only if that account is enabled/authorized. Otherwise omit it; this lab can run against one sandbox account.
- If sandbox returns a version-deprecation error for `2022-12-13`, use the plainly labeled local fixture events in Tab 3 and explicitly say that the old API was not callable in your account. Do not attribute local events to the sandbox.

## 3. Perform the real payout flow (both versions)

The lab pins the comparison to two saved US/USD/LOCAL sandbox payees:

| Demo flow | Payee | API version | Successful status path |
| --- | --- | --- | --- |
| `legacy_bofa` | Bank of America | `2022-12-13` | `NEW → READY_FOR_DISPATCH → DISPATCHED` |
| `target_chase` | Chase | `2024-04-30` | `SCHEDULED → PROCESSING → SENT → PAID` |

Both the UI and REST API enforce this mapping. The REST workflow is available under
`/api/demo-flows/{flow}`:

1. `POST /prepare` with `{"amount": 1, "source_currency": "USD"}`.
2. `POST /validate` with the returned payload inside `{"payload": {...}}`.
3. `POST /create` with the exact payload and `"authorized": true`.
4. `GET /payments/{payment_id}` to retrieve the authoritative current status.
5. `POST /payments/{payment_id}/simulate` with `{"next_status": "..."}`.

Use `GET /api/demo-flows` to inspect both mappings and `GET /api/payees` to inspect the
locally stored beneficiary UUIDs. Open `/docs` for the interactive request schemas. The
simulation route can be selected explicitly in the request when testing the documented
historical/current endpoint discrepancy; the server never retries through another route.

On **Tab 1: Prepare and create**:

1. Test authentication and confirm that both named payees are shown as configured.
2. In **Legacy · BofA**, click **Prepare BofA payout JSON**. The beneficiary UUID,
   USD payment currency, `LOCAL` method, and legacy API version are fixed by the server.
   The action creates a unique `request_id` and starter body, e.g.:

```json
{
  "beneficiary_id": "708140b1-acb9-4be7-9314-bf07cfc0ff65",
  "payment_amount": "1.0",
  "payment_currency": "USD",
  "source_currency": "USD",
  "payment_method": "LOCAL",
  "reason": "wages_salary",
  "reference": "PAYROLL-LAB-<unique-suffix>",
  "request_id": "<generated-uuid>"
}
```

3. Set the sandbox amount, source currency, or payer if needed, then **Validate** with
   `POST /api/v1/payments/validate` and header `x-api-version: 2022-12-13`.
4. Check the explicit authorization checkbox. **Create** once with `POST /api/v1/payments/create`. The app records the unique request ID in SQLite first to guard duplicate submissions.
5. Repeat in **Target · Chase**. The server fixes the Chase beneficiary UUID and uses
   `x-api-version: 2024-04-30` with a **new distinct** `request_id`. This is a separate
   synthetic test payout. **Never double-write the same actual salary payment to both versions.**
6. Copy the two generated IDs into Tab 2 (they should also propagate to the monitor fields when created).

If you receive a 400 validation error, inspect the API code and adapt `payer_id`, beneficiary selection, `reason`, payment method/currency, or corridor requirements. Do not guess bank details. If the payment creation times out, the app locks that `request_id` in `UNKNOWN_CREATE_RESULT`; look up the outcome in the Airwallex sandbox dashboard/API before proceeding. It will **not** retry a POST silently.

## 4. Observe the lifecycle in real sandbox

Tab 2 compares two distinct payout IDs, each pinned to its original API version.

**Legacy path** (simplified; approval/review, funding, cancellation and failure branches omitted):

`NEW → READY_FOR_DISPATCH → DISPATCHED`

- Click **Poll real payout status** to observe the current state.
- Select `DISPATCHED` and **Advance REAL sandbox test payout**. The documented legacy simulation can automatically pass through intermediate states for a direct `DISPATCHED` transition. You may use `READY_FOR_DISPATCH` first if it is the valid next state for your payout.
- Poll GET again. Explain that `DISPATCHED` does not mean beneficiary credited; the legacy API lacks a separate bank-partner `PAID` checkpoint.

**Target path** (simplified):

`SCHEDULED → PROCESSING → SENT → PAID`

- Poll current state.
- Try transitions in order: `PROCESSING`, then `SENT`, then `PAID`. Airwallex enforces valid next states; it may not require or permit each explicitly in every configuration. Do not claim a status you did not observe.
- At `SENT`, customer operations know only that Airwallex dispatched to the banking partner.
- At `PAID`, operations gain the distinct banking-partner-processed milestone. A late `FAILED` can still follow `PAID`, so it is **not** absolute final settlement to the employee.
- Observe `funding.status` independently when returned by GET.

**Simulation endpoint discrepancy, documented by Airwallex:**

- Legacy selectable default: `POST /api/v1/simulation/payments/{id}/transition`.
- Target selectable default (newer integration guide): `POST /api/v1/simulation/transfers/{id}/transition`.
- The historical `2024-04-30` simulation API reference also lists `/api/v1/simulation/payments/{id}/transition` and stale legacy enum descriptions. Both endpoints are selectable manually in Tab 2. The program never automatically falls back on failure, because an HTTP error or actual response should remain visible. If `transfers` returns 404 under the historical version, try the `payments` option and record which actual route works, subject to the server's accepted `next_status` values.
- `FAILED` simulation supplies a documented `failure_type`; apply it to **another test payout** if you want to preserve a successful comparison payout.

## 5. Capture signed Airwallex webhooks

FastAPI exposes:

- `POST /webhooks/legacy` configured for API version `2022-12-13`.
- `POST /webhooks/target` configured for API version `2024-04-30`.

Create two separate subscriptions in Airwallex sandbox Developer → Webhooks, selecting the correct API version for each subscription. Select events available under that version, such as:

| Legacy | Target |
| --- | --- |
| `payment.new` | `payment.scheduled` |
| `payment.in_review` | `payment.processing` |
| `payment.ready_for_dispatch` | `payment.sent` |
| `payment.dispatched` | `payment.paid` |
| `payment.failed` | `payment.failed` |
| — | `payment.funding.funded` (as applicable) |

**Event mapping is conceptual, not one-to-one equivalence**; for example `IN_REVIEW` and `PROCESSING` do not represent identical internal states.

Set secrets in `.env`:

```dotenv
AIRWALLEX_WEBHOOK_SECRET_LEGACY=<webhook-secret-for-legacy-url>
AIRWALLEX_WEBHOOK_SECRET_TARGET=<webhook-secret-for-target-url>
```

Webhook messages use headers `x-timestamp` (milliseconds) and `x-signature`. The app checks HMAC-SHA256 of **timestamp + raw request bytes**, enforces a 5-minute replay window, deduplicates event IDs, and stores selected non-sensitive fields. Missing secrets reject delivery; no route silently trusts unsigned traffic.

**Security when exposing a webhook receiver:** The **Gradio dashboard and `/api/observations` are not authenticated** and must remain accessible only on your laptop. If you use ngrok/cloudflared for webhook testing, configure the tunnel/reverse proxy to forward **only `/webhooks/legacy` and `/webhooks/target`** and deny `/ui`, `/docs`, `/api/observations`, and every other path. Do not expose a simple full-app tunnel to the public internet. Alternatively test webhooks locally using Airwallex's webhook test UI plus a correctly locked-down proxy.

The app does not need webhooks to prove real statuses; the GET snapshot buttons work independently. For local presentation resilience, Tab 3 has plainly labeled **LOCAL FIXTURE** events that illustrate historical event names but are **not** claims of sandbox delivery.

## 6. Observe and simulate the funding lifecycle

Tab 4 is independent from Tabs 1–3 and does not write to their payout or webhook ledger.
Its LIVE section accepts a dedicated sandbox payment ID and can:

- Retrieve the real funding object with `GET /api/v1/payments/{payment_id}`.
- Confirm manual funding with `POST /api/v1/payments/confirm_funding/{payment_id}` when
  the real funding state is `REQUIRES_FUNDING_CONFIRMATION`.
- Call a user-selected documented simulation route (the exact-version historical
  `/simulation/payments/...` route is the default), then retrieve the payment again to
  show the real funding state after the transfer-status transition.

Both state-changing calls require explicit authorization. They affect the supplied real
Airwallex sandbox payout, so use a dedicated test payout. Nothing is automatically copied
from the existing BofA/Chase monitor.

The same tab retains an isolated, in-memory fallback for deterministic demonstrations of
funding states that Airwallex does not expose as arbitrary simulation transitions:

- Manual confirmation: `REQUIRES_FUNDING_CONFIRMATION → SCHEDULED → PROCESSING → FUNDED`.
- Wallet: `SCHEDULED → PROCESSING → FUNDED`.
- Linked Account direct debit: `SCHEDULED → PROCESSING → FUNDED → REVERSED`, or
  `PROCESSING → FAILED`.

Every local snapshot and event is labeled `LOCAL_FUNDING_SIMULATION_NOT_AIRWALLEX`.
`FAILED` and `REVERSED` are shown only for direct debit, matching their documented
applicability in this API generation. LIVE responses are separately labeled
`LIVE_AIRWALLEX_SANDBOX`.

## 7. 5-minute interview runbook

| Time | Action | Talk track |
| --- | --- | --- |
| 0:00–0:45 | Tab 4 retrieve LIVE funding, then start a local scenario | “Funding and payout delivery are independent state machines.” |
| 0:45–1:30 | Tab 1 show two separately validated and created test payout IDs | “Both versions use the payment endpoint with independently versioned requests.” |
| 1:30–2:30 | Tab 2 legacy payout GET + `DISPATCHED` | “PayrollCo knows the money left Airwallex but lacks bank-partner completion visibility.” |
| 2:30–3:45 | Tab 2 target `PROCESSING` → `SENT` → `PAID`, show GET | “SENT indicates dispatch. PAID separately marks banking-partner processing, allowing precise customer support.” |
| 3:45–4:30 | Tab 3 signed event log; optionally inject labeled local duplicate/late failure | “Events provide real-time updates, GET provides a current snapshot; deduplication and reconciliation are essential.” |
| 4:30–5:00 | Tab 4 show `FUNDED` separately from payout status | “FUNDED secures the money; it does not mean the beneficiary was paid.” |

**Presentation reliability:** Prepare real test transactions and transitions before the interview, save screenshots of your sandbox GET responses with only allowlisted fields, and avoid any dependency on creating a new payout live during the panel.

## 8. Internal structure

- `service.py`: sandbox client, token handling, version headers, idempotent create, simulations, signed event verification, SQLite persistence and status interpretation.
- `app.py`: FastAPI routes and Gradio UI.
- `funding_simulation.py`: isolated in-memory funding-state fixture; no Airwallex or database dependency.
- `tests/`: unit and HTTP integration tests that mock Airwallex responses. Tests never contact Airwallex.
- `.env.example`: local credentials template; real `.env` is gitignored.

Test with:

```bash
PYTHONPATH=. pytest -q
```

## Official documentation

- [API 2024-04-30 — payouts](https://www.airwallex.com/docs/api/2024-04-30/payouts/transfers)
- [Legacy transfer statuses](https://www.airwallex.com/docs/payouts/older-api-versions/transfers/transfer-statuses)
- [Sandbox transition simulation guide](https://www.airwallex.com/docs/payouts/transfers/create-a-transfer/simulate-transfer-status-transition)
- [Historical 2024-04-30 simulation endpoint](https://www.airwallex.com/docs/api/2024-04-30/simulation/payouts)
- [Webhook signing and version selection](https://www.airwallex.com/docs/developer-tools/webhooks/listen-for-webhook-events)
