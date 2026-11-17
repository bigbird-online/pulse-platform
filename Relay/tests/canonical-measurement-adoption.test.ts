import {describe,it,expect} from 'vitest';
import {measurementBaselineReference} from '../src/canonical-measurement-baseline.js';
import {buildRuntimeUsageBinding,runtimeAuthorityRef} from '../src/runtime-usage-binding.js';
const wi='wi_20990101_001';
describe('CC intervals do not use the Pi origin reference',()=>{
 it('accepts an open CC measurement without throwing',()=>{
  const sid='cc-native';
  const binding=buildRuntimeUsageBinding({workItemId:wi,agentId:'worker',phase:'work_item',epoch:5,runtime:'cc',procurementSource:'claude_subscription',authorityRef:runtimeAuthorityRef(sid),executionSurface:'surface_v1_test'});
  const measurement={schema_version:'scl_measurement_epoch/v1',status:'open',work_item_id:wi,agent_id:'worker',execution_epoch:5,phase:'work_item',
   runtime:'cc',procurement_source:'claude_subscription',authority_ref:binding.authority_ref,execution_surface:'surface_v1_test',
   measurement_epoch_ref:'measurement_epoch_v1_'+'c'.repeat(64),runtime_binding_sha256:'1'.repeat(64),baseline_sha256:'2'.repeat(64),
   final_sha256:null,seal_ref:null,seal_sha256:null} as const;
  expect(measurementBaselineReference(binding,measurement as any)).toBeUndefined();
  expect(()=>measurementBaselineReference(binding,{...measurement,execution_epoch:6} as any)).toThrow('does not identify');
 });
});
