import copy
from datetime import datetime, timedelta, timezone
import pytest
from web2mcp.client import CallError
from web2mcp.contracts import ContractEngine, install_contract, assert_checks, check_deadline
from web2mcp.expressions import bind
from test_v2 import fixture, contract

def test_merge_preserves_metadata_and_applies_reviewed_patch():
 original={'form_id':'real','x':1}
 assert bind({'$fn':'merge','args':[{'$literal':original},{'x':2}]},{},{})=={'form_id':'real','x':2}
 assert original['x']==1
 with pytest.raises(CallError):bind({'$fn':'merge','args':[{},[]]},{},{})

def test_computed_children_cannot_skip_unresolved():
 check={'actual':{'$fn':'all','args':[{'$map':{'$step':'_response','pointer':'/data/items'},'value':{'$fn':'eq','args':[{'$item':'/done'},True]}}]},'value':True}
 assert_checks({'data':{'items':[{'done':True},{'done':True}]}},[check])
 with pytest.raises(CallError):assert_checks({'data':{'items':[{'done':True},{'done':False}]}},[check])
 with pytest.raises(CallError):bind({'$fn':'all','args':[[1]]},{},{})

@pytest.mark.parametrize('value',['2020-01-01T00:00:00+08:00','2099-01-01T00:00:00','bad',None])
def test_deadline_invalid_or_past(value):
 with pytest.raises(CallError):check_deadline({'deadline':value},{'deadline_pointer':'/deadline'})

def test_deadline_checked_again_before_write(fixture):
 c,state,*_=fixture;con=contract();con['preflight'][0]['deadline_pointer']='/data/deadline'
 state['rows']['101']['deadline']=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
 install_contract(c,con,reviewed=True);engine=ContractEngine(c)
 preview=engine.prepare(con['name'],{'id':'101','note':'ok','budget_code':'A'})
 state['rows']['101']['deadline']='2020-01-01T00:00:00+08:00'
 with pytest.raises(CallError,match='deadline'):engine.submit(con['name'],preview['preview_id'])
 assert state['writes']==0


def test_odata_quoting_and_unique_lookup():
 assert bind({'$fn':'odata_quote','args':["x' or owner eq 'other"]},{},{})=="'x'' or owner eq ''other'"
 value=bind({'$fn':'object_set','args':[{'$literal':{'$limit':2}},'$filter','safe']},{},{})
 assert value=={'$limit':2,'$filter':'safe'}
 with pytest.raises(CallError):bind({'$fn':'find_one','args':[[{'code':'x'},{'code':'x'}],'code','x']},{},{})


def test_computed_expected_uses_same_response():
 assert_checks({'data':[1,2]},[{'actual':{'$fn':'length','args':[{'$step':'_response','pointer':'/data'}]},'value':2}])
 assert_checks({'n':2,'rows':[1,2]},[{'actual':{'$fn':'length','args':[{'$step':'_response','pointer':'/rows'}]},'value':{'$step':'_response','pointer':'/n'}}])

def test_shared_lock_blocks_changed_decision_after_unknown(fixture):
 c,state,*_=fixture;one=contract();one['lock_namespace']='same_action';two=copy.deepcopy(one);two['name']='alternative_approve'
 install_contract(c,one,reviewed=True);install_contract(c,two,reviewed=True);engine=ContractEngine(c)
 a={'id':'101','note':'ok','budget_code':'A'};p1=engine.prepare(one['name'],a);p2=engine.prepare(two['name'],a);state['mode']='timeout'
 with pytest.raises(CallError):engine.submit(one['name'],p1['preview_id'])
 with pytest.raises(CallError,match='Already attempted'):engine.submit(two['name'],p2['preview_id'])
 assert state['writes']==1
