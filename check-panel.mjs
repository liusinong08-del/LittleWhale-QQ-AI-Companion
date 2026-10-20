// Run with node check-panel.mjs; no browser, account, API key or model calls.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html=fs.readFileSync(new URL('./index.html',import.meta.url),'utf8');
// Native form validation must allow an image without accompanying text.
assert.ok(!/\brequired\b/.test(html.match(/<textarea\b[^>]*\bid="message"[^>]*>/)[0]));
const listener=html.split('\n').find(line=>line.startsWith("window.addEventListener('hashchange'"));
const api=html.split('\n').find(line=>line.startsWith('async function api('));
assert.ok(listener&&api);
const saved=new Map([['whale-role-draft','my unfinished role']]);
let onHash,refreshes=0,reconnects=0;
const context={token:'A'.repeat(43),renewAfter:0,location:{hash:'#'+'B'.repeat(43),pathname:'/'},
  sessionStorage:{setItem:(k,v)=>saved.set(k,v)},
  history:{replaceState:()=>{context.location.hash='';}},
  refresh:initial=>{assert.equal(initial,true);refreshes++;},refreshSocial:()=>{},
  window:{addEventListener:(event,fn)=>{assert.equal(event,'hashchange');onHash=fn;},WhaleApp:{reconnect:()=>reconnects++}},
  fetch:async()=>({status:401,ok:false,json:async()=>({error:'expired'})}),Date:{now:()=>100}};
vm.runInNewContext(listener+'\n'+api,context);
onHash();
assert.equal(context.token,'B'.repeat(43));
assert.equal(saved.get('whale-token'),context.token);
assert.equal(saved.get('whale-role-draft'),'my unfinished role');
assert.equal(refreshes,1);
for(const hash of ['#bad','#'+'B'.repeat(43)]){context.location.hash=hash;onHash();}
assert.equal(refreshes,1);
await assert.rejects(context.api('/api/state'),/expired/);
await assert.rejects(context.api('/api/state'),/expired/);
assert.equal(reconnects,1);
console.log('PASS: renewed fragment changes credentials, preserves role draft, rejects invalid links and bounds reconnect requests');
