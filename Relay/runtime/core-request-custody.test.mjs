import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync,readdirSync,symlinkSync,mkdirSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {requestCustody} from './core-request-custody.mjs';
test('unknown results retain one original request across Host restarts and concurrent attempts',()=>{
 const root=realpathSync(mkdtempSync(join(tmpdir(),'native-custody-')));
 try{
  const a=requestCustody(root,'same-original-session'),b=requestCustody(root,'same-original-session');
  const data={identity:'a'.repeat(64),request_id:'original.1',unresolved:true};
  a.appendEntry('pulse.native-request/v1',data);
  assert.equal(b.getEntries()[0].data.request_id,'original.1');
  assert.throws(()=>b.appendEntry('pulse.native-request/v1',{...data,request_id:'duplicate.2'}),/Original unresolved/);
  b.appendEntry('pulse.native-request/v1',{...data,request_id:'other',unresolved:false});
  assert.equal(a.getEntries().length,1);
  b.appendEntry('pulse.native-request/v1',{...data,unresolved:false});
  assert.equal(a.getEntries().length,0);
  a.appendEntry('pulse.native-request/v1',{...data,request_id:'new-real.3'});
  assert.equal(requestCustody(root,'same-original-session').getEntries()[0].data.request_id,'new-real.3');
  const dir=join(root,readdirSync(root)[0]);
  const raw=JSON.parse(readFileSync(join(dir,readdirSync(dir)[0]),'utf8'));
  assert.deepEqual(Object.keys(raw.data).sort(),['identity','request_id','unresolved']);
 }finally{rmSync(root,{recursive:true,force:true});}
});
test('redirected private transport does not dispatch',()=>{
 const root=realpathSync(mkdtempSync(join(tmpdir(),'native-custody-')));
 try{mkdirSync(join(root,'target'),{mode:0o700});symlinkSync(join(root,'target'),join(root,'redirect'));
 assert.throws(()=>requestCustody(join(root,'redirect'),'session'),/Unsafe/);
 }finally{rmSync(root,{recursive:true,force:true});}
});
