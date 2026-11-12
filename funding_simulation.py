"""Isolated, in-memory funding lifecycle simulator for API 2024-04-30.

This module deliberately has no Airwallex client or database dependency. It is
used only by the LOCAL funding-simulation tab and never mutates a real payout.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any


API_VERSION = '2024-04-30'
SOURCE = 'LOCAL_FUNDING_SIMULATION_NOT_AIRWALLEX'

SCENARIOS = {
    'Manual funding confirmation': {
        'description': 'Demonstrates the confirmation gate before funding is scheduled.',
        'initial_status': 'REQUIRES_FUNDING_CONFIRMATION',
        'transitions': {
            'REQUIRES_FUNDING_CONFIRMATION': ['SCHEDULED'],
            'SCHEDULED': ['PROCESSING'],
            'PROCESSING': ['FUNDED'],
            'FUNDED': [],
        },
    },
    'Wallet auto-funding': {
        'description': 'Demonstrates successful funding from an available Airwallex Wallet balance.',
        'initial_status': 'SCHEDULED',
        'transitions': {
            'SCHEDULED': ['PROCESSING'],
            'PROCESSING': ['FUNDED'],
            'FUNDED': [],
        },
    },
    'Linked Account direct debit': {
        'description': 'Demonstrates success, rejection, or a later recall for direct-debit funding.',
        'initial_status': 'SCHEDULED',
        'transitions': {
            'SCHEDULED': ['PROCESSING'],
            'PROCESSING': ['FUNDED', 'FAILED'],
            'FUNDED': ['REVERSED'],
            'FAILED': [],
            'REVERSED': [],
        },
    },
}

STATUS_DETAILS = {
    'REQUIRES_FUNDING_CONFIRMATION': 'Manual confirmation is required before fund deduction is scheduled.',
    'SCHEDULED': 'Funding is scheduled for the transfer date.',
    'PROCESSING': 'Airwallex would be processing the funding instruction.',
    'FUNDED': 'The transfer has been successfully funded; this does not mean the beneficiary was paid.',
    'FAILED': 'Direct-debit funding was rejected by Airwallex or the banking scheme.',
    'REVERSED': 'A settled direct-debit deposit was subsequently recalled by the external bank.',
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _event(status: str, sequence: int) -> dict[str, Any]:
    return {
        'sequence': sequence,
        'recorded_at': _timestamp(),
        'event_name': 'payment.funding.' + status.lower(),
        'funding_status': status,
        'source': SOURCE,
    }


def _require_state(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get('source') != SOURCE:
        raise ValueError('Start a local funding simulation first')
    if state.get('api_version') != API_VERSION:
        raise ValueError('Funding simulation supports API version 2024-04-30 only')
    scenario = state.get('scenario')
    if scenario not in SCENARIOS:
        raise ValueError('Unknown local funding scenario')
    if state.get('status') not in SCENARIOS[scenario]['transitions']:
        raise ValueError('Invalid funding status for this scenario')
    return state


def start(scenario: str) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError('Choose a supported local funding scenario')
    status = str(SCENARIOS[scenario]['initial_status'])
    return {
        'simulation_id': 'funding-local-' + uuid.uuid4().hex,
        'api_version': API_VERSION,
        'scenario': scenario,
        'source': SOURCE,
        'live_api_called': False,
        'status': status,
        'history': [_event(status, 1)],
    }


def allowed_next(state: dict[str, Any]) -> list[str]:
    current = _require_state(state)
    scenario = str(current['scenario'])
    status = str(current['status'])
    return list(SCENARIOS[scenario]['transitions'][status])


def advance(state: dict[str, Any], next_status: str) -> dict[str, Any]:
    current = _require_state(state)
    allowed = allowed_next(current)
    if next_status not in allowed:
        choices = ', '.join(allowed) if allowed else 'none (terminal state)'
        raise ValueError(f'Invalid funding transition from {current["status"]}; allowed: {choices}')
    updated = copy.deepcopy(current)
    updated['status'] = next_status
    updated['history'].append(_event(next_status, len(updated['history']) + 1))
    return updated


def snapshot(state: dict[str, Any]) -> dict[str, Any]:
    current = _require_state(state)
    return {
        'simulation_id': current['simulation_id'],
        'api_version': current['api_version'],
        'scenario': current['scenario'],
        'funding_status': current['status'],
        'source': current['source'],
        'live_api_called': False,
    }


def explanation(state: dict[str, Any]) -> str:
    current = _require_state(state)
    status = str(current['status'])
    upcoming = allowed_next(current)
    next_text = ', '.join(f'`{item}`' for item in upcoming) if upcoming else 'none; this is a terminal state'
    return (
        f'**LOCAL fixture — not an Airwallex response.** Current funding state: `{status}`. '
        f'{STATUS_DETAILS[status]} Next valid state(s): {next_text}.'
    )
