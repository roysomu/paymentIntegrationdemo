"""FastAPI + Gradio Airwallex payout status migration demonstration.

Only Airwallex sandbox host is used. Credentials always remain server-side.
"""
from __future__ import annotations

import json
import os
from typing import Any

import gradio as gr
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

import funding_simulation as funding_sim
import service as svc

api = FastAPI(title='Airwallex Payout Version Migration Lab', version='1.0')


class PrepareDemoRequest(BaseModel):
    amount: float = Field(default=1.0, gt=0, le=100)
    source_currency: str = 'USD'
    payer_id: str = ''


class DemoPayloadRequest(BaseModel):
    payload: dict[str, Any]


class CreateDemoRequest(DemoPayloadRequest):
    authorized: bool = False


class SimulateDemoRequest(BaseModel):
    next_status: str
    failure_type: str = 'BENEFICIARY_BANK_RETURNED'
    route: str | None = None


def api_result(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=svc.safe_error(exc)) from exc


@api.get('/api/health')
def health():
    return {'ok': True, 'airwallex_host': svc.BASE, 'live_api_calls_available': bool(os.getenv('AIRWALLEX_CLIENT_ID') and os.getenv('AIRWALLEX_API_KEY')),
            'sandbox_only': True}


@api.get('/api/observations')
def observations():
    payouts, events = svc.list_records()
    return {'payouts': payouts, 'observations': events}


@api.get('/api/payees')
def saved_payees():
    return {'payees': svc.list_saved_payees()}


@api.get('/api/demo-flows')
def demo_flows():
    return {'flows': svc.list_demo_flows()}


@api.post('/api/demo-flows/{flow}/prepare')
def prepare_demo(flow: str, body: PrepareDemoRequest):
    return {'flow': flow, 'payload': api_result(
        svc.prepare_demo_payload, flow, body.amount, body.source_currency, body.payer_id
    )}


@api.post('/api/demo-flows/{flow}/validate')
def validate_demo(flow: str, body: DemoPayloadRequest):
    return {'flow': flow, 'result': api_result(svc.validate_demo, flow, body.payload)}


@api.post('/api/demo-flows/{flow}/create')
def create_demo(flow: str, body: CreateDemoRequest):
    return api_result(svc.create_demo, flow, body.payload, body.authorized)


@api.get('/api/demo-flows/{flow}/payments/{payment_id}')
def get_demo_payment(flow: str, payment_id: str):
    return api_result(svc.get_demo_payment, flow, payment_id)


@api.post('/api/demo-flows/{flow}/payments/{payment_id}/simulate')
def simulate_demo(flow: str, payment_id: str, body: SimulateDemoRequest):
    return api_result(
        svc.simulate_demo, flow, payment_id, body.next_status, body.failure_type, body.route
    )


@api.post('/webhooks/{subscription}')
async def webhook(subscription: str, request: Request, x_timestamp: str | None = Header(default=None),
                  x_signature: str | None = Header(default=None)):
    if subscription not in ('legacy', 'target'):
        raise HTTPException(status_code=404, detail='Unknown webhook subscription')
    version = svc.LEGACY if subscription == 'legacy' else svc.TARGET
    secret_name = 'AIRWALLEX_WEBHOOK_SECRET_LEGACY' if subscription == 'legacy' else 'AIRWALLEX_WEBHOOK_SECRET_TARGET'
    secret = os.getenv(secret_name, '')
    if not secret:
        raise HTTPException(status_code=503, detail='Configure this webhook subscription secret in .env')
    raw = await request.body()
    if not svc.verify_signature(secret, x_timestamp or '', x_signature or '', raw):
        raise HTTPException(status_code=401, detail='Invalid webhook signature or timestamp')
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Expected JSON object')
        accepted = svc.record_webhook(version, data)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=svc.safe_error(exc)) from exc
    return {'ok': True, 'accepted': accepted, 'duplicate': not accepted}


