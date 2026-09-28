import pytest
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAClientPermissionScope
from services.ctrader import oauth_state, tokens as token_store
from services.ctrader.prop_sim import PropSimError, complete_oauth, inventory, validate_listed_accounts
from services.demo_orders.factory import UnconfiguredDemoTransport, build_gateway
from tests.test_api import Cache, settings
from tests.test_demo_orders import demo_settings


def prop_settings(**kw):
    values = dict(
        ctrader_client_id='40796_id', ctrader_client_secret='secret',
        ctrader_account_id='48803059',
        prop_sim_ctrader_account_id='17204978',
        prop_sim_allowed_account_ids='17204978',
        prop_sim_execution_enabled=False,
        prop_sim_acknowledged_live_environment=False,
        ctrader_oauth_state_secret='x'*32,
    )
    values.update(kw)
    return settings(**values)


def listed_ok():
    return {
        'permission_scope': ProtoOAClientPermissionScope.SCOPE_TRADE,
        'accounts': [
            {'ctidTraderAccountId': 17204978, 'isLive': True, 'broker': 'FTMO'},
            {'ctidTraderAccountId': 48803059, 'isLive': True, 'broker': 'other'},
        ],
        'broker': 'FTMO',
    }


def test_callback_does_not_trust_ctrader_account_id():
    cfg = prop_settings()
    cache = Cache()
    state = oauth_state.issue(cfg, cache, purpose='prop-sim', expected_account='17204978')
    consumed = oauth_state.consume(cfg, cache, state)
    assert consumed['expected_account'] == '17204978'
    assert consumed['expected_account'] != cfg.ctrader_account_id
    public = complete_oauth(
        cfg, cache,
        {'access_token': 'prop-sim-token', 'refresh_token': 'prop-sim-refresh', 'expires_in': 3600},
        consumed, list_accounts=lambda token, host: listed_ok())
    assert public['account'] == '****4978'
    assert '48803059' not in str(public)
    market = token_store.load_token_record(cache, profile='market-data')
    prop_sim = token_store.load_token_record(cache, profile='prop-sim')
    assert market is None
    assert prop_sim['account_id'] == '17204978'
    assert prop_sim['access_token'] == 'prop-sim-token'
    assert prop_sim['execution_usable'] is False


def test_prop_sim_token_never_saved_as_forbidden_account():
    cfg = prop_settings()
    cache = Cache()
    token_store.save_market_data_tokens(cache, {'access_token': 'md-token', 'expires_in': 3600})
    state = {'purpose': 'prop-sim', 'expected_account': '17204978'}
    complete_oauth(cfg, cache, {'access_token': 'prop-sim-token', 'expires_in': 3600},
                   state, list_accounts=lambda token, host: listed_ok())
    assert token_store.load_access_token(cache) == 'md-token'
    assert token_store.load_prop_sim_access_token(cache) == 'prop-sim-token'
    assert token_store.load_token_record(cache, profile='prop-sim')['account_id'] != '48803059'


def test_expected_account_mismatch_rejected():
    cfg = prop_settings()
    cache = Cache()
    only_forbidden = {
        'permission_scope': ProtoOAClientPermissionScope.SCOPE_TRADE,
        'accounts': [{'ctidTraderAccountId': 48803059, 'isLive': True}],
    }
    with pytest.raises(PropSimError, match='TOKEN_ONLY_HAS_FORBIDDEN_ACCOUNT'):
        complete_oauth(cfg, cache, {'access_token': 'x', 'expires_in': 60},
                       {'purpose': 'prop-sim', 'expected_account': '17204978'},
                       list_accounts=lambda token, host: only_forbidden)
    assert token_store.load_prop_sim_access_token(cache) is None
    missing = {
        'permission_scope': ProtoOAClientPermissionScope.SCOPE_TRADE,
        'accounts': [{'ctidTraderAccountId': 999, 'isLive': True}],
    }
    with pytest.raises(PropSimError):
        complete_oauth(cfg, cache, {'access_token': 'x', 'expires_in': 60},
                       {'purpose': 'prop-sim', 'expected_account': '17204978'},
                       list_accounts=lambda token, host: missing)


