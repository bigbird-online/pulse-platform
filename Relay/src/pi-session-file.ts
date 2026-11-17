import {closeSync,openSync,readSync,readdirSync,realpathSync} from 'node:fs';
import {dirname,join} from 'node:path';

/** A native session id names identity; --session explicitly restores its history. */
export function piSessionArgs(sessionDir:string,sessionId:string):string[]{
  const files=readdirSync(sessionDir).filter(name=>name.endsWith(`_${sessionId}.jsonl`));
  if(!files.length)return ['--session-id',sessionId];
  const candidates=files.map(file=>{
  const path=realpathSync(join(sessionDir,file));
  if(dirname(path)!==realpathSync(sessionDir))throw new Error('Pi session transcript escaped tenant root');
  const fd=openSync(path,'r');
  let header:string;
  try{const buffer=Buffer.alloc(65536);const count=readSync(fd,buffer,0,buffer.length,0);header=buffer.subarray(0,count).toString('utf8').split('\n')[0]!;}
  finally{closeSync(fd);}
  const value=JSON.parse(header);
  if(value.type!=='session'||value.id!==sessionId)throw new Error('Pi session transcript identity mismatch');
  return {path,timestamp:Date.parse(value.timestamp)};
  });
  if(candidates.length>1){
    if(candidates.some(item=>!Number.isFinite(item.timestamp)))throw new Error('Pi session transcript is ambiguous');
    candidates.sort((a,b)=>b.timestamp-a.timestamp);
    if(candidates[0]!.timestamp===candidates[1]!.timestamp)throw new Error('Pi session transcript is ambiguous');
  }
  return ['--session',candidates[0]!.path];
}
