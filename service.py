"""Real, sandbox-only Airwallex historical payout flows.

No automatic fallback to synthetic API responses. POST requests are never retried.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')

LEGACY = '2022-12-13'
TARGET = '2024-04-30'
VERSIONS = (LEGACY, TARGET)
BASE = 'https://api.sandbox.airwallex.com'
ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('LAB_DB_PATH', str(ROOT / 'status_lab.sqlite3')))
_DB_LOCK = threading.RLock()
_TOKEN_LOCK = threading.Lock()
_TOKEN: dict[str, Any] = {'value': '', 'expiry': 0.0}

STEPS = {
    LEGACY: ['READY_FOR_DISPATCH', 'DISPATCHED', 'FAILED'],
    TARGET: ['PROCESSING', 'SENT', 'PAID', 'FAILED'],
}
SIMULATION_ROUTES = {
    'Transfer simulation (current guide)': '/api/v1/simulation/transfers/{id}/transition',
    'Payment simulation (historical reference)': '/api/v1/simulation/payments/{id}/transition',
}
DEMO_FLOWS = {
    'legacy_bofa': {
        'title': 'Legacy · BofA',
        'payee_label': 'BofA US Local',
        'version': LEGACY,
        'default_simulation_route': 'Payment simulation (historical reference)',
        'successful_path': 'NEW → READY_FOR_DISPATCH → DISPATCHED',
    },
    'target_chase': {
        'title': 'Target · Chase',
        'payee_label': 'Chase US Local',
        'version': TARGET,
        'default_simulation_route': 'Transfer simulation (current guide)',
        'successful_path': 'SCHEDULED → PROCESSING → SENT → PAID',
    },
}

STATUS_DETAILS = {
    'NEW': ('Created', 'The payout has been accepted, but has not left Airwallex.'),
    'IN_REVIEW': ('Under review', 'Operations may need to respond to compliance or risk-review requests.'),
    'READY_FOR_DISPATCH': ('Ready to dispatch', 'Airwallex is ready to dispatch; a cut-off may cause waiting.'),
    'DISPATCHED': ('Dispatched; not confirmed received', 'Legacy status indicates funds were sent out; bank-side progress is not independently visible.'),
    'SCHEDULED': ('Scheduled', 'Track the due date and funding requirement independently.'),
    'OVERDUE': ('Funding overdue', 'Investigate funding and prevent payroll deadline misses.'),
    'PROCESSING': ('Processing', 'Funding and payment processing can be monitored separately.'),
    'SENT': ('Sent to banking partner', 'Airwallex has sent the payout; banking-partner processing remains pending.'),
    'PAID': ('Processed by banking partner', 'Additional confidence for customer support and reconciliation, but late rejection remains possible.'),
    'FAILED': ('Failed / needs intervention', 'Open a payroll exception; investigate before considering any replacement payment.'),
    'CANCELLED': ('Cancelled', 'No replacement should be attempted without checking the original financial outcome.'),
    'UNKNOWN': ('Status unknown', 'Poll the authoritative API; do not create another payout while outcome is uncertain.'),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK, connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS validations (
            request_id TEXT PRIMARY KEY, version TEXT NOT NULL, payload_sha TEXT NOT NULL,
            validated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payouts (
            request_id TEXT PRIMARY KEY, payment_id TEXT UNIQUE, version TEXT NOT NULL,
            status TEXT DEFAULT 'UNKNOWN', funding TEXT DEFAULT '', creation_state TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source TEXT NOT NULL,
            beneficiary_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, source TEXT NOT NULL,
            version TEXT NOT NULL, payment_id TEXT, event_id TEXT, event_name TEXT,
            payout_status TEXT, funding_status TEXT, failure_reason TEXT, details TEXT
        );
        CREATE TABLE IF NOT EXISTS saved_payees (
            label TEXT PRIMARY KEY, beneficiary_id TEXT UNIQUE NOT NULL, version TEXT NOT NULL,
            bank_name TEXT NOT NULL, bank_country TEXT NOT NULL, account_currency TEXT NOT NULL,
            payment_method TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_id ON observations(event_id) WHERE event_id IS NOT NULL;
        ''')
        payout_columns = {row['name'] for row in db.execute('PRAGMA table_info(payouts)')}
        if 'beneficiary_id' not in payout_columns:
            db.execute("ALTER TABLE payouts ADD COLUMN beneficiary_id TEXT DEFAULT ''")


init_db()


def safe_error(exc: Exception) -> str:
    """Keep user-facing errors actionable without exposing sensitive payloads."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        try:
            data = exc.response.json()
            # Allow only known, non-personal diagnostic fields. Airwallex commonly
            # puts the rejected request path in `source`, or under
            # `details.errors`, so retaining those paths makes 400s actionable
            # without echoing the submitted beneficiary or payer data.
            fields = {
                k: str(data[k])[:220]
                for k in ('code', 'message', 'source')
                if isinstance(data, dict) and isinstance(data.get(k), str)
            }
            details = data.get('details') if isinstance(data, dict) else None
            errors = details.get('errors') if isinstance(details, dict) else None
            if isinstance(errors, list):
                safe_errors = []
                for error in errors[:10]:
                    if not isinstance(error, dict):
                        continue
                    safe_error_fields = {
                        k: str(error[k])[:220]
                        for k in ('code', 'source')
                        if isinstance(error.get(k), (str, int, float))
                    }
                    params = error.get('params')
                    if isinstance(params, dict):
                        safe_error_fields['params'] = {
                            str(k)[:80]: str(v)[:120]
                            for k, v in list(params.items())[:10]
                            if isinstance(v, (str, int, float, bool))
                        }
                    if safe_error_fields:
                        safe_errors.append(safe_error_fields)
                if safe_errors:
                    fields['errors'] = safe_errors
        except (ValueError, TypeError):
            fields = {}
        return f'Airwallex HTTP {code}: {json.dumps(fields)}' if fields else f'Airwallex HTTP {code} (check permissions, payload, version and payout status)'
    if isinstance(exc, httpx.TimeoutException):
        return 'Timeout: payout creation outcome may be UNKNOWN. Check the payout by request_id; do not blindly retry.'
    if isinstance(exc, httpx.RequestError):
        return f'Network problem ({type(exc).__name__}); check sandbox connectivity.'
    return str(exc)[:240] if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__


