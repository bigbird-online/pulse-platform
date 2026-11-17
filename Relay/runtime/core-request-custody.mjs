/** Private unresolved transport IDs. Business facts exist only in the core. */
import {createHash, randomUUID} from 'node:crypto';
import {mkdirSync, lstatSync, realpathSync, readdirSync, readFileSync, openSync,
  writeFileSync, fsyncSync, closeSync, linkSync, unlinkSync} from 'node:fs';
import {join, isAbsolute} from 'node:path';
const type='pulse.native-request/v1';
export function requestCustody(root, session) {
  if (!root || !isAbsolute(root) || !session) throw new Error('Missing native Host transport custody');
  mkdirSync(root,{recursive:true,mode:0o700});
  function privatePath(path, directory=false) {
    const info=lstatSync(path);
    if (realpathSync(path)!==path || info.uid!==process.getuid?.() || (info.mode&0o077) ||
        (directory?!info.isDirectory():!info.isFile()) || (!directory && info.size>4096))
      throw new Error('Unsafe native transport custody');
  }
  privatePath(root,true);
  const directory=join(root,createHash('sha256').update(session).digest('hex'));
  mkdirSync(directory,{mode:0o700,recursive:true}); privatePath(directory,true);
  function record(path) {
    privatePath(path);
    const row=JSON.parse(readFileSync(path,'utf8'));
    if(row.customType!==type || row.type!=='custom' || row.data?.unresolved!==true ||
       !/^[a-f0-9]{64}$/.test(row.data.identity) || typeof row.data.request_id!=='string' ||
       !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(row.data.request_id) ||
       Object.keys(row.data).sort().join(',')!=='identity,request_id,unresolved')
      throw new Error('Invalid native transport record');
    return row;
  }
  function sync() {const fd=openSync(directory,'r');try{fsyncSync(fd);}finally{closeSync(fd);}}
  function getEntries() {
    return readdirSync(directory).filter(name=>/^[a-f0-9]{64}\.json$/.test(name))
      .map(name=>record(join(directory,name)));
  }
  function appendEntry(customType,data) {
    if(customType!==type || !/^[a-f0-9]{64}$/.test(data.identity)) throw new Error('Invalid native request identity');
    const path=join(directory,data.identity+'.json');
    if (!data.unresolved) {
      try {if(record(path).data.request_id===data.request_id){unlinkSync(path);sync();}}
      catch(error){if(error.code!=='ENOENT')throw error;}
      return;
    }
    try {
      const original=record(path);
      if(original.data.request_id!==data.request_id) throw new Error('Original unresolved core request must be read back before a new attempt');
      return;
    } catch(error){if(error.code!=='ENOENT')throw error;}
    if(getEntries().length>=256) throw new Error('Read back existing native requests before adding more');
    const temporary=join(directory,'.pending-'+randomUUID());
    const fd=openSync(temporary,'wx',0o600);
    try{writeFileSync(fd,JSON.stringify({type:'custom',customType,data}));fsyncSync(fd);}finally{closeSync(fd);}
    try {
      // Link is no-clobber: two Host processes cannot dispatch different IDs
      // for an already uncertain intention. Fully persisted bytes precede send.
      try{linkSync(temporary,path);sync();}
      catch(error){if(error.code!=='EEXIST')throw error;
        if(record(path).data.request_id!==data.request_id) throw new Error('Concurrent original native request requires readback');}
    }finally{unlinkSync(temporary);}
  }
  return {getEntries,appendEntry};
}
