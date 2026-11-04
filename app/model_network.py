"""Model-only connection policy. Market/material clients do not use this module.

HTTPX 0.28 treats proxy and certificate environment as one trust_env flag. A
direct client therefore gets an explicit verified SSLContext before disabling
proxy environment lookup. No system environment or network setting is changed.
"""
from __future__ import annotations

import os
import ssl

import certifi


def network_policy(config: dict) -> dict:
    route = config.get('model_network_route', 'environment')
    if route not in {'environment', 'direct'}:
        raise ValueError('model_network_route 必须为 environment 或 direct')
    return {'route': route, 'revision': config.get('model_network_revision', 0)}


def network_observation(config: dict, *, custom_transport=False) -> dict:
    """Configuration is not evidence of an actually observed physical path."""
    return {**network_policy(config),
            'transport': 'injected' if custom_transport else 'httpx',
            'environment_proxy_present': any(os.environ.get(key) for key in
                ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy')),
            'environment_no_proxy_present': bool(os.environ.get('NO_PROXY') or os.environ.get('no_proxy')),
            'ca_source': 'transport_controlled' if custom_transport else
                         'SSL_CERT_FILE' if os.environ.get('SSL_CERT_FILE') else
                         'SSL_CERT_DIR' if os.environ.get('SSL_CERT_DIR') else 'certifi',
            'certificate_verification': None if custom_transport else True,
            'proxy_connect_observed': False,
            'physical_route': 'not_observable'}


def model_http_options(config: dict, transport=None) -> dict:
    route = network_policy(config)['route']
    if route == 'environment':
        return {'trust_env': True, 'transport': transport}
    # Match HTTPX's documented SSL_CERT_FILE > SSL_CERT_DIR > certifi order,
    # without using the private httpx._config API or disabling TLS validation.
    if os.environ.get('SSL_CERT_FILE'):
        context = ssl.create_default_context(cafile=os.environ['SSL_CERT_FILE'])
    elif os.environ.get('SSL_CERT_DIR'):
        context = ssl.create_default_context(capath=os.environ['SSL_CERT_DIR'])
    else:
        context = ssl.create_default_context(cafile=certifi.where())
    return {'trust_env': False, 'verify': context, 'transport': transport}