def token() -> str:
    configured = os.getenv('AIRWALLEX_ACCESS_TOKEN', '').strip()
    if configured:
        return configured
    client_id, api_key = os.getenv('AIRWALLEX_CLIENT_ID'), os.getenv('AIRWALLEX_API_KEY')
    if not client_id or not api_key:
        raise RuntimeError('Set AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY in .env')
    with _TOKEN_LOCK:
        if _TOKEN['value'] and time.time() + 90 < _TOKEN['expiry']:
            return str(_TOKEN['value'])
        with httpx.Client(timeout=20) as client:
            response = client.post(BASE + '/api/v1/authentication/login', headers={
                'x-client-id': client_id, 'x-api-key': api_key, 'Content-Type': 'application/json'
            })
            response.raise_for_status()
            data = response.json()
        expiry = data.get('expires_at', '')
        try:
            epoch = datetime.fromisoformat(expiry.replace('Z', '+00:00')).timestamp()
        except (ValueError, AttributeError):
            epoch = time.time() + 1500
        _TOKEN.update(value=data['token'], expiry=epoch)
        return str(data['token'])


def headers(version: str) -> dict[str, str]:
    if version not in VERSIONS:
        raise ValueError('Unsupported version')
    result = {'Authorization': f'Bearer {token()}', 'x-api-version': version, 'Content-Type': 'application/json'}
    connected = os.getenv('AIRWALLEX_CONNECTED_ACCOUNT_ID', '').strip()
    if connected:
        result['x-on-behalf-of'] = connected
    return result


