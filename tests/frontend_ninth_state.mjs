// Controlled races through the production state loader; no DOM/HTTP fixture claims.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const {createSixthUI}=await import('data:text/javascript;base64,'+Buffer.from(await readFile(new URL('../app/static/sixth.js',import.meta.url),'utf8')).toString('base64'));
globalThis.document={addEventListener(){}};
const provider=(id,revision=1,status='not_fetched')=>({id,revision,display_name:id,connection_source:'environment',key_available:true,catalog:{status,models:[]}});
let rows=[provider('A')], mode='fail', calls=[], gate=null;
const state={route:'providers',accountId:'account-a',accounts:[]};
const api=async path=>{
 if(path==='/api/providers')return structuredClone(rows);
 if(path==='/api/environment-connection')return {status:'current'};
 if(path.endsWith('/models')){
  calls.push(path);
  if(gate){const captured=gate;gate=null;await captured.wait;}
  if(mode==='fail')throw Error('controlled directory read failure');
  return {status:'success',models:[{id:'actual-model'}]};
 }
 throw Error('unexpected fixture path');
};
const noop=()=>'';
const ui=createSixthUI({state,api,e:String,render:noop,toast:noop,operation:noop,field:noop,selectField:noop,dialog:noop,objectView:noop});
const snapshot=()=>JSON.parse(ui.snapshot());
await ui.load();
assert.equal(snapshot().catalogErrors.A.revision,1);
await ui.load();
assert.equal(calls.length,1,'failed automatic directory request waits for an explicit retry');
mode='success';await ui.load({retryCatalogs:true});
assert.deepEqual(snapshot().catalogErrors,{},'successful retry clears its own directory error');
// Fail A and B independently, then recover only A from the persisted directory.
mode='fail';rows=[provider('A'),provider('B')];await ui.load();await ui.load();
assert.deepEqual(Object.keys(snapshot().catalogErrors).sort(),['A','B']);
rows=[provider('A',1,'success'),provider('B')];await ui.load();
assert.deepEqual(Object.keys(snapshot().catalogErrors),['B'],'A success cannot clear B failure');
// A connection revision change discards only that revision's old error.
rows=[provider('A',1,'success'),provider('B',2)];mode='success';await ui.load();
assert.deepEqual(snapshot().catalogErrors,{});
// Older read resolves after a newer connection is loaded: neither result nor
// error may overwrite the new revision or clear another provider's failure.
rows=[provider('A',2),provider('B',2)];mode='fail';await ui.load();await ui.load();
let release;gate={wait:new Promise(resolve=>{release=resolve;})};mode='success';
const late=ui.load({retryCatalogs:true});
await new Promise(resolve=>setTimeout(resolve,0));
rows=[provider('A',3,'success'),provider('B',2)];await ui.load();
release();await late;
assert.equal(snapshot().providers.find(p=>p.id==='A').revision,3);
assert.deepEqual(Object.keys(snapshot().catalogErrors),['B']);
// Account navigation makes an old rejection irrelevant even without new load.
rows=[provider('C')];gate={wait:new Promise(resolve=>{release=resolve;})};mode='fail';
const switched=ui.load();await new Promise(resolve=>setTimeout(resolve,0));state.accountId='account-b';release();await switched;
assert.equal(snapshot().catalogErrors.C,undefined);
console.log('Ninth provider state recovery and target-bound race regressions passed');
