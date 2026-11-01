import httpx
import pytest
from web2mcp.client import Client
from web2mcp.discover import discover
from web2mcp.storage import save_private, load_json


def sample(body='', unavailable=None):
    return {'format':'web2mcp-capture-v1','cookies':[], 'exchanges':[
        {'request':{'method':'POST','url':'https://fixture.example/tasks/101/delay',
                    'headers':{'content-type':'application/json'},'postData':'{"days":7}'},
         'response':{'status':200,'body':body,'unavailable':unavailable}}]}


@pytest.mark.parametrize('body,unavailable,policy,expected', [
    ('',None,{},0),
    ('',None,{'allow_empty_response':True,'read_only':True},0),
    ('',None,{'allow_empty_response':True,'read_only':False},1),
    ('','Response body unavailable',{'allow_empty_response':True,'read_only':False},0),
    (None,None,{'allow_empty_response':True,'read_only':False},0),
    ('<html>Login</html>',None,{'allow_empty_response':True,'read_only':False},0),
])
def test_empty_write_requires_observation_and_review(tmp_path,body,unavailable,policy,expected):
    capture=tmp_path/'capture.json';save_private(capture,sample(body,unavailable))
    result=discover(capture,tmp_path/'profile',['https://fixture.example'],
                    {'POST /tasks/101/delay':{'name':'delay',**policy}})
    assert result['operations']==expected
    if expected:
        sent=[]
        def handle(request):
            sent.append(request);return httpx.Response(200,content=b'')
        with Client(tmp_path/'profile',transport=httpx.MockTransport(handle)) as client:
            assert client.call('delay',{})['preview'] is True
            assert not sent
            result=client.call('delay',{},confirm=True)
            assert result=={'status':200,'data':''}
            assert len(sent)==1
        response=load_json(tmp_path/'profile/openapi.json')['paths']['/tasks/101/delay']['post']['responses']['200']
        assert 'content' not in response