def call_api(method: str, path: str, version: str, body: dict | None = None, params: dict | None = None) -> Any:
    if not path.startswith('/api/v1/') or '/..' in path:
        raise ValueError('Unrecognized sandbox endpoint')
    with httpx.Client(timeout=25, follow_redirects=False) as client:
        response = client.request(method, BASE + path, headers=headers(version), json=body, params=params)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return response.text  # Validation endpoint can return plain "OK"


def scrub(response: Any) -> dict[str, Any]:
    """Allowlist only payment metadata: never display account numbers, addresses or access tokens."""
    if not isinstance(response, dict):
        return {'api_response': str(response)[:120]}
    keys = ('id', 'payment_id', 'request_id', 'beneficiary_id', 'status', 'payment_amount', 'payment_currency',
            'source_currency', 'dispatch_date', 'created_at', 'last_updated_at', 'updated_at',
            'failure_reason', 'failure_type')
    result = {k: response[k] for k in keys if k in response}
    if isinstance(response.get('funding'), dict):
        funding = response['funding']
        result['funding'] = {k: funding[k] for k in ('status', 'failure_reason') if k in funding}
    return result


def payment_id_of(data: dict) -> str:
    return str(data.get('payment_id') or data.get('id') or '')


def collection_items(data: Any) -> list:
    """Normalize Airwallex list responses seen across historical sandbox versions."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get('items'), list):
        return data['items']
    raise RuntimeError('Unexpected list response from Airwallex')


def check_connection() -> str:
    # Some historical sandbox accounts reject pagination parameters on this
    # endpoint and return a bare array instead of the documented `items`
    # envelope. A parameter-free read is sufficient for this authorization
    # check, and collection_items supports both observed response shapes.
    result = call_api('GET', '/api/v1/payments', TARGET)
    return f'Connected to Airwallex sandbox; target API listing authorized. Found {len(collection_items(result))} recent item(s).'


def list_beneficiaries(version: str) -> list[dict]:
    data = call_api('GET', '/api/v1/beneficiaries', version, params={'page_num': 0, 'page_size': 20})
    # Simple and intentionally minimal selection output. Historical sandbox
    # responses use beneficiary_id/payment_methods and nest the safe corridor
    # metadata below beneficiary.bank_details.
    results = []
    for i in collection_items(data):
        if isinstance(i, dict):
            beneficiary = i.get('beneficiary') if isinstance(i.get('beneficiary'), dict) else {}
            bank = beneficiary.get('bank_details') if isinstance(beneficiary.get('bank_details'), dict) else {}
            account_holder_name = bank.get('account_name')
            methods = i.get('payment_methods', i.get('transfer_methods', []))
            results.append({
                'id': i.get('beneficiary_id') or i.get('id'),
                'account_holder_name': account_holder_name,
                'bank_country': bank.get('bank_country_code'),
                'account_currency': bank.get('account_currency'),
                'entity_type': beneficiary.get('entity_type'),
                'payment_methods': methods if isinstance(methods, list) else [],
            })
    return results


def beneficiary_corridor(version: str, beneficiary_id: str) -> dict:
    """Return non-sensitive routing metadata for one saved beneficiary."""
    wanted = beneficiary_id.strip()
    for item in list_beneficiaries(version):
        if item.get('id') == wanted:
            return item
    raise ValueError('Selected beneficiary was not found in the first 20 saved sandbox beneficiaries')


def save_payee(label: str, beneficiary_id: str, version: str, bank_name: str,
               bank_country: str = 'US', account_currency: str = 'USD',
               payment_method: str = 'LOCAL') -> None:
    """Persist a created beneficiary ID without retaining any bank-account data."""
    if version not in VERSIONS:
        raise ValueError('Unsupported version')
    if not label.strip() or not beneficiary_id.strip():
        raise ValueError('Payee label and beneficiary ID are required')
    with _DB_LOCK, connect() as db:
        db.execute('''
            INSERT INTO saved_payees
                (label, beneficiary_id, version, bank_name, bank_country,
                 account_currency, payment_method, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(label) DO UPDATE SET
                beneficiary_id=excluded.beneficiary_id,
                version=excluded.version,
                bank_name=excluded.bank_name,
                bank_country=excluded.bank_country,
                account_currency=excluded.account_currency,
                payment_method=excluded.payment_method,
                created_at=excluded.created_at
        ''', (label.strip(), beneficiary_id.strip(), version, bank_name.strip(),
              bank_country, account_currency, payment_method, now()))


def list_saved_payees() -> list[dict]:
    with _DB_LOCK, connect() as db:
        return [dict(row) for row in db.execute('''
            SELECT label, beneficiary_id, version, bank_name, bank_country,
                   account_currency, payment_method, created_at
            FROM saved_payees ORDER BY label
        ''')]


def saved_payee(label: str) -> dict | None:
    with _DB_LOCK, connect() as db:
        row = db.execute('''
            SELECT label, beneficiary_id, version, bank_name, bank_country,
                   account_currency, payment_method, created_at
            FROM saved_payees WHERE label=?
        ''', (label,)).fetchone()
    return dict(row) if row else None


def demo_flow(flow: str, require_payee: bool = True) -> dict:
    config = DEMO_FLOWS.get(flow)
    if not config:
        raise ValueError('Unknown demo flow; use legacy_bofa or target_chase')
    result = {'flow': flow, **config, 'allowed_statuses': STEPS[config['version']]}
    payee = saved_payee(config['payee_label'])
    if require_payee and not payee:
        raise RuntimeError(f'Saved payee {config["payee_label"]!r} is not configured')
    result['payee'] = payee
    return result


def list_demo_flows() -> list[dict]:
    return [demo_flow(flow, require_payee=False) for flow in DEMO_FLOWS]


def new_payload(beneficiary_id: str, amount: float, currency: str, source_currency: str,
                payer_id: str = '', payment_method: str = 'LOCAL') -> dict:
    """Starter payload using a saved sandbox beneficiary; user can edit for corridor rules."""
    if not beneficiary_id.strip():
        raise ValueError('Select an existing saved beneficiary in Airwallex sandbox')
    if amount <= 0 or amount > 100:
        raise ValueError('For this demo, payout amount must be > 0 and <= 100 sandbox units')
    if len(currency) != 3 or len(source_currency) != 3:
        raise ValueError('Currency must be a three-letter ISO code')
    req_id = str(uuid.uuid4())
    payload = {
        'beneficiary_id': beneficiary_id.strip(),
        'payment_amount': str(round(amount, 2)),
        'payment_currency': currency.upper().strip(),
        'source_currency': source_currency.upper().strip(),
        'payment_method': payment_method,
        'reason': 'wages_salary',
        'reference': 'PAYROLL-LAB-' + req_id[:8].upper(),
        'request_id': req_id,
    }
    if payer_id.strip():
        payload['payer_id'] = payer_id.strip()
    return payload


def prepare_demo_payload(flow: str, amount: float = 1.0, source_currency: str = 'USD',
                         payer_id: str = '') -> dict:
    config = demo_flow(flow)
    payee = config['payee']
    return new_payload(
        str(payee['beneficiary_id']), amount, str(payee['account_currency']),
        source_currency, payer_id, str(payee['payment_method']),
    )


def require_demo_payload(flow: str, payload: dict) -> dict:
    config = demo_flow(flow)
    payee = config['payee']
    expected = {
        'beneficiary_id': payee['beneficiary_id'],
        'payment_currency': payee['account_currency'],
        'payment_method': payee['payment_method'],
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f'{flow} requires {key}={value}')
    return config


def validate_demo(flow: str, payload: dict) -> str:
    config = require_demo_payload(flow, payload)
    return validate(str(config['version']), json.dumps(payload))


def create_demo(flow: str, payload: dict, authorized: bool) -> dict:
    config = require_demo_payload(flow, payload)
    return create(str(config['version']), json.dumps(payload), authorized)


def parse_payload(text: str) -> dict:
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError('Provide a valid JSON request body') from exc
    if not isinstance(obj, dict):
        raise ValueError('Payout request must be a JSON object')
    if not isinstance(obj.get('request_id'), str) or not obj['request_id'].strip():
        raise ValueError('Payout payload requires a unique request_id')
    return obj


def digest_payload(obj: dict) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate(version: str, text: str) -> str:
    payload = parse_payload(text)
    result = call_api('POST', '/api/v1/payments/validate', version, payload)
    with _DB_LOCK, connect() as db:
        db.execute('INSERT OR REPLACE INTO validations VALUES (?, ?, ?, ?)',
                   (payload['request_id'], version, digest_payload(payload), now()))
    return f'LIVE SANDBOX: validation returned {result!s}. This exact payload is now eligible for a single authorized creation.'


def create(version: str, text: str, authorized: bool) -> dict:
    if not authorized:
        raise ValueError('Check the explicit sandbox creation authorization box first')
    payload = parse_payload(text)
    req_id = payload['request_id']
    sha = digest_payload(payload)
    with _DB_LOCK, connect() as db:
        row = db.execute('SELECT * FROM validations WHERE request_id=?', (req_id,)).fetchone()
        if not row or row['version'] != version or row['payload_sha'] != sha:
            raise ValueError('Validate this exact payload under the selected API version first')
        existing = db.execute('SELECT * FROM payouts WHERE request_id=?', (req_id,)).fetchone()
        if existing:
            raise ValueError(f'Request_id already submitted (state={existing["creation_state"]}). Never reissue blindly.')
        db.execute('''
            INSERT INTO payouts
                (request_id, payment_id, version, status, funding, creation_state,
                 created_at, updated_at, source, beneficiary_id)
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (req_id, version, 'UNKNOWN', '', 'CREATION_IN_FLIGHT', now(), now(),
              'CREATED_BY_LAB', str(payload.get('beneficiary_id') or '')))
    # Important: NO retries for payout creation. On ambiguity require manual reconciliation.
    try:
        result = call_api('POST', '/api/v1/payments/create', version, payload)
        if not isinstance(result, dict):
            raise RuntimeError('Unexpected non-object create response; query payout by request_id')
        safe = scrub(result)
        pid = payment_id_of(result)
        if not pid:
            raise RuntimeError('Create response had no payment_id; do not retry, investigate request_id')
        with _DB_LOCK, connect() as db:
            db.execute('UPDATE payouts SET payment_id=?, status=?, funding=?, creation_state=?, updated_at=? WHERE request_id=?',
                       (pid, str(result.get('status') or 'UNKNOWN'), str((result.get('funding') or {}).get('status', '')),
                        'CREATED', now(), req_id))
        record_observation('API_CREATE', version, pid, safe)
        return {'status': 'CREATED IN REAL AIRWALLEX SANDBOX', 'version': version, **safe}
    except Exception as exc:
        state = 'CREATE_REJECTED' if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 401, 403, 404, 422) else 'UNKNOWN_CREATE_RESULT'
        with _DB_LOCK, connect() as db:
            db.execute('UPDATE payouts SET creation_state=?, updated_at=? WHERE request_id=?', (state, now(), req_id))
        raise RuntimeError(safe_error(exc) + f' | Recorded creation state: {state}; do not resend same request.') from exc


def get_payment(version: str, pid: str) -> dict:
    if not pid.strip():
        raise ValueError('Enter a sandbox payment ID')
    data = call_api('GET', '/api/v1/payments/' + pid.strip(), version)
    if not isinstance(data, dict):
        raise RuntimeError('Unexpected payment retrieval response')
    safe = scrub(data)
    record_observation('API_GET', version, pid.strip(), safe)
    with _DB_LOCK, connect() as db:
        db.execute('UPDATE payouts SET status=?, funding=?, updated_at=? WHERE payment_id=? AND version=?',
                   (str(safe.get('status', 'UNKNOWN')), str((safe.get('funding') or {}).get('status', '')), now(), pid.strip(), version))
    return safe


def attach_existing(version: str, pid: str) -> dict:
    payment = get_payment(version, pid)
    req_id = str(payment.get('request_id') or 'attached-' + uuid.uuid4().hex)
    with _DB_LOCK, connect() as db:
        db.execute('INSERT OR IGNORE INTO payouts (request_id, payment_id, version, status, funding, creation_state, created_at, updated_at, source, beneficiary_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (req_id, pid, version, str(payment.get('status', 'UNKNOWN')), str((payment.get('funding') or {}).get('status', '')),
                    'ATTACHED_READ_ONLY', now(), now(), 'ATTACHED_READ_ONLY', str(payment.get('beneficiary_id') or '')))
    return payment


def simulate(version: str, pid: str, status: str, route: str, failure_type: str = 'BENEFICIARY_BANK_RETURNED') -> dict:
    if status not in STEPS[version]:
        raise ValueError('Status is not supported for this version')
    if route not in SIMULATION_ROUTES:
        raise ValueError('Choose a documented simulation route')
    with _DB_LOCK, connect() as db:
        row = db.execute('SELECT source FROM payouts WHERE payment_id=? AND version=?', (pid, version)).fetchone()
    if not row or row['source'] != 'CREATED_BY_LAB':
        raise ValueError('For safety, only payouts created by THIS lab can be transitioned. Attached payouts are read-only.')
    body = {'next_status': status}
    if status == 'FAILED':
        body['failure_type'] = failure_type
    endpoint = SIMULATION_ROUTES[route].format(id=pid)
    result = call_api('POST', endpoint, version, body)
    if not isinstance(result, dict):
        raise RuntimeError('Unexpected simulation response')
    safe = scrub(result)
    record_observation('SANDBOX_TRANSITION (' + route + ')', version, pid, safe)
    # Fetch authoritative current state after transition, if available; some state updates are async.
    try:
        current = get_payment(version, pid)
    except Exception:
        current = {'note': 'Simulation accepted; GET not yet available. Refresh status.'}
    return {'simulation_response': safe, 'retrieved_now': current, 'route_used': endpoint}


def require_demo_payment(flow: str, pid: str) -> dict:
    config = demo_flow(flow)
    payee = config['payee']
    with _DB_LOCK, connect() as db:
        row = db.execute(
            'SELECT version, source, beneficiary_id FROM payouts WHERE payment_id=?',
            (pid.strip(),),
        ).fetchone()
    if not row:
        raise ValueError('Payment ID is not recorded in this lab')
    if row['version'] != config['version']:
        raise ValueError(f'Payment belongs to API version {row["version"]}, not {config["version"]}')
    if row['beneficiary_id'] != payee['beneficiary_id']:
        raise ValueError(f'Payment does not belong to the payee assigned to {flow}')
    return config


def get_demo_payment(flow: str, pid: str) -> dict:
    config = require_demo_payment(flow, pid)
    return get_payment(str(config['version']), pid)


def simulate_demo(flow: str, pid: str, status: str, failure_type: str = 'BENEFICIARY_BANK_RETURNED',
                  route: str | None = None) -> dict:
    config = require_demo_payment(flow, pid)
    selected_route = route or str(config['default_simulation_route'])
    return simulate(str(config['version']), pid, status, selected_route, failure_type)


def _funding_lab_payment_id(payment_id: str) -> str:
    value = payment_id.strip()
    if not value:
        raise ValueError('Enter a dedicated Airwallex sandbox payment ID')
    if not all(char.isalnum() or char in '-_' for char in value):
        raise ValueError('Payment ID contains unsupported characters')
    return value


def get_live_funding(payment_id: str) -> dict:
    """Read target-version funding state without touching the normal payout ledger."""
    pid = _funding_lab_payment_id(payment_id)
    response = call_api('GET', '/api/v1/payments/' + pid, TARGET)
    if not isinstance(response, dict):
        raise RuntimeError('Unexpected payment retrieval response')
    return {
        'origin': 'LIVE_AIRWALLEX_SANDBOX',
        'api_version': TARGET,
        'operation': 'GET funding observation',
        'payment': scrub(response),
    }


def confirm_live_funding(payment_id: str, authorized: bool) -> dict:
    """Confirm manual funding in sandbox, isolated from the normal payout ledger."""
    if not authorized:
        raise ValueError('Authorize the LIVE sandbox funding confirmation first')
    pid = _funding_lab_payment_id(payment_id)
    before = get_live_funding(pid)
    funding = before['payment'].get('funding')
    current = funding.get('status') if isinstance(funding, dict) else None
    if current != 'REQUIRES_FUNDING_CONFIRMATION':
        raise ValueError(
            'Confirm funding is valid only when LIVE funding.status is '
            'REQUIRES_FUNDING_CONFIRMATION'
        )
    response = call_api('POST', f'/api/v1/payments/confirm_funding/{pid}', TARGET)
    try:
        after = get_live_funding(pid)
    except Exception:
        after = {'note': 'Confirmation accepted; refresh LIVE funding status to observe the asynchronous result.'}
    return {
        'origin': 'LIVE_AIRWALLEX_SANDBOX',
        'api_version': TARGET,
        'operation': 'POST confirm funding',
        'endpoint': f'/api/v1/payments/confirm_funding/{pid}',
        'before': before['payment'],
        'api_response': scrub(response),
        'retrieved_after': after,
    }


def simulate_live_transfer_for_funding(payment_id: str, status: str, failure_type: str,
                                       authorized: bool, route: str | None = None) -> dict:
    """Advance a real sandbox transfer, then observe its real funding state.

    This uses no payout-ledger state and is intentionally separate from simulate().
    The Airwallex endpoint transitions the transfer status; funding is read, not forged.
    """
    if not authorized:
        raise ValueError('Authorize the LIVE sandbox transfer transition first')
    if status not in STEPS[TARGET]:
        raise ValueError('Status is not supported for API version 2024-04-30')
    selected_route = route or 'Payment simulation (historical reference)'
    if selected_route not in SIMULATION_ROUTES:
        raise ValueError('Choose a documented simulation route')
    pid = _funding_lab_payment_id(payment_id)
    body = {'next_status': status}
    if status == 'FAILED':
        body['failure_type'] = failure_type
    endpoint = SIMULATION_ROUTES[selected_route].format(id=pid)
    response = call_api('POST', endpoint, TARGET, body)
    try:
        after = get_live_funding(pid)
    except Exception:
        after = {'note': 'Transition accepted; refresh LIVE funding status to observe the asynchronous result.'}
    return {
        'origin': 'LIVE_AIRWALLEX_SANDBOX',
        'api_version': TARGET,
        'operation': 'POST transfer transition, then GET funding',
        'endpoint': endpoint,
        'route': selected_route,
        'requested_transfer_status': status,
        'api_response': scrub(response),
        'retrieved_after': after,
    }


def record_observation(source: str, version: str, payment_id: str, data: dict, event_id: str | None = None,
                       event_name: str = '') -> bool:
    status = str(data.get('status') or '')
    funding = str((data.get('funding') or {}).get('status') or '') if isinstance(data.get('funding'), dict) else ''
    details = {k: data[k] for k in ('request_id', 'failure_type') if k in data}
    with _DB_LOCK, connect() as db:
        if event_id and db.execute('SELECT 1 FROM observations WHERE event_id=?', (event_id,)).fetchone():
            return False
        db.execute('INSERT INTO observations (created_at, source, version, payment_id, event_id, event_name, payout_status, funding_status, failure_reason, details) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (now(), source, version, payment_id, event_id, event_name, status, funding,
                    str(data.get('failure_reason') or '')[:200], json.dumps(details)))
    return True


def list_records() -> tuple[list[dict], list[dict]]:
    with _DB_LOCK, connect() as db:
        payouts = [dict(x) for x in db.execute('SELECT version, payment_id, request_id, beneficiary_id, status, funding, creation_state, source, updated_at FROM payouts ORDER BY updated_at DESC LIMIT 30')]
        events = [dict(x) for x in db.execute('SELECT created_at, source, version, payment_id, event_name, payout_status, funding_status, failure_reason FROM observations ORDER BY id DESC LIMIT 75')]
    return payouts, events


def business_explanation(version: str, status: str, funding: str = '') -> str:
    title, detail = STATUS_DETAILS.get(status, STATUS_DETAILS['UNKNOWN'])
    funding_line = f' Funding status is independently `{funding}`.' if funding else ''
    if version == LEGACY and status == 'DISPATCHED':
        detail += ' Legacy consumers may mistake dispatched for bank-confirmed; the new PAID event closes this visibility gap.'
    return f'**{title}** — {detail}{funding_line}'


def compare_value(version: str, data: dict) -> dict:
    status = str(data.get('status', 'UNKNOWN'))
    fund = str((data.get('funding') or {}).get('status', ''))
    return {'API version': version, 'Payout state': status, 'Funding state': fund or 'Not reported in response',
            'Operations interpretation': STATUS_DETAILS.get(status, STATUS_DETAILS['UNKNOWN'])[1],
            'Webhook event expected': ('payment.' + status.lower()) if status and status != 'UNKNOWN' else '—',
            'Origin': 'LIVE SANDBOX API_GET (single snapshot; not a timeline)'}


def verify_signature(secret: str, timestamp: str, signature: str, raw: bytes) -> bool:
    if not all((secret, timestamp, signature)):
        return False
    try:
        age = abs(time.time() - int(timestamp)/1000.0)
    except (TypeError, ValueError):
        return False
    if age > 300:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def record_webhook(version: str, event: dict, source: str = 'SIGNED_SANDBOX_WEBHOOK') -> bool:
    ev_id = event.get('id')
    if not isinstance(ev_id, str) or not ev_id:
        raise ValueError('Webhook missing event id')
    event_name = str(event.get('name') or '')
    if not event_name.startswith('payment.'):
        raise ValueError('Only the historical payment.* event family is supported')
    obj = (event.get('data') or {}).get('object', {})
    if not isinstance(obj, dict):
        raise ValueError('Invalid webhook data.object')
    pid = payment_id_of(obj)
    if not pid:
        raise ValueError('Webhook missing payout identifier')
    # Some webhook payloads omit explicit version; route identifies subscription's version.
    if event.get('version') and event['version'] != version:
        raise ValueError('Event version conflicts with configured subscription')
    status = str(obj.get('status') or '')
    if not status and not event_name.startswith('payment.funding.'):
        status = event_name.rsplit('.', 1)[-1].upper()
    funding = obj.get('funding') if isinstance(obj.get('funding'), dict) else {}
    if event_name.startswith('payment.funding.') and not funding:
        funding = {'status': event_name.rsplit('.', 1)[-1].upper()}
    clean = {'status': status, 'funding': {'status': funding.get('status')} if funding.get('status') else {},
             'failure_reason': str(obj.get('failure_reason') or '')[:200]}
    # Webhook is an observation, not authoritative state mutation: out-of-order events remain auditable.
    return record_observation(source, version, pid, clean, event_id=ev_id, event_name=event_name)


def fixture_event(version: str, payment_id: str, event_name: str) -> dict:
    if not event_name.startswith('payment.'):
        raise ValueError('Use payment.* events only')
    status = event_name.split('.')[-1].upper()
    return {'id': 'local-' + str(uuid.uuid4()), 'name': event_name, 'version': version,
            'created_at': now(), 'data': {'object': {'payment_id': payment_id, 'status': status}}}
