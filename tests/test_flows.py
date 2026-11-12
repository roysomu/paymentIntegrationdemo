import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import funding_simulation as funding_sim
import service as svc


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, 'DB_PATH', tmp_path / 'flow.sqlite3')
    svc.init_db()


def request():
    return svc.new_payload('beneficiary-sandbox-001', 1.0, 'USD', 'USD')


def test_payload_has_different_unique_ids():
    a = request()
    b = request()
    assert a['request_id'] != b['request_id']
    assert a['payment_currency'] == 'USD'
    assert a['beneficiary_id']
    assert a['reason'] == 'wages_salary'


def test_safe_error_includes_only_sanitized_validation_fields():
    import httpx

    request = httpx.Request('POST', svc.BASE + '/api/v1/payments/validate')
    response = httpx.Response(400, request=request, json={
        'code': 'validation_failed',
        'message': 'The request failed schema validation.',
        'source': 'reason',
        'beneficiary': {'account_number': 'do-not-display'},
        'details': {'errors': [
            {'code': '018', 'source': 'reason', 'params': {'allowed': 'wages_salary'}},
        ]},
    })
    error = svc.safe_error(httpx.HTTPStatusError('bad request', request=request, response=response))

    assert 'validation_failed' in error
    assert '"source": "reason"' in error
    assert 'wages_salary' in error
    assert 'do-not-display' not in error


@pytest.mark.parametrize('response', [[], {'items': []}])
def test_connection_accepts_historical_list_response_shapes(monkeypatch, response):
    calls = []

    def fake_call(method, path, version, body=None, params=None):
        calls.append((method, path, version, params))
        return response

    monkeypatch.setattr(svc, 'call_api', fake_call)

    assert 'Found 0 recent item(s)' in svc.check_connection()
    assert calls == [('GET', '/api/v1/payments', svc.TARGET, None)]


def test_beneficiary_list_normalizes_historical_response(monkeypatch):
    monkeypatch.setattr(svc, 'call_api', lambda *args, **kwargs: {'items': [{
        'beneficiary_id': 'ben-123',
        'payment_methods': ['LOCAL'],
        'beneficiary': {
            'entity_type': 'PERSONAL',
            'bank_details': {
                'account_name': 'Test Payee',
                'bank_country_code': 'US',
                'account_currency': 'USD',
            },
        },
    }]})

    rows = svc.list_beneficiaries(svc.TARGET)

    assert rows == [{
        'id': 'ben-123',
        'account_holder_name': 'Test Payee',
        'bank_country': 'US',
        'account_currency': 'USD',
        'entity_type': 'PERSONAL',
        'payment_methods': ['LOCAL'],
    }]
    assert svc.beneficiary_corridor(svc.TARGET, 'ben-123') == rows[0]


def test_saved_payee_registry_excludes_bank_account_data():
    svc.save_payee('Chase test payee', 'ben-chase', svc.TARGET, 'JPMorgan Chase Bank, NA')
    svc.save_payee('BofA test payee', 'ben-bofa', svc.TARGET, 'Bank of America, NA')

    rows = svc.list_saved_payees()

    assert [row['beneficiary_id'] for row in rows] == ['ben-bofa', 'ben-chase']
    assert all(row['bank_country'] == 'US' for row in rows)
    assert all(row['account_currency'] == 'USD' for row in rows)
    assert all(row['payment_method'] == 'LOCAL' for row in rows)
    assert all('account_number' not in row for row in rows)


