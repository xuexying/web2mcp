import pytest
from test_v2 import fixture, contract
from web2mcp.contracts import ContractEngine, install_contract
from web2mcp.client import CallError

def private(c):
    c.manifest.update(sensitive_responses=True,contract_privacy='metadata_only')
    c.audit.sensitive=True
    spec=contract();spec['verify_on_status']=True
    install_contract(c,spec,reviewed=True)
    return ContractEngine(c)

def test_bodies_stay_in_memory_and_status_after_restart_is_honest(fixture):
    c,s,*_=fixture;e=private(c)
    p=e.prepare('purchase_approve',{'id':'101','note':'PRIVATE_SENTINEL_DO_NOT_PERSIST','budget_code':'PRIVATE_BUDGET'})
    e.submit('purchase_approve',p['preview_id'])
    assert e.status('purchase_approve',p['preview_id'])['readback_verified']
    for f in c.root.rglob('*.json*'):
        assert 'PRIVATE_SENTINEL_DO_NOT_PERSIST' not in f.read_text()
        assert 'PRIVATE_BUDGET' not in f.read_text()
    restarted=ContractEngine(c)
    assert restarted.status('purchase_approve',p['preview_id'])['state']=='needs_check'
    with pytest.raises(CallError):restarted.submit('purchase_approve',p['preview_id'])
    assert s['writes']==1

def test_unknown_keeps_durable_duplicate_guard_without_body(fixture):
    c,s,*_=fixture;e=private(c)
    args={'id':'101','note':'PRIVATE_UNKNOWN_SENTINEL','budget_code':'42'}
    p=e.prepare('purchase_approve',args);s['mode']='timeout'
    with pytest.raises(CallError):e.submit('purchase_approve',p['preview_id'])
    fresh=ContractEngine(c);q=fresh.prepare('purchase_approve',args)
    with pytest.raises(CallError):fresh.submit('purchase_approve',q['preview_id'])
    assert s['writes']==1
    for f in c.root.rglob('*.json*'):assert 'PRIVATE_UNKNOWN_SENTINEL' not in f.read_text()
