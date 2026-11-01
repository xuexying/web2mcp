// Bounded UI bridge. The host AI chooses targets from observations, never site scripts.
import fs from 'node:fs/promises';
import path from 'node:path';
import {startCapture, saveCapture} from './ego_capture.mjs';

async function write(file,value){
  await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});
  await fs.writeFile(file,JSON.stringify(value,null,2),{mode:0o600});
  await fs.chmod(file,0o600);
}
// Heavy SPAs often never fire 'load'/'domcontentloaded' (measured: not even within
// 60s), yet navigation commits, the page is usable, and XHR still flows. Wait for
// commit, swallow the settle timeout (page is ready), then allow a fixed settle
// window so API traffic lands in the capture. WEB2MCP_SETTLE_MS overrides the wait.
async function navigate(page,url){
  try{ await page.goto(url,{waitUntil:'commit',timeout:30000}); }
  catch(e){ if(!/timed out|Timeout/i.test(e.message)) throw e; }
  const settle=parseInt(process.env.WEB2MCP_SETTLE_MS||'',10);
  await new Promise(r=>setTimeout(r,Number.isFinite(settle)?settle:8000));
}
export async function run(inputPath){
  const {root,state,action,run_id}=JSON.parse(await fs.readFile(inputPath,'utf8'));
  const kind=action.kind||'observe';
  const task=kind==='resume' ? await takeOverTaskSpace(state.space_id) :
    await taskSpace(state.space_id ?? ('Web2MCP-'+state.id));
  const first=state.space_id===null;
  state.space_id=task.spaceId;
  await write(path.join(root,'session.json'),state);
  const page=task.page('p1');
  if(kind==='handoff'){
    await task.handOff();state.stage='login_required';
    await write(path.join(root,'session.json'),state);
    await write(path.join(root,'rounds',run_id+'.output.json'),{state:'login_required',space_id:task.spaceId});return;
  }
  if(kind==='finish'){
    await task.finish({keep:[]});state.stage='finished';
    await write(path.join(root,'session.json'),state);
    await write(path.join(root,'rounds',run_id+'.output.json'),{state:'finished',space_id:task.spaceId});return;
  }
  // List the JavaScript the app ships so the host can download it and statically extract the
  // API surface. We only need the entry <script src> (present in the initial HTML, no mount
  // needed) — the host then follows the build's embedded chunk references to fetch route chunks
  // deterministically. So this does NOT wait for the SPA to mount (that was flaky); a short
  // settle is enough for the script tags and any eager resources. Read-only, no clicks.
  if(kind==='source_urls'){
    await navigate(page,state.url);
    await new Promise(r=>setTimeout(r,4000));
    let urls=[];
    try{ urls=await page.evaluate(()=>{
      const perf=performance.getEntriesByType('resource').map(r=>r.name);
      const tags=[...document.querySelectorAll('script[src]')].map(s=>s.src);
      return [...new Set([...perf,...tags])].filter(u=>/\.m?js(\?|$)/i.test(u));
    }); }catch(e){}
    state.stage='observing';await write(path.join(root,'session.json'),state);
    await write(path.join(root,'rounds',run_id+'.output.json'),{state:'source_urls',js_urls:urls,space_id:task.spaceId});
    console.log(JSON.stringify({space_id:task.spaceId,js_urls:urls.length}));return;
  }
  // Active survey: autonomously follow in-scope <a href> links (GET navigation only),
  // capturing read endpoints on every view. Never clicks/fills/submits — safe to run on a
  // logged-in production site. Action-looking links (logout/delete/pay…) are skipped.
  if(kind==='survey'){
    await startCapture(page);
    const maxPages=Math.min(Math.max(1,action.max_pages||20),60);
    const inScope=u=>{try{return state.origins.includes(new URL(u).origin);}catch{return false;}};
    const SKIP=/logout|sign-?out|delete|remove|destroy|\bcancel\b|pay|checkout|退出|登出|删除|注销|支付|付款|取消/i;
    const settleRaw=parseInt(process.env.WEB2MCP_SETTLE_MS||'',10);
    const settleMs=Number.isFinite(settleRaw)?settleRaw:8000;
    const seen=new Set(),visited=[],errors=[];
    // Pass 1: breadth-first over in-scope <a href> links (classic multi-page sites).
    const queue=[state.url];
    while(queue.length && visited.length<maxPages){
      const url=queue.shift(),key=url.split('#')[0];
      if(seen.has(key)) continue; seen.add(key);
      if(!inScope(url) || SKIP.test(url)) continue;
      try{ await navigate(page,url); }catch(e){ errors.push({url,error:String(e.message||e).slice(0,80)}); continue; }
      visited.push(url);
      try{ await saveCapture(page,root,'survey '+url,{origins:state.origins,storageSources:state.storage_sources}); }catch(e){}
      let links=[]; try{ links=await page.evaluate(()=>[...document.querySelectorAll('a[href]')].map(a=>a.href)); }catch(e){}
      for(const l of links){ const k=l.split('#')[0]; if(k && !seen.has(k) && inScope(l) && !SKIP.test(l)) queue.push(l); }
    }
    // Pass 2: SPA client-routing — click navigation-only elements (tabs / menu / sidebar) to
    // trigger client-routed views. Strictly navigational: role=tab/menuitem or nav/sidebar/menu
    // containers, and the text must not look like a write/action. Never submits or deletes.
    const BLOCK='发起|提交|删除|新建|创建|保存|付款|支付|取消|确认|上传|导入|导出|修改|编辑|退出|登出|logout|sign-?out|delete|submit|create|save|\\bpay\\b|upload|remove|confirm|edit';
    const clicked=new Set();
    while(visited.length<maxPages){
      let target=null;
      try{ target=await page.evaluate(({blockSrc,done})=>{
        const BLK=new RegExp(blockSrc,'i');
        // Navigation regions only: semantic nav roles, plus clickable children inside <nav>/<aside>/
        // sidebar/menu containers (SPAs often route via <div onClick> with no role). The text
        // blocklist below still filters out anything that looks like a write/action.
        const sel='[role=tab],[role=menuitem],nav a,nav button,nav li,nav div,aside a,aside button,aside li,aside div,[class*=sidebar] a,[class*=sidebar] button,[class*=sidebar] li,[class*=sidebar] div,[class*=menu] a,[class*=menu] button,[class*=menu] li,[class*=tab] button,[class*=nav] button,[class*=nav] li,[class*=nav] div';
        for(const el of document.querySelectorAll(sel)){
          if(!el.getClientRects().length||el.hasAttribute('disabled')) continue;
          const t=(el.innerText||el.getAttribute('aria-label')||'').trim();
          if(!t||t.length>24||BLK.test(t)||done.includes(t)) continue;
          el.setAttribute('data-w2m-nav','1'); return t;
        }
        return null;
      }, {blockSrc:BLOCK, done:[...clicked]}); }catch(e){}
      if(!target) break;
      clicked.add(target);
      try{ await page.click('[data-w2m-nav="1"]'); }
      catch(e){ try{await page.evaluate(()=>document.querySelector('[data-w2m-nav="1"]')?.removeAttribute('data-w2m-nav'));}catch(_){ } continue; }
      try{ await page.evaluate(()=>document.querySelector('[data-w2m-nav="1"]')?.removeAttribute('data-w2m-nav')); }catch(e){}
      await new Promise(r=>setTimeout(r,settleMs));
      try{ if(!state.origins.includes(new URL(await page.url()).origin)){ await navigate(page,state.url); continue; } }catch(e){}
      try{ await saveCapture(page,root,'survey-nav '+target,{origins:state.origins,storageSources:state.storage_sources}); }catch(e){}
      visited.push('[nav] '+target);
    }
    state.stage='observing';state.actions_used=Math.min(state.budget,state.actions_used+visited.length);
    await write(path.join(root,'session.json'),state);
    const trace=JSON.parse(await fs.readFile(path.join(root,'capture.json'),'utf8').catch(()=>'{}'));
    const out={state:'surveyed',pages:visited.length,visited,errors,
      total_exchanges:(trace.exchanges||[]).length,space_id:task.spaceId};
    await write(path.join(root,'rounds',run_id+'.output.json'),out);
    console.log(JSON.stringify({space_id:task.spaceId,surveyed:visited.length,exchanges:out.total_exchanges}));return;
  }
  if(first || kind==='actions' || kind==='resume') await startCapture(page);
  if(first) await navigate(page,state.url);
  if(!first && !state.origins.includes(new URL(await page.url()).origin)) throw new Error('Current page is outside reviewed scope');
  for(const step of action.steps||[]){
    if(step.type==='goto') await navigate(page,step.url);
    else if(step.type==='click') await page.click(step.selector,{label:step.purpose});
    else if(step.type==='fill') await page.fill(step.selector,step.value);
    else if(step.type==='select') await page.selectOption(step.selector,step.value);
    else if(step.type==='press') await page.press(step.selector,step.value);
    if(step.wait_selector) await page.waitForSelector(step.wait_selector,{state:'visible',timeout:10000});
    if(!state.origins.includes(new URL(await page.url()).origin)) throw new Error('Navigation left reviewed scope; inspect before continuing');
  }
  if(action.wait_selector) await page.waitForSelector(action.wait_selector,{state:'visible',timeout:10000});
  const capture=await saveCapture(page,root,action.label||'observe '+state.goal,
    {origins:state.origins,storageSources:state.storage_sources});
  // This reads rendered form semantics only. It does not inspect app source or private state.
  const fields=await page.evaluate(()=>[...document.querySelectorAll('input,select,textarea')]
    .filter(e=>e.getClientRects().length && e.type!=='hidden').slice(0,100).map(e=>({
      tag:e.tagName.toLowerCase(),type:e.type,name:e.name,id:e.id,required:e.required,disabled:e.disabled,
      label:[...(e.labels||[])].map(l=>l.textContent.trim()).join(' '),
      options:e.tagName==='SELECT'?[...e.options].map(o=>({value:o.value,label:o.text})):undefined
    })));
  const observation={goal:state.goal,space_id:task.spaceId,page:'p1',url:await page.url(),
    fields,capture,remaining_actions:state.budget-state.actions_used,content_trust:'untrusted_product_data',
    snapshot:await page.snapshot()};
  state.stage='observing';await write(path.join(root,'session.json'),state);
  await write(path.join(root,'rounds',run_id+'.output.json'),observation);
  console.log(JSON.stringify({space_id:task.spaceId,round:state.round,captured:capture.captured}));
}