def test_named_demo_flows_bind_payee_version_and_simulation(monkeypatch):
    svc.save_payee('BofA US Local', 'ben-bofa', svc.TARGET, 'Bank of America, NA')
    svc.save_payee('Chase US Local', 'ben-chase', svc.TARGET, 'JPMorgan Chase Bank, NA')
    calls = []

    def fake_api(method, path, version, body=None, params=None):
        calls.append((method, path, version, body))
        if path.endswith('/validate'):
            return 'OK'
        if path.endswith('/create'):
            return {
                'payment_id': 'pmt-chase-target',
                'beneficiary_id': body['beneficiary_id'],
                'request_id': body['request_id'],
                'status': 'SCHEDULED',
            }
        if method == 'GET':
            return {'payment_id': 'pmt-chase-target', 'status': 'PROCESSING'}
        return {'payment_id': 'pmt-chase-target', 'status': body['next_status']}

    monkeypatch.setattr(svc, 'call_api', fake_api)
    payload = svc.prepare_demo_payload('target_chase')

    assert payload['beneficiary_id'] == 'ben-chase'
    assert 'validation returned OK' in svc.validate_demo('target_chase', payload)
    created = svc.create_demo('target_chase', payload, True)
    assert created['payment_id'] == 'pmt-chase-target'
    transitioned = svc.simulate_demo('target_chase', 'pmt-chase-target', 'SENT')
    assert transitioned['route_used'] == '/api/v1/simulation/transfers/pmt-chase-target/transition'
    with pytest.raises(ValueError, match='belongs to API version|does not belong'):
        svc.simulate_demo('legacy_bofa', 'pmt-chase-target', 'DISPATCHED')


def test_local_funding_simulation_is_isolated_and_enforces_paths():
    state = funding_sim.start('Manual funding confirmation')
    assert state['source'] == funding_sim.SOURCE
    assert state['live_api_called'] is False
    assert funding_sim.allowed_next(state) == ['SCHEDULED']

    for next_status in ('SCHEDULED', 'PROCESSING', 'FUNDED'):
        state = funding_sim.advance(state, next_status)
    assert state['status'] == 'FUNDED'
    assert funding_sim.allowed_next(state) == []
    assert [event['funding_status'] for event in state['history']] == [
        'REQUIRES_FUNDING_CONFIRMATION', 'SCHEDULED', 'PROCESSING', 'FUNDED'
    ]
    assert all(event['source'] == funding_sim.SOURCE for event in state['history'])


def test_local_funding_failure_and_reversal_are_direct_debit_only():
    wallet = funding_sim.start('Wallet auto-funding')
    wallet = funding_sim.advance(wallet, 'PROCESSING')
    with pytest.raises(ValueError, match='Invalid funding transition'):
        funding_sim.advance(wallet, 'FAILED')

    failed = funding_sim.start('Linked Account direct debit')
    failed = funding_sim.advance(failed, 'PROCESSING')
    failed = funding_sim.advance(failed, 'FAILED')
    assert failed['status'] == 'FAILED'

    reversed_state = funding_sim.start('Linked Account direct debit')
    for next_status in ('PROCESSING', 'FUNDED', 'REVERSED'):
        reversed_state = funding_sim.advance(reversed_state, next_status)
    assert reversed_state['status'] == 'REVERSED'


def test_live_funding_calls_are_target_version_and_separate_from_ledger(monkeypatch):
    calls = []

    def fake_api(method, path, version, body=None, params=None):
        calls.append((method, path, version, body))
        return {
            'payment_id': 'pmt-funding-test',
            'status': 'SCHEDULED',
            'funding': {'status': 'FUNDED'},
        }

    monkeypatch.setattr(svc, 'call_api', fake_api)
    result = svc.get_live_funding('pmt-funding-test')

    assert result['origin'] == 'LIVE_AIRWALLEX_SANDBOX'
    assert result['payment']['funding']['status'] == 'FUNDED'
    assert calls == [('GET', '/api/v1/payments/pmt-funding-test', svc.TARGET, None)]
    payouts, observations = svc.list_records()
    assert payouts == []
    assert observations == []


