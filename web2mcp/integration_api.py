"""Authenticated local HTTP job API. The generated product MCP remains stdio."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from .autopilot import IntegrationJobs
from .client import CallError
from .planner import ChatCompletionsPlanner
from .storage import load_json, save_private

class IntegrationAPI:
    def __init__(self, jobs, planner_factory, token, *, executor=None):
        self.jobs=jobs;self.planner_factory=planner_factory;self.token=token
        self.pool=executor or ThreadPoolExecutor(max_workers=2,thread_name_prefix='web2mcp')
        self.futures=[]
    def schedule(self, ident):
        def work():
            try:return self.jobs.run(ident,self.planner_factory())
            except Exception:
                # Keep raw provider/website traces out of the public API.
                job=self.jobs.load(ident)
                if job['status'] in ('queued','running'):
                    self.jobs.ask(job,'worker_interrupted','执行进程异常停止，请核对状态后恢复。未确定的写入不会自动重放。')
        future=self.pool.submit(work)
        self.futures=[f for f in self.futures if not f.done()]+[future]
    def recover(self):
        for path in self.jobs.root.glob('*/job.json'):
            job=load_json(path)
            if job['status'] in ('queued','running'):self.schedule(job['id'])
    def dispatch(self, method, path, body=None, *, authorization='', origin=None):
        if origin is not None:return 403,{'error':'Cross-origin browser requests are not accepted'}
        if not secrets.compare_digest(authorization,'Bearer '+self.token):return 401,{'error':'Local API authentication required'}
        parts=urlsplit(path).path.strip('/').split('/');body={} if body is None else body
        if not isinstance(body,dict):return 400,{'error':'Expected a JSON object'}
        try:
            if method=='GET' and parts==['health']:
                return 200,{'service':'web2mcp-autopilot','version':'0.4.0','model_configured':'checked_when_job_runs'}
            if method=='POST' and parts==['v1','integrations']:
                job=self.jobs.create(body);self.schedule(job['id']);return 202,job
            if len(parts)>=3 and parts[:2]==['v1','integrations']:
                ident=parts[2]
                if len(parts)==3 and method=='GET':return 200,self.jobs.get(ident)
                if len(parts)==4 and parts[3]=='responses' and method=='POST':
                    if set(body)-{'question_id','answer','approved','login_completed'}:raise CallError('Unknown response fields')
                    if not isinstance(body.get('question_id'),str) or not isinstance(body.get('answer'),str):raise CallError('question_id and answer are required')
                    for key in ('approved','login_completed'):
                        if key in body and type(body[key]) is not bool:raise CallError(key+' must be boolean')
                    job=self.jobs.answer(ident,**body);self.schedule(ident);return 202,job
                if len(parts)==4 and parts[3]=='pause' and method=='POST':
                    if body:raise CallError('pause takes no arguments')
                    directory=self.jobs.directory(ident);save_private(directory/'pause-request.json',{'requested':True})
                    return 202,{'id':ident,'pause_requested':True,'takes_effect':'after_current_step'}
                if len(parts)==4 and parts[3]=='resume' and method=='POST':
                    job=self.jobs.resume(ident,extra_steps=body.get('extra_steps',0));self.schedule(ident);return 202,job
                if len(parts)==4 and parts[3]=='mcp-config' and method=='GET':
                    job=self.jobs.load(ident)
                    if job['status'] not in ('ready','partial') or not job.get('result',{}).get('mcp_config'):
                        return 409,{'error':'No validated MCP config is available yet'}
                    return 200,job['result']['mcp_config']
            return 404,{'error':'Unknown route'}
        except (CallError,ValueError,TypeError):return 400,{'error':'Invalid request or job state; check input and current job status'}
    def close(self):self.pool.shutdown(wait=True)


def make_server(api, port=8766):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def handle_request(self):
            body={}
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<=size<=2_000_000:raise ValueError()
                if size:body=json.loads(self.rfile.read(size))
                status,value=api.dispatch(self.command,self.path,body,
                    authorization=self.headers.get('Authorization',''),origin=self.headers.get('Origin'))
            except (ValueError,UnicodeError):status,value=400,{'error':'Invalid or oversized JSON body'}
            encoded=json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(encoded)));self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(encoded)
        do_GET=handle_request;do_POST=handle_request
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def serve(root, model_config, port=8766):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    token_path=root/'api-token.json'
    if not token_path.exists():save_private(token_path,{'token':secrets.token_urlsafe(32)})
    token=load_json(token_path)['token']
    def planner_factory():
        config=load_json(model_config) if Path(model_config).exists() else {}
        return ChatCompletionsPlanner(config)
    api=IntegrationAPI(IntegrationJobs(root/'jobs'),planner_factory,token)
    server=make_server(api,port);api.recover()
    print(json.dumps({'listening':'http://127.0.0.1:'+str(server.server_port),
        'api_token_file':str(token_path),'model_config':str(Path(model_config).resolve())}),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();api.close()
