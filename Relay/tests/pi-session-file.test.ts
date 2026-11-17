import {readFileSync,utimesSync,realpathSync,mkdtempSync,writeFileSync,rmSync,symlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {afterEach,expect,it} from 'vitest';
import {piSessionArgs} from '../src/pi-session-file.js';
const dirs:string[]=[];
function dir(){const d=mkdtempSync(join(tmpdir(),'pi-resume-'));dirs.push(d);return d;}
afterEach(()=>dirs.splice(0).forEach(d=>rmSync(d,{recursive:true,force:true})));
it('opens the existing transcript instead of creating an empty session with the same id',()=>{
 const d=dir();const p=join(d,'older_abc.jsonl');writeFileSync(p,JSON.stringify({type:'session',id:'abc'})+'\n');
 expect(piSessionArgs(d,'abc')).toEqual(['--session',realpathSync(p)]);
});
it('creates a new session only when that id has no transcript',()=>expect(piSessionArgs(dir(),'fresh')).toEqual(['--session-id','fresh']));
it('rejects mismatched headers and ambiguous transcripts',()=>{
 const d=dir();writeFileSync(join(d,'first_abc.jsonl'),JSON.stringify({type:'session',id:'other'})+'\n');
 expect(()=>piSessionArgs(d,'abc')).toThrow();
 writeFileSync(join(d,'first_abc.jsonl'),JSON.stringify({type:'session',id:'abc'})+'\n');
 writeFileSync(join(d,'second_abc.jsonl'),JSON.stringify({type:'session',id:'abc'})+'\n');
 expect(()=>piSessionArgs(d,'abc')).toThrow();
});
it('rejects transcript links outside the tenant directory',()=>{
 const d=dir(),outside=join(dir(),'source.jsonl');writeFileSync(outside,JSON.stringify({type:'session',id:'abc'})+'\n');
 symlinkSync(outside,join(d,'linked_abc.jsonl'));expect(()=>piSessionArgs(d,'abc')).toThrow();
});

it('resumes the newest native generation when older restarts reused the same identity',()=>{
 const d=dir();const old=join(d,'old_abc.jsonl'),latest=join(d,'latest_abc.jsonl');
 writeFileSync(old,JSON.stringify({type:'session',id:'abc',timestamp:'2099-01-01T00:00:00.000Z'})+'\n');
 writeFileSync(latest,JSON.stringify({type:'session',id:'abc',timestamp:'2099-01-02T00:00:00.000Z'})+'\n');
 const before=readFileSync(old,'utf8');utimesSync(old,new Date('2100-01-01'),new Date('2100-01-01'));
 expect(piSessionArgs(d,'abc')).toEqual(['--session',realpathSync(latest)]);
 expect(readFileSync(old,'utf8')).toBe(before);
});
it('rejects equally dated native generations instead of guessing',()=>{
 const d=dir();for(const name of ['a','b'])writeFileSync(join(d,`${name}_abc.jsonl`),JSON.stringify({type:'session',id:'abc',timestamp:'2099-01-01T00:00:00.000Z'})+'\n');
 expect(()=>piSessionArgs(d,'abc')).toThrow('ambiguous');
});