def test_live_confirm_funding_requires_authorization_and_required_state(monkeypatch):
    calls = []
    funding_states = iter(['REQUIRES_FUNDING_CONFIRMATION', 'SCHEDULED'])

    def fake_api(method, path, version, body=None, params=None):
        calls.append((method, path, version, body))
        if method == 'GET':
            return {
                'payment_id': 'pmt-manual-funding',
                'status': 'SCHEDULED',
                'funding': {'status': next(funding_states)},
            }
        return {'payment_id': 'pmt-manual-funding', 'funding': {'status': 'SCHEDULED'}}

    monkeypatch.setattr(svc, 'call_api', fake_api)
    with pytest.raises(ValueError, match='Authorize'):
        svc.confirm_live_funding('pmt-manual-funding', False)
    result = svc.confirm_live_funding('pmt-manual-funding', True)

    assert result['endpoint'] == '/api/v1/payments/confirm_funding/pmt-manual-funding'
    assert calls[1] == (
        'POST', '/api/v1/payments/confirm_funding/pmt-manual-funding', svc.TARGET, None
    )
    assert result['retrieved_after']['payment']['funding']['status'] == 'SCHEDULED'


def test_live_transfer_simulation_retrieves_real_funding_afterward(monkeypatch):
    calls = []

    def fake_api(method, path, version, body=None, params=None):
        calls.append((method, path, version, body))
        if method == 'POST':
            return {'payment_id': 'pmt-live-transition', 'status': body['next_status']}
        return {
            'payment_id': 'pmt-live-transition',
            'status': 'PROCESSING',
            'funding': {'status': 'FUNDED'},
        }

    monkeypatch.setattr(svc, 'call_api', fake_api)
    result = svc.simulate_live_transfer_for_funding(
        'pmt-live-transition', 'PROCESSING', 'OTHER', True,
        'Transfer simulation (current guide)'
    )

    assert calls[0] == (
        'POST',
        '/api/v1/simulation/transfers/pmt-live-transition/transition',
        svc.TARGET,
        {'next_status': 'PROCESSING'},
    )
    assert calls[1][0:3] == (
        'GET', '/api/v1/payments/pmt-live-transition', svc.TARGET
    )
    assert result['retrieved_after']['payment']['funding']['status'] == 'FUNDED'


def test_validation_and_create_are_versioned(monkeypatch):
    calls = []
    def fake_call(method, path, version, body=None, params=None):
        calls.append((method, path, version))
        if '/validate' in path:
            return 'OK'
        if '/create' in path:
            return {'payment_id': 'pmt-test-0001', 'request_id': body['request_id'], 'status': 'NEW'}
    monkeypatch.setattr(svc, 'call_api', fake_call)
    payload = json.dumps(request())
    assert 'LIVE SANDBOX: validation returned OK' in svc.validate(svc.LEGACY, payload)
    result = svc.create(svc.LEGACY, payload, True)
    assert result['payment_id'] == 'pmt-test-0001'
    assert all(c[2] == svc.LEGACY for c in calls)
    assert calls[0][1].endswith('/payments/validate')
    assert calls[1][1].endswith('/payments/create')
    with pytest.raises(ValueError, match='already submitted'):
        svc.create(svc.LEGACY, payload, True)
    assert len(calls) == 2


def test_create_requires_exact_validation_and_authorization(monkeypatch):
    monkeypatch.setattr(svc, 'call_api', lambda *args, **kwargs: 'OK')
    payload = request()
    svc.validate(svc.TARGET, json.dumps(payload))
    with pytest.raises(ValueError, match='authorization'):
        svc.create(svc.TARGET, json.dumps(payload), False)
    payload['reference'] = 'CHANGED'
    with pytest.raises(ValueError, match='exact payload'):
        svc.create(svc.TARGET, json.dumps(payload), True)
    with pytest.raises(ValueError, match='exact payload'):
        svc.create(svc.LEGACY, json.dumps(request()), True)


def test_timeout_is_not_retried(monkeypatch):
    import httpx
    calls = []
    def fail(method, path, version, body=None, params=None):
        calls.append(path)
        if path.endswith('/validate'):
            return 'OK'
        raise httpx.ReadTimeout('timeout')
    monkeypatch.setattr(svc, 'call_api', fail)
    payload = json.dumps(request())
    svc.validate(svc.TARGET, payload)
    with pytest.raises(RuntimeError, match='UNKNOWN_CREATE_RESULT'):
        svc.create(svc.TARGET, payload, True)
    with pytest.raises(ValueError, match='already submitted'):
        svc.create(svc.TARGET, payload, True)
    assert len(calls) == 2