def test_market_data_and_prop_sim_profiles_stay_separated():
    cache = Cache()
    token_store.save_market_data_tokens(cache, {'access_token': 'md', 'expires_in': 60}, scope='accounts')
    token_store.save_prop_sim_tokens(cache, {'access_token': 'ps', 'expires_in': 60},
                                     scope='trading', account_id='17204978')
    assert token_store.load_access_token(cache) == 'md'
    assert token_store.load_prop_sim_access_token(cache) == 'ps'
    status = token_store.profiles_status(cache)
    assert status['market_data']['present'] is True
    assert status['prop_sim']['account_id'] == '****4978'
    assert status['market_data']['execution_usable'] is False
    assert status['prop_sim']['execution_usable'] is False


def test_forbidden_account_cannot_execute(factory):
    with pytest.raises(Exception):
        demo_settings(demo_ctrader_account_id='48803059')
    gw = build_gateway(demo_settings(demo_execution_enabled=False))
    assert isinstance(gw.transport, UnconfiguredDemoTransport)
    with pytest.raises(Exception):
        gw.submit_market({
            'symbol': 'EURUSD', 'side': 'buy', 'volume': '1000',
            'stop_loss': '1.09', 'take_profit': '1.13', 'signal_id': 'nope',
        })


def test_oauth_html_and_inventory_never_include_tokens(factory):
    from fastapi.testclient import TestClient
    from apps.api.main import create_app, _oauth_html
    html = _oauth_html({
        'purpose': 'prop-sim', 'account': '****4978',
        'environment': 'LIVE infrastructure', 'scope': 'TRADE', 'execution': 'disabled',
    })
    assert '****4978' in html
    assert '48803059' not in html
    assert 'prop-sim' in html
    assert 'TRADE' in html
    assert 'disabled' in html
    cache = Cache()
    token_store.save_prop_sim_tokens(cache, {'access_token': 'secret-token-value', 'expires_in': 60},
                                     account_id='17204978')
    listed = listed_ok()
    report = inventory(listed, settings=prop_settings())
    blob = str(report)
    assert 'secret-token-value' not in blob
    assert '48803059' not in blob
    assert report['accounts'][0]['account'] == '****4978'
    cfg = prop_settings(ctrader_client_id='40796_id', ctrader_client_secret='secret')
    with TestClient(create_app(cfg, factory, cache)) as client:
        r = client.get('/research/ctrader/callback', params={'access_token': 'another-sandbox-token'})
        assert r.status_code == 200
        assert b'48803059' not in r.content
        assert b'market-data' in r.content
        assert b'Orders stay disabled' in r.content
    assert token_store.load_access_token(cache) == 'another-sandbox-token'
    assert token_store.load_prop_sim_access_token(cache) == 'secret-token-value'


def test_quarantine_backs_up_shared_token_without_deleting():
    cache = Cache()
    token_store.save_market_data_tokens(cache, {'access_token': 'shared-misbound', 'expires_in': 120})
    result = token_store.backup_and_quarantine_shared(cache)
    assert result['backed_up'] is True
    assert cache.get(result['backup_key'])
    assert token_store.load_access_token(cache) == 'shared-misbound'
    assert token_store.execution_quarantined(cache) is True
    record = token_store.load_token_record(cache)
    assert record['execution_usable'] is False


def test_inventory_cli_zero_orders(capsys):
    from apps.ctrader_inventory import main
    cache = Cache()
    token_store.save_market_data_tokens(cache, {'access_token': 'md', 'expires_in': 60})
    code = main(argv=[], settings=prop_settings(), redis_client=cache, listed=listed_ok())
    assert code == 0
    out = capsys.readouterr().out
    assert '****4978' in out
    assert 'isLive' in out
    assert 'TRADE' in out
    assert 'md' not in out
    assert 'prop-sim-token' not in out
