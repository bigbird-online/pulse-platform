import {writeFileSync,realpathSync} from 'node:fs';
import {join} from 'node:path';

export function nativeCoreFixture(directory:string):string {
  directory=realpathSync(directory);
  const extension=join(directory,'command.mjs'),python=join(directory,'python'),entrypoint=join(directory,'harness.pyz');
  for(const path of [extension,python,entrypoint])writeFileSync(path,'fixture',{mode:0o755});
  const shellPath="'"+join(directory,'request.json').replaceAll("'","'\\''")+"'";
  writeFileSync(python, '#!/bin/sh\ncat > '+shellPath+
    '\nprintf \'%s\\n\' \'{"status":"local_committed","work_item":{"id":"wi_native_fixture","business_progress":"executing"}}\'\n', {mode:0o755});
  const manifest=join(directory,'manifest.json');
  writeFileSync(manifest,JSON.stringify({
    schemaVersion:'relay_pi_runtime_manifest/v1',piVersion:'0.87.1',piCommit:'a'.repeat(40),
    workItemCoreHost:{extension,python,entrypoint},
    profiles:{daily:{kind:'host-native',nodeResourceRoots:[directory],workItemCoreHost:{extension,python,entrypoint}}},
  }));
  return manifest;
}
