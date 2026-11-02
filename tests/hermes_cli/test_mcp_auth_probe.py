"""Offline, pinned-SDK regressions for explicit protected login probes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from mcp.types import CallToolResult
import pytest


def test_probe_calls_only_declared_read_tool(monkeypatch):
    from hermes_cli import mcp_config as mc
    seen = {}
    session = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(content=[], is_error=False)))
    server = SimpleNamespace(_auth_probe_authenticated=True, _auth_probe_challenged=True, _auth_probe_effective_url='https://example.invalid/mcp', _tools=[SimpleNamespace(name='read', description='', annotations=SimpleNamespace(read_only_hint=True))], session=session, shutdown=AsyncMock())
    async def connect(name, config):
        seen.update(config)
        return server
    monkeypatch.setattr('tools.mcp_tool_discovery._connect_server', connect)
    assert mc._probe_single_server('srv', {'url': 'https://example.invalid/mcp', 'auth': 'oauth'}, connect_timeout=2,
                                   auth_probe={'tool': 'read', 'arguments': {'key': 'value'}, 'origin': 'https://example.invalid'}) == [('read', '')]
    assert session.call_tool.await_count == 2
    session.call_tool.assert_awaited_with('read', {'key': 'value'})
    assert seen['_auth_probe_no_redirect'] is True
    assert server.shutdown.await_count >= 1


def test_probe_refuses_tool_without_read_only_hint(monkeypatch):
    from hermes_cli import mcp_config as mc
    session = SimpleNamespace(call_tool=AsyncMock())
    server = SimpleNamespace(_auth_probe_authenticated=True, _auth_probe_challenged=True, _auth_probe_effective_url='https://example.invalid/mcp', _tools=[SimpleNamespace(name='write', description='', annotations=SimpleNamespace(read_only_hint=False))], session=session, shutdown=AsyncMock())
    async def connect(name, config): return server
    monkeypatch.setattr('tools.mcp_tool_discovery._connect_server', connect)
    with pytest.raises(ValueError, match='readOnlyHint'):
        mc._probe_single_server('srv', {'url': 'https://example.invalid/mcp', 'auth': 'oauth'}, connect_timeout=2,
                                auth_probe={'tool': 'write', 'arguments': {}, 'origin': 'https://example.invalid'})
    session.call_tool.assert_not_awaited()
    assert server.shutdown.await_count >= 1


def test_failed_probe_does_not_mark_dashboard_initialized(monkeypatch):
    from hermes_cli import mcp_config as mc
    session = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(content=[], is_error=True)))
    server = SimpleNamespace(_auth_probe_authenticated=True, _auth_probe_challenged=True, _auth_probe_effective_url='https://example.invalid/mcp', _tools=[SimpleNamespace(name='read', description='', annotations=SimpleNamespace(read_only_hint=True))], session=session, shutdown=AsyncMock())
    async def connect(name, config): return server
    monkeypatch.setattr('tools.mcp_tool_discovery._connect_server', connect)
    details = {}
    with pytest.raises(RuntimeError, match='protected read failed'):
        mc._probe_single_server('srv', {'url': 'https://example.invalid/mcp', 'auth': 'oauth'}, connect_timeout=2, details=details,
                                auth_probe={'tool': 'read', 'arguments': {}, 'origin': 'https://example.invalid'})
    assert details['initialized'] is False


@pytest.mark.asyncio
async def test_native_http_records_authenticated_probe_without_following_redirects(monkeypatch):
    from contextlib import asynccontextmanager
    from tools import mcp_tool as core
    from tools import mcp_tool_transport as transport
    httpx = core.sdk_httpx()
    captured = {}
    class Client:
        def __init__(self, **kwargs): captured.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    @asynccontextmanager
    async def streams(*args, **kwargs):
        yield (None, None, None)
    monkeypatch.setattr(transport, '_make_redirect_header_stripper', lambda *args, **kwargs: Client)
    monkeypatch.setattr(core, 'streamable_http_client', streams)
    server = core.MCPServerTask('fixture')
    server._http_rejection = {}
    spec = {'tool': 'read', 'arguments': {'key': 'value'}, 'origin': 'https://example.invalid'}
    async with server._streamable_http_transport('https://example.invalid/mcp', {}, 2, True, None, None,
                                                  False, set(), True, spec):
        assert captured['follow_redirects'] is False
        hook = captured['event_hooks']['response'][-1]
        request = httpx.Request('POST', 'https://example.invalid/mcp', headers={'Authorization': 'Bearer fixture'},
                                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                      'params': {'name': 'read', 'arguments': {'key': 'value'}}})
        await hook(httpx.Response(200, request=request))
        assert server._auth_probe_authenticated is True


def test_probe_rejects_plaintext_and_unapproved_origins():
    from hermes_cli import mcp_config as mc
    spec = {'tool': 'read', 'arguments': {}, 'origin': 'https://approved.invalid'}
    for url in ('http://approved.invalid/mcp', 'https://other.invalid/mcp', 'https://user@approved.invalid/mcp'):
        with pytest.raises(ValueError, match='approved HTTPS origin'):
            mc._probe_single_server('srv', {'url': url, 'auth': 'oauth'}, auth_probe=spec)


def test_probe_requires_authenticated_response(monkeypatch):
    from hermes_cli import mcp_config as mc
    session = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(content=[], is_error=False)))
    server = SimpleNamespace(_auth_probe_authenticated=False, _auth_probe_challenged=True, _auth_probe_effective_url='https://example.invalid/mcp', _tools=[SimpleNamespace(name='read', description='', annotations=SimpleNamespace(read_only_hint=True))], session=session, shutdown=AsyncMock())
    async def connect(name, config): return server
    monkeypatch.setattr('tools.mcp_tool_discovery._connect_server', connect)
    with pytest.raises(RuntimeError, match='authenticated transport evidence'):
        mc._probe_single_server('srv', {'url': 'https://example.invalid/mcp', 'auth': 'oauth'}, connect_timeout=2,
                                auth_probe={'tool': 'read', 'arguments': {}, 'origin': 'https://example.invalid'})


def test_probe_reconnect_does_not_reload_config(monkeypatch):
    from tools import mcp_tool as core
    server = core.MCPServerTask('fixture')
    server._auth_type = 'oauth'
    monkeypatch.setattr(server, '_is_http', lambda: True)
    config = {'url': 'https://example.invalid/mcp', '_auth_probe_no_redirect': True}
    monkeypatch.setattr('tools.mcp_tool_config._load_mcp_config', lambda: (_ for _ in ()).throw(AssertionError('reloaded')))
    assert server._refresh_remote_config(config) is config


def test_probe_rejects_unverified_tls():
    from hermes_cli import mcp_config as mc
    with pytest.raises(ValueError, match='verified TLS'):
        mc._probe_single_server('srv', {'url': 'https://example.invalid/mcp', 'auth': 'oauth', 'ssl_verify': False},
                                auth_probe={'tool': 'read', 'arguments': {}, 'origin': 'https://example.invalid'})


def test_anonymous_public_read_is_not_authenticated(monkeypatch):
    from hermes_cli import mcp_config as mc
    session = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(content=[], is_error=False)))
    server = SimpleNamespace(_auth_probe_challenged=False, _auth_probe_effective_url='https://example.invalid/mcp',
                             _tools=[SimpleNamespace(name='read', description='', annotations=SimpleNamespace(read_only_hint=True))],
                             session=session, shutdown=AsyncMock())
    async def connect(name, config): return server
    monkeypatch.setattr('tools.mcp_tool_discovery._connect_server', connect)
    with pytest.raises(RuntimeError, match='anonymous control'):
        mc._probe_single_server('srv', {'url': 'https://example.invalid/mcp', 'auth': 'oauth'}, connect_timeout=2,
                                auth_probe={'tool': 'read', 'arguments': {}, 'origin': 'https://example.invalid'})
    assert session.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_native_adapter_does_not_follow_redirect(monkeypatch):
    from contextlib import asynccontextmanager
    from tools import mcp_tool as core
    from tools import mcp_tool_transport as transport
    httpx = core.sdk_httpx()
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={'Location': 'https://other.invalid/mcp'}, request=request)
    mock_transport = httpx.MockTransport(handler)
    monkeypatch.setattr(transport, '_make_mcp_body_cap_transport', lambda *args: mock_transport)
    monkeypatch.setattr(transport, '_mcp_proxy_mounts', lambda *args: None)
    @asynccontextmanager
    async def streams(url, http_client, **kwargs):
        response = await http_client.post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        assert response.status_code == 307
        yield (None, None, None)
    monkeypatch.setattr(core, 'streamable_http_client', streams)
    server = core.MCPServerTask('fixture')
    server._http_rejection = {}
    spec = {'tool': 'read', 'arguments': {}, 'origin': 'https://example.invalid'}
    async with server._streamable_http_transport('https://example.invalid/mcp', {}, 2, True, None, None,
                                                  False, set(), True, spec):
        pass
    assert seen == ['https://example.invalid/mcp']


@pytest.mark.asyncio
async def test_changed_effective_endpoint_is_rejected_before_oauth(monkeypatch):
    from tools import mcp_tool as core
    from tools import mcp_tool_transport as transport
    monkeypatch.setattr(core, '_ensure_mcp_sdk', lambda: None)
    monkeypatch.setattr(core, '_MCP_HTTP_AVAILABLE', True)
    monkeypatch.setattr(transport, '_connect_inputs', lambda *args: (('https://example.invalid/changed', {}), set()))
    server = core.MCPServerTask('fixture')
    build_oauth = AsyncMock(side_effect=AssertionError('credentials used'))
    monkeypatch.setattr(server, '_build_oauth_auth', build_oauth)
    config = {'url': 'https://example.invalid/mcp', '_auth_probe_no_redirect': True,
              '_auth_probe_expected_url': 'https://example.invalid/mcp',
              '_auth_probe_spec': {'tool': 'read', 'arguments': {}, 'origin': 'https://example.invalid'}}
    with pytest.raises(ValueError, match='changed after anonymous'):
        await server._run_http(config)
    build_oauth.assert_not_called()


@pytest.mark.parametrize("case", ["dcr", "cimd", "denied", "pending", "expired", "manager"])
def test_native_session_discovery_then_401_then_dcr_read(monkeypatch, tmp_path, case):
    """Exercise MCP session, native HTTP hooks and SDK authorization with no network."""
    from urllib.parse import parse_qs, urlsplit
    from pydantic import AnyUrl
    from mcp.shared.auth import OAuthClientMetadata
    from hermes_cli import mcp_config as mc
    from tools import mcp_tool as core, mcp_tool_transport as transport
    from tools.mcp_oauth import HermesTokenStorage, _authorization_code_result
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS
    httpx = core.sdk_httpx()
    target = 'https://resource.example.invalid/mcp'
    issuer = 'https://issuer.example.invalid'
    prm = 'https://resource.example.invalid/.well-known/oauth-protected-resource/mcp'
    seen = {'requests': [], 'state': None}
    def handler(request):
        import json
        url = str(request.url)
        seen['requests'].append((request.method, url))
        def reply(status, data=None, **headers):
            return httpx.Response(status, json=data, headers=headers, request=request)
        if url == prm:
            return reply(200, {'resource': target, 'authorization_servers': [issuer]})
        if url.startswith(issuer + '/.well-known/'):
            return reply(200, {'issuer': issuer, 'authorization_endpoint': issuer + '/authorize',
                               'token_endpoint': issuer + '/token', 'registration_endpoint': issuer + '/register',
                               'response_types_supported': ['code'], 'code_challenge_methods_supported': ['S256'],
                               'client_id_metadata_document_supported': True})
        if url == issuer + '/register':
            return reply(201, {'client_id': 'fixture-client', 'redirect_uris': ['http://127.0.0.1:1/cb'],
                               'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code'],
                               'response_types': ['code']})
        if url == issuer + '/token':
            return reply(200, {'access_token': 'fixture-access', 'token_type': 'Bearer', 'expires_in': 3600})
        if url != target:
            return reply(404, {})
        if request.method == 'GET':
            return reply(405, {})
        message = json.loads(request.content)
        method = message.get('method')
        if method == 'notifications/initialized':
            return reply(202, None)
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'read', 'description': '', 'inputSchema': {'type': 'object'},
                                 'annotations': {'readOnlyHint': True}}]}
        elif method == 'tools/call':
            if request.headers.get('authorization') != 'Bearer fixture-access':
                return reply(401, {}, **{'WWW-Authenticate': f'Bearer resource_metadata="{prm}"'})
            result = {'content': [{'type': 'text', 'text': 'ok'}], 'isError': False}
        else:
            return reply(404, {})
        return reply(200, {'jsonrpc': '2.0', 'id': message['id'], 'result': result},
                     **{'Content-Type': 'application/json'})
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    async def redirect(url): seen['state'] = parse_qs(urlsplit(url).query)['state'][0]
    async def callback():
        if case == 'denied':
            raise RuntimeError('consent denied')
        if case == 'pending':
            import asyncio
            await asyncio.sleep(30)
        return _authorization_code_result('fixture-code', seen['state'])
    storage = HermesTokenStorage('fixture')
    if case == 'expired':
        import asyncio
        from mcp.shared.auth import OAuthToken
        asyncio.run(storage.set_tokens(OAuthToken(access_token='expired-fixture', token_type='Bearer', expires_in=0)))
    provider = _HERMES_PROVIDER_CLS(
        server_name='fixture', server_url=target, storage=storage,
        client_metadata=OAuthClientMetadata(redirect_uris=[AnyUrl('http://127.0.0.1:1/cb')], client_name='fixture'),
        redirect_handler=redirect, callback_handler=callback,
        client_metadata_url='https://client.example.invalid/hermes.json' if case == 'cimd' else None)
    if case == 'expired':
        async def no_prefetch(): pass
        monkeypatch.setattr(provider, '_prefetch_oauth_metadata', no_prefetch)
    monkeypatch.setattr(transport, '_make_mcp_body_cap_transport', lambda *args: httpx.MockTransport(handler))
    monkeypatch.setattr(transport, '_mcp_proxy_mounts', lambda *args: None)
    if case == 'manager':
        import tools.mcp_oauth as oauth
        from tools.mcp_oauth_manager import reset_manager_for_tests
        reset_manager_for_tests()
        monkeypatch.setattr(oauth, '_is_interactive', lambda: True)
        def configure_port(cfg, storage):
            cfg['_resolved_port'] = 1
            return 1
        monkeypatch.setattr(oauth, '_configure_callback_port', configure_port)
        monkeypatch.setattr(oauth, '_make_redirect_handler', lambda *args, **kwargs: redirect)
        monkeypatch.setattr(oauth, '_make_callback_waiter', lambda *args, **kwargs: callback)
    else:
        monkeypatch.setattr(core.MCPServerTask, '_build_oauth_auth', lambda self, url, cfg: provider if self._auth_type == 'oauth' else None)
    probe = {'tool': 'read', 'arguments': {}, 'origin': 'https://resource.example.invalid'}
    if case in ('denied', 'pending'):
        with pytest.raises(Exception):
            mc._probe_single_server('fixture', {'url': target, 'auth': 'oauth'}, connect_timeout=1, auth_probe=probe)
        assert ('POST', issuer + '/token') not in seen['requests']
    else:
        assert mc._probe_single_server('fixture', {'url': target, 'auth': 'oauth'}, connect_timeout=10,
                                       auth_probe=probe) == [('read', '')]
        assert (('POST', issuer + '/register') in seen['requests']) == (case in ('dcr', 'expired', 'manager'))
        assert seen['requests'].count(('POST', issuer + '/token')) == 1


def test_anonymous_control_strips_server_json_bearer(monkeypatch):
    from tools import mcp_tool_transport as transport
    monkeypatch.setattr(transport, '_live_endpoint', lambda name: ('https://example.invalid/live', {'Authorization': 'Bearer fixture'}))
    inputs, names = transport._connect_inputs('fixture', {'url': 'https://example.invalid/config', '_auth_probe_anonymous': True})
    assert inputs == ['https://example.invalid/live', {}]
    assert not names


def test_device_login_keeps_existing_device_flow(monkeypatch, tmp_path):
    from hermes_cli import mcp_config as mc
    import tools.mcp_oauth_device as device
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    login = AsyncMock()
    monkeypatch.setattr(device, 'login_device', login)
    monkeypatch.setattr(mc, '_oauth_tokens_present', lambda name: True)
    probe = {'tool': 'read', 'arguments': {}, 'origin': 'https://example.invalid'}
    calls = []
    def fake_probe(name, cfg, **kwargs):
        calls.append((cfg, kwargs))
        return [('read', '')]
    monkeypatch.setattr(mc, '_probe_single_server', fake_probe)
    assert mc._reauth_oauth_server('fixture', {'url': 'https://example.invalid/mcp', 'auth': 'oauth',
                                               'oauth': {'flow': 'device', 'auth_probe': probe}})
    login.assert_awaited_once()
    assert calls[0][0]['oauth']['flow'] == 'device'
    assert calls[0][1]['auth_probe'] == probe


@pytest.mark.asyncio
async def test_probe_rejects_live_bearer_before_oauth(monkeypatch):
    from tools import mcp_tool as core
    from tools import mcp_tool_transport as transport
    monkeypatch.setattr(core, '_ensure_mcp_sdk', lambda: None)
    monkeypatch.setattr(core, '_MCP_HTTP_AVAILABLE', True)
    monkeypatch.setattr(transport, '_connect_inputs', lambda *args: (('https://example.invalid/mcp', {'Authorization': 'Bearer fixture'}), {'authorization'}))
    server = core.MCPServerTask('fixture')
    build_oauth = AsyncMock(side_effect=AssertionError('credentials used'))
    monkeypatch.setattr(server, '_build_oauth_auth', build_oauth)
    config = {'url': 'https://example.invalid/mcp', '_auth_probe_no_redirect': True,
              '_auth_probe_spec': {'tool': 'read', 'arguments': {}, 'origin': 'https://example.invalid'}}
    with pytest.raises(ValueError, match='externally supplied request headers'):
        await server._run_http(config)
    build_oauth.assert_not_called()


@pytest.mark.parametrize('failed', [False, True])
def test_tui_and_card_worker_uses_probe_and_rolls_back_failed_consent(monkeypatch, tmp_path, failed):
    from unittest.mock import MagicMock
    from hermes_cli import mcp_config as mc
    from tools.connectors import mcp_oauth as worker
    import tools.mcp_oauth as oauth
    import tools.mcp_oauth_manager as manager_module
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    storage = MagicMock()
    manager = MagicMock()
    monkeypatch.setattr(oauth, 'HermesTokenStorage', lambda name: storage)
    monkeypatch.setattr(manager_module, 'get_manager', lambda: manager)
    monkeypatch.setattr(mc, '_oauth_tokens_present', lambda name: True)
    probe = {'tool': 'read', 'arguments': {}, 'origin': 'https://example.invalid'}
    seen = []
    def fake_probe(name, cfg, *, details, auth_probe, **kwargs):
        seen.append(auth_probe)
        if failed:
            raise RuntimeError('consent denied')
        details['initialized'] = True
        return [('read', '')]
    monkeypatch.setattr(mc, '_probe_single_server', fake_probe)
    commit = MagicMock()
    monkeypatch.setattr(worker, '_commit', commit)
    flow = MagicMock()
    flow.inherited_backup = None
    config = {'url': 'https://example.invalid/mcp', 'auth': 'oauth', 'oauth': {'auth_probe': probe}}
    if failed:
        with pytest.raises(RuntimeError, match='consent denied'):
            worker.probe_with_rollback('fixture', config, str(tmp_path), flow, False)
        storage.restore.assert_called_once()
        flow.mark_approved.assert_not_called()
        commit.assert_not_called()
    else:
        worker.probe_with_rollback('fixture', config, str(tmp_path), flow, False)
        commit.assert_called_once()
        flow.mark_approved.assert_called_once()
    assert seen == [probe]