def test_transition_routes_and_authorization(monkeypatch):
    calls = []
    def fake_api(method, path, version, body=None, params=None):
        calls.append((method, path, version, body))
        if path.endswith('/validate'):
            return 'OK'
        if path.endswith('/create'):
            return {'payment_id': 'pmt-target-01', 'status': 'SCHEDULED', 'request_id': body['request_id']}
        if method == 'GET':
            return {'payment_id': 'pmt-target-01', 'status': 'PROCESSING', 'funding': {'status': 'FUNDED'}}
        return {'payment_id': 'pmt-target-01', 'status': body['next_status']}
    monkeypatch.setattr(svc, 'call_api', fake_api)
    payload = json.dumps(request())
    svc.validate(svc.TARGET, payload)
    svc.create(svc.TARGET, payload, True)
    outcome = svc.simulate(svc.TARGET, 'pmt-target-01', 'SENT',
                           'Transfer simulation (current guide)')
    assert outcome['route_used'] == '/api/v1/simulation/transfers/pmt-target-01/transition'
    assert calls[-2][2] == svc.TARGET
    assert calls[-2][3]['next_status'] == 'SENT'
    assert outcome['retrieved_now']['funding']['status'] == 'FUNDED'
    with pytest.raises(ValueError, match='not supported'):
        svc.simulate(svc.LEGACY, 'pmt-target-01', 'PAID', 'Transfer simulation (current guide)')


def test_webhook_dedup_and_non_authoritative_observations():
    a = svc.fixture_event(svc.TARGET, 'pmt-local-test', 'payment.paid')
    assert svc.record_webhook(svc.TARGET, a, 'LOCAL_FIXTURE_NOT_AIRWALLEX')
    assert not svc.record_webhook(svc.TARGET, a, 'LOCAL_FIXTURE_NOT_AIRWALLEX')
    b = svc.fixture_event(svc.TARGET, 'pmt-local-test', 'payment.failed')
    assert svc.record_webhook(svc.TARGET, b, 'LOCAL_FIXTURE_NOT_AIRWALLEX')
    assert len(svc.list_records()[1]) == 2


def test_signature_verification_and_timestamp_rejection():
    body = b'{"id":"evt-1"}'
    ts = str(int(time.time() * 1000))
    sig = hmac.new(b'mytestsecret', ts.encode()+body, hashlib.sha256).hexdigest()
    assert svc.verify_signature('mytestsecret', ts, sig, body)
    assert not svc.verify_signature('other', ts, sig, body)
    assert not svc.verify_signature('mytestsecret', str(int(ts) - 900000), sig, body)


def test_webhook_server_requires_signature(monkeypatch):
    monkeypatch.setenv('AIRWALLEX_WEBHOOK_SECRET_TARGET', 'abc')
    from app import app
    client = TestClient(app)
    payload = {'id': 'evt-abcdef', 'name': 'payment.paid', 'version': svc.TARGET,
               'data': {'object': {'payment_id': 'pmt-sandbox-1', 'status': 'PAID'}}}
    raw = json.dumps(payload).encode()
    response = client.post('/webhooks/target', content=raw)
    assert response.status_code == 401
    ts = str(int(time.time()*1000))
    sig = hmac.new(b'abc', ts.encode()+raw, hashlib.sha256).hexdigest()
    good = client.post('/webhooks/target', content=raw, headers={'x-timestamp': ts, 'x-signature': sig})
    assert good.status_code == 200, good.text
    assert good.json()['accepted']
    again = client.post('/webhooks/target', content=raw, headers={'x-timestamp': ts, 'x-signature': sig})
    assert again.status_code == 200 and again.json()['duplicate']
