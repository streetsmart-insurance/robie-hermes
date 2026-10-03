"""CPython audit regressions; all transport and DNS tests are network-free."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from robie_job_engine import safety_seal as seal

ROOT = Path(__file__).resolve().parents[1]


class SafetySealAuditTests(unittest.TestCase):
    def child(self, script: str) -> None:
        env = dict(os.environ, PYTHONPATH=str(ROOT), ROBIE_ENV="TEST")
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=env,
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_actual_audit_shapes_block_named_hosts(self) -> None:
        for host in ("ezlynx.com", "app.ezlynx.com", "API.UATEZLYNX.COM.",
                     b"app.ezlynx.com"):
            for event, args in (
                ("socket.connect", (object(), (host, 443))),
                ("socket.getaddrinfo", (host, 443, 0, 0, 0)),
                ("socket.gethostbyname", (host,)),
                ("http.client.connect", (object(), host, 443)),
            ):
                with self.subTest(host=host, event=event), patch.dict(seal._STATE, installed=True):
                    with self.assertRaises(seal.SafetySealError):
                        seal._audit_hook(event, args)

    def test_local_browser_and_unattributed_destinations_remain_permitted(self) -> None:
        # IP-only connects cannot be attributed to EZLynx by this hostname guard.
        for host in ("127.0.0.1", "localhost", "::1", "192.0.2.1",
                     "example.test", "ezlynx.com.example.test", "notezlynx.com"):
            with self.subTest(host=host), patch.dict(seal._STATE, installed=True):
                seal._audit_hook("socket.connect", (object(), (host, 9222)))
                seal._audit_hook("socket.getaddrinfo", (host, 9222, 0, 0, 0))
                seal._audit_hook("http.client.connect", (object(), host, 9222))
        with patch.dict(seal._STATE, installed=True):
            seal._audit_hook("socket.connect", (object(), ("::1", 9222, 0, 0)))
            seal._audit_hook("socket.connect", (object(), "/tmp/browser.sock"))

    def test_inactive_seal_and_unrelated_or_incomplete_events_are_noops(self) -> None:
        with patch.dict(seal._STATE, installed=False):
            seal._audit_hook("socket.connect", (object(), ("app.ezlynx.com", 443)))
            seal._audit_hook("socket.getaddrinfo", ("app.ezlynx.com", 443, 0, 0, 0))
        with patch.dict(seal._STATE, installed=True):
            for event in ("socket.connect", "http.client.connect", "socket.getaddrinfo",
                          "socket.gethostbyname", "unrelated.event"):
                seal._audit_hook(event, ())
            seal._audit_hook("socket.connect", (object(),))
            seal._audit_hook("unrelated.event", ("app.ezlynx.com",))

    def test_real_socket_and_resolver_events_block_before_network(self) -> None:
        self.child('''
import socket, sys
from robie_job_engine.safety_seal import install_agent_seal, SafetySealError
install_agent_seal()
# Installed after the seal: if the seal misses, fail before any DNS/connect.
def no_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname'}:
        if event == 'socket.connect':
            assert isinstance(args[0], socket.socket), args
            assert args[1] == ('192.0.2.1', 443), args
        raise AssertionError('seal missed actual audit event: ' + event)
sys.addaudithook(no_network)
calls = [lambda: socket.getaddrinfo('app.ezlynx.com', 443),
         lambda: socket.gethostbyname('app.ezlynx.com')]
for call in calls:
    try: call()
    except SafetySealError: pass
    else: raise AssertionError('named EZLynx operation was allowed')
with socket.socket() as sock:
    try: sock.connect(('192.0.2.1', 443))
    except AssertionError as exc:
        assert 'socket.connect' in str(exc), exc
    else: raise AssertionError('network tripwire was not reached')
''')

    def test_create_connection_blocks_before_dns_returns_resolved_ip(self) -> None:
        self.child('''
import socket, sys
from unittest.mock import patch
from robie_job_engine.safety_seal import install_agent_seal, SafetySealError
calls = []
def fake_dns(host, port, *args, **kwargs):
    sys.audit('socket.getaddrinfo', host, port, 0, 0, 0)
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.1', port))]
class FakeSocket:
    def __init__(self, *args): pass
    def settimeout(self, *args): pass
    def connect(self, address): calls.append(address)
    def close(self): pass
install_agent_seal()
with patch.object(socket, 'getaddrinfo', fake_dns), patch.object(socket, 'socket', FakeSocket):
    try: socket.create_connection(('app.ezlynx.com', 443), timeout=1)
    except SafetySealError: pass
    else: raise AssertionError('named host reached resolved-IP transport')
assert not calls, calls
''')

    def test_urllib_http_and_https_block_before_transport(self) -> None:
        self.child('''
import socket, urllib.request
from unittest.mock import patch
from robie_job_engine.safety_seal import install_agent_seal, SafetySealError
calls = []
def fake_transport(address, *args, **kwargs):
    calls.append(address)
    raise AssertionError('transport reached; no real network call')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
install_agent_seal()
with patch.object(socket, 'create_connection', fake_transport):
    for scheme in ('http', 'https'):
        try: opener.open(scheme + '://app.ezlynx.com/network-free-test', timeout=1)
        except SafetySealError: pass
        else: raise AssertionError('urllib named host not blocked')
assert not calls, calls
''')

    def test_urllib_local_cdp_route_reaches_mock_transport(self) -> None:
        self.child('''
import socket, urllib.request
from unittest.mock import patch
from robie_job_engine.safety_seal import install_agent_seal
class MockTransportReached(Exception): pass
calls = []
def fake_transport(address, *args, **kwargs):
    calls.append(address)
    raise MockTransportReached()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
install_agent_seal()
with patch.object(socket, 'create_connection', fake_transport):
    try: opener.open('http://127.0.0.1:9222/json/version', timeout=1)
    except MockTransportReached: pass
    else: raise AssertionError('mock transport not reached')
assert calls == [('127.0.0.1', 9222)], calls
''')


if __name__ == "__main__":
    unittest.main()
