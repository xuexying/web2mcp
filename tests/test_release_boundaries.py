import pytest
from test_v2 import fixture
from web2mcp.client import CallError


@pytest.mark.parametrize('option', ['headers_from_body','body_clock_fields'])
def test_custom_request_transforms_need_an_adapter(fixture, option):
    client,state,*_=fixture
    client.operations['decide'][option]={'x-custom':'/id'} if option=='headers_from_body' else ['custom_time']
    with pytest.raises(CallError,match='custom adapter'):
        client.call('decide',{'path':{'record_id':'101'},'body':{'note':'test'}},confirm=True)
    assert state['calls']==0


def test_unknown_auth_cannot_silently_send_without_credentials(fixture):
    client,state,*_=fixture
    ref=client.operations['me']['credential_ref']
    client.credentials['auth'][ref]={'type':'unknown_protocol'}
    with pytest.raises(CallError,match='custom adapter'):client.call('me')
    assert state['calls']==0