def ui_call(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        return '⚠️ ' + svc.safe_error(exc)


def ui_demo_payee(flow: str):
    try:
        config = svc.demo_flow(flow)
        payee = config['payee']
        return payee, gr.update(value=payee['beneficiary_id']), (
            f'**Assigned flow:** `{flow}` · `{config["version"]}` · '
            f'`{payee["bank_country"]}/{payee["account_currency"]}/{payee["payment_method"]}`'
        )
    except Exception as exc:
        return {}, gr.update(), '⚠️ ' + svc.safe_error(exc)


def build_demo_json(flow: str, amount: float, source: str, payer: str):
    try:
        config = svc.demo_flow(flow)
        payload = svc.prepare_demo_payload(flow, amount, source, payer)
        return json.dumps(payload, indent=2), (
            f'**Fresh request prepared for {config["title"]}.** '
            f'Path: `{config["successful_path"]}`. Validate before creating.'
        )
    except Exception as exc:
        return '', '⚠️ ' + svc.safe_error(exc)


def ui_validate_demo(flow: str, text: str):
    try:
        return svc.validate_demo(flow, svc.parse_payload(text))
    except Exception as exc:
        return '⚠️ ' + svc.safe_error(exc)


def ui_create_demo(flow: str, text: str, check: bool):
    try:
        result = svc.create_demo(flow, svc.parse_payload(text), check)
        payment_id = result.get('payment_id') or result.get('id') or ''
        return result, gr.update(value=payment_id), (
            f'**LIVE SANDBOX:** `{flow}` payout created. Use its lifecycle controls in Tab 2.'
        )
    except Exception as exc:
        return {}, gr.update(), '⚠️ ' + svc.safe_error(exc)


def build_json(version: str, beneficiary: str, amount: float, currency: str, source: str, payer: str, method: str):
    try:
        corridor = svc.beneficiary_corridor(version, beneficiary)
        account_currency = str(corridor.get('account_currency') or '')
        methods = corridor.get('payment_methods') or []
        if account_currency and currency.upper().strip() != account_currency:
            raise ValueError(f'This beneficiary requires payment currency {account_currency}; the form currently uses {currency.upper().strip()}')
        if methods and method not in methods:
            raise ValueError(f'This beneficiary supports payment method(s) {", ".join(methods)}; the form currently uses {method}')
        return json.dumps(svc.new_payload(beneficiary, amount, currency, source, payer, method), indent=2), (
            f'**New `request_id` generated.** Corridor checked: '
            f'`{corridor.get("bank_country")}/{account_currency}/{method}`. Validate before creating.'
        )
    except Exception as exc:
        return '', '⚠️ ' + svc.safe_error(exc)


def ui_beneficiaries(version: str):
    try:
        rows = svc.list_beneficiaries(version)
        first_id = rows[0]['id'] if rows else ''
        return rows, gr.update(value=first_id), (f'**LIVE SANDBOX:** {len(rows)} saved beneficiaries found.' if rows else
                '**LIVE SANDBOX:** No saved beneficiaries. Create one in Airwallex sandbox first, then refresh.')
    except Exception as exc:
        return [], gr.update(), '⚠️ ' + svc.safe_error(exc)


def ui_create(version: str, text: str, check: bool):
    try:
        result = svc.create(version, text, check)
        payment_id = result.get('payment_id') or result.get('id') or ''
        return result, gr.update(value=payment_id), '**LIVE SANDBOX:** Payout created. Next use the lifecycle monitor.'
    except Exception as exc:
        return {}, gr.update(), '⚠️ ' + svc.safe_error(exc)


def ui_get(version: str, payment_id: str):
    try:
        obj = svc.get_payment(version, payment_id)
        return obj, svc.business_explanation(version, str(obj.get('status', 'UNKNOWN')),
                                             str((obj.get('funding') or {}).get('status', ''))),
    except Exception as exc:
        return {'error': svc.safe_error(exc)}, '⚠️ Could not retrieve payout'


def ui_get_demo(flow: str, payment_id: str):
    try:
        obj = svc.get_demo_payment(flow, payment_id)
        version = str(svc.demo_flow(flow)['version'])
        return obj, svc.business_explanation(
            version, str(obj.get('status', 'UNKNOWN')),
            str((obj.get('funding') or {}).get('status', '')),
        )
    except Exception as exc:
        return {'error': svc.safe_error(exc)}, '⚠️ Could not retrieve the payout assigned to this flow'


def ui_transition(version: str, payment_id: str, status: str, route: str, failure: str):
    try:
        return svc.simulate(version, payment_id, status, route, failure), '**LIVE SANDBOX:** Transition response shown. Poll to verify resulting state. Invalid transition responses are not masked.'
    except Exception as exc:
        return {'error': svc.safe_error(exc)}, '⚠️ Transition failed; inspect the error and try only a valid next status.'


def ui_transition_demo(flow: str, payment_id: str, status: str, route: str, failure: str):
    try:
        result = svc.simulate_demo(flow, payment_id, status, failure, route)
        return result, (
            f'**LIVE SANDBOX:** `{flow}` transitioned toward `{status}`. '
            'The authoritative GET snapshot is included below.'
        )
    except Exception as exc:
        return {'error': svc.safe_error(exc)}, '⚠️ Transition failed; use only a valid next state for this payout.'


def ui_attach(version: str, pid: str):
    try:
        result = svc.attach_existing(version, pid)
        return result, '**LIVE SANDBOX:** Existing payout attached for read-only comparison. Simulation disabled for safety.'
    except Exception as exc:
        return {'error': svc.safe_error(exc)}, '⚠️ ' + svc.safe_error(exc)


def ui_records():
    payouts, events = svc.list_records()
    return payouts, events


def ui_fixture(version: str, payment_id: str, name: str):
    try:
        if not payment_id:
            raise ValueError('Provide a payout ID for the local event illustration')
        event = svc.fixture_event(version, payment_id, name)
        accepted = svc.record_webhook(version, event, source='LOCAL_FIXTURE_NOT_AIRWALLEX')
        return event, f'**LOCAL FIXTURE, not Airwallex webhook.** Event accepted: `{accepted}`'
    except Exception as exc:
        return {}, '⚠️ ' + svc.safe_error(exc)


def funding_ui_result(state: dict[str, Any]):
    choices = funding_sim.allowed_next(state)
    return (
        state,
        funding_sim.snapshot(state),
        state['history'],
        funding_sim.explanation(state),
        gr.update(
            choices=choices,
            value=choices[0] if choices else None,
            interactive=bool(choices),
        ),
    )


def ui_start_funding_simulation(scenario: str):
    try:
        return funding_ui_result(funding_sim.start(scenario))
    except Exception as exc:
        return {}, {}, [], '⚠️ ' + svc.safe_error(exc), gr.update(choices=[], value=None, interactive=False)


def ui_advance_funding_simulation(state: dict[str, Any], next_status: str):
    try:
        return funding_ui_result(funding_sim.advance(state, next_status))
    except Exception as exc:
        return state, {}, state.get('history', []) if isinstance(state, dict) else [], (
            '⚠️ ' + svc.safe_error(exc)
        ), gr.update()


def ui_get_live_funding(payment_id: str):
    try:
        result = svc.get_live_funding(payment_id)
        funding = result['payment'].get('funding') or {}
        status = funding.get('status') or 'NOT_REPORTED'
        return result, f'**LIVE Airwallex sandbox:** funding status is `{status}`.'
    except Exception as exc:
        return {'error': svc.safe_error(exc)}, '⚠️ Could not retrieve LIVE sandbox funding state.'


def ui_confirm_live_funding(payment_id: str, authorized: bool):
    try:
        result = svc.confirm_live_funding(payment_id, authorized)
        return result, (
            '**LIVE Airwallex sandbox:** funding confirmation was submitted. '
            'Review `retrieved_after`, then refresh if the update is asynchronous.'
        )
    except Exception as exc:
        return {'error': svc.safe_error(exc)}, '⚠️ LIVE funding confirmation was not submitted.'


def ui_simulate_live_funding_observation(payment_id: str, status: str, route: str,
                                         failure: str, authorized: bool):
    try:
        result = svc.simulate_live_transfer_for_funding(
            payment_id, status, failure, authorized, route
        )
        return result, (
            f'**LIVE Airwallex sandbox:** transfer transition toward `{status}` was submitted. '
            'The real funding snapshot retrieved afterward is shown in the response.'
        )
    except Exception as exc:
        return {'error': svc.safe_error(exc)}, '⚠️ LIVE transfer simulation was not submitted.'


with gr.Blocks(title='Airwallex Payout Migration — live sandbox') as ui:
    gr.Markdown('''# Airwallex · PayrollCo Payout Status Migration Lab
**Comparing API `2022-12-13` vs `2024-04-30`** · Real Airwallex **sandbox-only** create/validate/status GET · Version-specific event capture.''')
    with gr.Tab('1 · Prepare and create REAL sandbox payouts'):
        check = gr.Button('Test sandbox authentication & payout listing')
        preflight = gr.Markdown()
        check.click(lambda: ui_call(svc.check_connection), outputs=preflight)
        gr.Markdown('''The comparison is pinned to two independent sandbox payees:

- **Legacy:** Bank of America using API `2022-12-13`.
- **Target:** Chase using API `2024-04-30`.

Both are US/USD/LOCAL ACH beneficiaries. The request JSON is visible, but each flow rejects edits that change its assigned beneficiary, payment currency, or payment method.''')
        with gr.Row():
            columns = []
            for flow in svc.DEMO_FLOWS:
                config = svc.demo_flow(flow, require_payee=False)
                ver = str(config['version'])
                payee = config.get('payee') or {}
                with gr.Column():
                    gr.Markdown(f'### {config["title"]} · `{ver}`')
                    gr.Markdown(f'Expected lifecycle: `{config["successful_path"]}`')
                    ben_btn = gr.Button('Refresh assigned payee')
                    ben_results = gr.JSON(value=payee, label='Assigned payee (no account number stored)')
                    ben = gr.Textbox(value=str(payee.get('beneficiary_id') or ''),
                                     label='Locked beneficiary_id', interactive=False)
                    amount = gr.Number(value=1.00, minimum=0.01, maximum=100, label='Sandbox payout amount (max 100)')
                    src = gr.Textbox(value='USD', label='Funding currency')
                    payer = gr.Textbox(label='Optional saved payer_id', placeholder='Not required for these test beneficiaries')
                    payee_name = str(config['payee_label']).removesuffix(' US Local')
                    prep_btn = gr.Button(f'1. Prepare {payee_name} payout JSON')
                    payload = gr.Code(label='Editable request JSON (assigned payee/corridor enforced)', language='json', value='{}', lines=13)
                    prepare_result = gr.Markdown()
                    validate_btn = gr.Button('2. Validate via LIVE sandbox')
                    validation = gr.Markdown()
                    authorize = gr.Checkbox(label='I authorize ONE test payout in Airwallex SANDBOX (not production)', value=False)
                    create_btn = gr.Button('3. Create payout in LIVE sandbox', variant='primary')
                    creation = gr.JSON(label='Actual sandbox create response (redacted)')
                    creation_msg = gr.Markdown()
                    payment_id = gr.Textbox(label='Created payment_id (auto-populated; copy to Monitor)')
                    ben_btn.click(lambda f=flow: ui_demo_payee(f), outputs=[ben_results, ben, prepare_result])
                    prep_btn.click(lambda a, s, p, f=flow: build_demo_json(f, a, s, p),
                                   [amount, src, payer], [payload, prepare_result])
                    validate_btn.click(lambda p, f=flow: ui_validate_demo(f, p), [payload], [validation])
                    create_btn.click(lambda p, a, f=flow: ui_create_demo(f, p, a),
                                     [payload, authorize], [creation, payment_id, creation_msg])
                    columns.append((flow, ver, payment_id))
    with gr.Tab('2 · REAL lifecycle + banking outcomes'):
        gr.Markdown('''Each column monitors its assigned payee and API version. `GET` supplies the authoritative current snapshot; the transition control calls Airwallex's sandbox-only simulation API. A flow cannot transition a payout created for the other payee or version.''')
        with gr.Row():
            monitor_outputs = []
            for flow, ver, source_id in columns:
                config = svc.demo_flow(flow, require_payee=False)
                with gr.Column():
                    gr.Markdown(f'### {config["title"]} · `{ver}`')
                    gr.Markdown(f'`{config["successful_path"]}`')
                    pid = gr.Textbox(label='Sandbox payment_id')
                    source_id.change(lambda x: x, source_id, pid)
                    attach = gr.Button('Attach existing sandbox payout (read only)')
                    attach_info = gr.Markdown()
                    fresh_get = gr.Button('Poll real payout status', variant='primary')
                    snapshot = gr.JSON(label='LIVE Airwallex GET snapshot')
                    expl = gr.Markdown()
                    attach.click(lambda p, v=ver: ui_attach(v, p), pid, [snapshot, attach_info])
                    fresh_get.click(lambda p, f=flow: ui_get_demo(f, p), pid, [snapshot, expl])
                    step = gr.Dropdown(svc.STEPS[ver], value=('DISPATCHED' if ver == svc.LEGACY else 'PROCESSING'), label='Next simulated status')
                    route = gr.Dropdown(list(svc.SIMULATION_ROUTES),
                                        value=config['default_simulation_route'],
                                        label='Simulation endpoint family')
                    failure = gr.Dropdown(['BENEFICIARY_BANK_RETURNED', 'INVALID_ACCOUNT_NAME_OR_NUMBER', 'ACCOUNT_CLOSED', 'OTHER'],
                                          value='BENEFICIARY_BANK_RETURNED', label='Failure type (used only when FAILED)')
                    move = gr.Button('Advance REAL sandbox test payout')
                    transition_response = gr.JSON(label='Simulation response + GET verification')
                    transition_note = gr.Markdown()
                    move.click(lambda p, s, r, fail, demo=flow: ui_transition_demo(demo, p, s, r, fail),
                               [pid, step, route, failure], [transition_response, transition_note])
                    monitor_outputs.append(pid)
        gr.Markdown('''**Demo narrative:** BofA's legacy `DISPATCHED` state only proves Airwallex sent the payout. Chase's target flow separates `PROCESSING`, `SENT`, and `PAID`, showing whether processing is internal, the payout has reached the banking partner, or that partner has processed it. **PAID is stronger operational evidence, but a later failure can still occur.**''')
    with gr.Tab('3 · Webhook delivery and status history'):
        gr.Markdown('''For real webhook delivery, configure two Airwallex subscriptions with version-specific secrets:
- `POST https://YOUR-HTTPS-URL/webhooks/legacy` (subscription version `2022-12-13`)
- `POST https://YOUR-HTTPS-URL/webhooks/target` (subscription version `2024-04-30`)

Subscribe to the applicable `payment.*` events. The endpoint verifies `x-signature` + `x-timestamp` against raw JSON, deduplicates by event ID, and writes durable observations. **Use a tunnel exposing only `/webhooks/*`; keep the Gradio console private.**''')
        with gr.Row():
            fixture_ver = gr.Dropdown(list(svc.VERSIONS), value=svc.TARGET, label='Fixture version')
            fixture_id = gr.Textbox(label='Payout ID for illustrative event', value='pmt_LOCAL_DEMO')
            fixture_name = gr.Dropdown(['payment.new', 'payment.dispatched', 'payment.scheduled', 'payment.processing', 'payment.sent', 'payment.paid', 'payment.failed', 'payment.funding.funded'], value='payment.paid', label='Illustrative event name')
        fixture_btn = gr.Button('Inject clearly labeled LOCAL fixture (optional fallback)')
        fixture_obj = gr.JSON(label='Fixture payload (NOT from Airwallex)')
        fixture_note = gr.Markdown()
        fixture_btn.click(ui_fixture, [fixture_ver, fixture_id, fixture_name], [fixture_obj, fixture_note])
        refresh = gr.Button('Refresh payout records and signed webhook event history', variant='primary')
        with gr.Row():
            payouts_view = gr.JSON(label='Versioned payout ledger')
            observations_view = gr.JSON(label='Chronological observations: LIVE GET / LIVE WEBHOOK / FIXTURE')
        refresh.click(ui_records, outputs=[payouts_view, observations_view])
        ui.load(ui_records, outputs=[payouts_view, observations_view])
        gr.Markdown('''**Interpretation:** A webhook reports a transition, whereas `GET` shows current state. They are deliberately stored independently because webhooks can be duplicated or arrive out of order. Real production processing should reconcile before committing irreversible business decisions.''')
    with gr.Tab('4 · Funding status simulation'):
        gr.Markdown('''## API `2024-04-30` funding-state demonstration

This tab is independent from Tabs 1–3 and does not write to their payout or webhook ledger. It contains two visibly separated tools: real Airwallex sandbox calls and an in-memory fixture for funding states that Airwallex does not expose as arbitrary simulation transitions.

Funding status is independent of payout delivery status. `FUNDED` means the payout funding succeeded; it does not mean the beneficiary received the money. `FAILED` and `REVERSED` are offered only in the Linked Account direct-debit scenario, matching the documented applicability for this API generation.''')
        gr.Markdown('''### LIVE Airwallex sandbox funding calls

Use a **dedicated sandbox test payout**. Nothing is copied automatically from Tabs 1–3. Retrieval is read-only. Confirm funding and transfer transition are real state-changing sandbox calls and require explicit authorization.

The confirm-funding endpoint is valid only for accounts using manual-funding mode and only while `funding.status` is `REQUIRES_FUNDING_CONFIRMATION`. The transfer simulator changes the payout status; the subsequent GET reports the real funding status—it does not fabricate one.''')
        live_funding_id = gr.Textbox(label='Dedicated sandbox payment_id')
        live_get = gr.Button('Retrieve LIVE funding status', variant='primary')
        live_funding_result = gr.JSON(label='LIVE Airwallex response (redacted)')
        live_funding_note = gr.Markdown()
        live_get.click(
            ui_get_live_funding,
            live_funding_id,
            [live_funding_result, live_funding_note],
        )
        live_authorized = gr.Checkbox(
            label='I authorize state changes to this dedicated Airwallex SANDBOX payout',
            value=False,
        )
        with gr.Row():
            confirm_funding = gr.Button('Confirm funding via LIVE API')
            live_transfer_status = gr.Dropdown(
                svc.STEPS[svc.TARGET], value='PROCESSING', label='Real transfer status to simulate'
            )
            live_simulation_route = gr.Dropdown(
                list(svc.SIMULATION_ROUTES),
                value='Payment simulation (historical reference)',
                label='LIVE simulation endpoint family',
            )
            live_failure = gr.Dropdown(
                ['BENEFICIARY_BANK_RETURNED', 'INVALID_ACCOUNT_NAME_OR_NUMBER', 'ACCOUNT_CLOSED', 'OTHER'],
                value='BENEFICIARY_BANK_RETURNED',
                label='Failure type (used only for FAILED)',
            )
            simulate_and_observe = gr.Button('Simulate LIVE transfer + retrieve funding')
        confirm_funding.click(
            ui_confirm_live_funding,
            [live_funding_id, live_authorized],
            [live_funding_result, live_funding_note],
        )
        simulate_and_observe.click(
            ui_simulate_live_funding_observation,
            [live_funding_id, live_transfer_status, live_simulation_route,
             live_failure, live_authorized],
            [live_funding_result, live_funding_note],
        )
        gr.Markdown('''### LOCAL-only funding paths

Use this section for deterministic walkthroughs of states that cannot be directly forced through the documented Airwallex funding API. It does not call Airwallex, accept a payout ID, or write to any ledger.''')
        funding_state = gr.State({})
        scenario = gr.Dropdown(
            list(funding_sim.SCENARIOS),
            value='Manual funding confirmation',
            label='Local funding scenario',
        )
        start_funding = gr.Button('Start / reset local funding simulation', variant='primary')
        with gr.Row():
            funding_snapshot = gr.JSON(label='Current LOCAL funding snapshot')
            funding_history = gr.JSON(label='LOCAL funding event history')
        funding_explanation = gr.Markdown()
        funding_next = gr.Dropdown([], label='Next valid funding status', interactive=False)
        advance_funding = gr.Button('Advance LOCAL funding state')
        start_funding.click(
            ui_start_funding_simulation,
            scenario,
            [funding_state, funding_snapshot, funding_history, funding_explanation, funding_next],
        )
        advance_funding.click(
            ui_advance_funding_simulation,
            [funding_state, funding_next],
            [funding_state, funding_snapshot, funding_history, funding_explanation, funding_next],
        )
        gr.Markdown('''Supported paths:

- Manual confirmation: `REQUIRES_FUNDING_CONFIRMATION → SCHEDULED → PROCESSING → FUNDED`
- Wallet: `SCHEDULED → PROCESSING → FUNDED`
- Linked Account direct debit: `SCHEDULED → PROCESSING → FUNDED → REVERSED`, or `PROCESSING → FAILED`

Each generated local event is labeled `LOCAL_FUNDING_SIMULATION_NOT_AIRWALLEX`. The LIVE section above uses `GET /api/v1/payments/{payment_id}`, `POST /api/v1/payments/confirm_funding/{payment_id}`, and `POST /api/v1/simulation/transfers/{payment_id}/transition` against the Airwallex sandbox.''')

app = gr.mount_gradio_app(
    api,
    ui,
    path='/ui',
    footer_links=[],
    run_history=False,
)
