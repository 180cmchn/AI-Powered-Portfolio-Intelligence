// Run every browser group, retaining each exit status instead of short-circuiting.
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
const results=[];
for (const group of ['iteration','production','seventh','ninth','tenth','eleventh']) {
  const started=Date.now();
  const exit=await new Promise(resolve=>{
    const child=spawn(process.execPath,[...(group==='ninth'?['--import','./tests/frontend_ninth_state.mjs']:[]),`tests/frontend_${group}.mjs`],{stdio:'inherit',env:process.env});
    child.once('error',error=>resolve({code:1,error:String(error)}));
    child.once('exit',(code,signal)=>resolve({code:code ?? 1,signal}));
  });
  results.push({group,...exit,duration_ms:Date.now()-started});
}
await mkdir('test-results',{recursive:true});
await writeFile('test-results/browser-groups.json',JSON.stringify(results,null,2));
console.log(JSON.stringify({browser_groups:results}));
process.exitCode=results.some(result=>result.code!==0)?1:0;
