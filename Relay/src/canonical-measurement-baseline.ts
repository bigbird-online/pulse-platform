import type { RuntimeUsageBinding } from './runtime-usage-binding.js';

export interface CanonicalMeasurementReadback {
  schema_version:'scl_measurement_epoch/v1';
  status:'baseline_pending'|'open'|'sealed';
  work_item_id:string;agent_id:string;execution_epoch:number;phase:string;
  runtime:string|null;procurement_source:string|null;authority_ref:string|null;
  execution_surface:string;measurement_epoch_ref:string;
  runtime_binding_sha256:string|null;baseline_sha256:string|null;
  final_sha256:string|null;seal_ref:string|null;seal_sha256:string|null;
}

/** A reference to an accepted measurement origin, not a replacement v3 binding.
 * The active process retains its independently validated runtime binding. */
export interface CanonicalMeasurementBaselineReference {
  schema:'pulse_canonical_measurement_baseline_reference/v1';
  status:'open';work_item_id:string;agent_id:string;epoch:number;phase:string;
  runtime:'pi';procurement_source:string;authority_ref:string;execution_surface:string;
  measurement_epoch_ref:string;baseline_binding_sha256:string;baseline_sha256:string;
  active_runtime_binding_sha256:string;
}
export const CANONICAL_MEASUREMENT_BASELINE_ENV='PULSE_CANONICAL_MEASUREMENT_BASELINE_REFERENCE';
const SHA=/^[a-f0-9]{64}$/;

export function parseCanonicalMeasurement(value:unknown):CanonicalMeasurementReadback{
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('Canonical measurement readback is invalid');
  const m=value as Record<string,unknown>;
  if(m.schema_version!=='scl_measurement_epoch/v1'||!['baseline_pending','open','sealed'].includes(String(m.status))
    ||!Number.isSafeInteger(m.execution_epoch)||Number(m.execution_epoch)<1
    ||!['work_item_id','agent_id','phase','execution_surface','measurement_epoch_ref'].every(k=>typeof m[k]==='string'&&m[k]))
    throw new Error('Canonical measurement identity is invalid');
  for(const key of ['runtime_binding_sha256','baseline_sha256','final_sha256','seal_sha256']){
    if(m[key]!==null&&m[key]!==undefined&&!SHA.test(String(m[key])))throw new Error('Canonical measurement digest is invalid');
  }
  return m as unknown as CanonicalMeasurementReadback;
}

export function validateMeasurementBaselineReference(value:unknown,binding:RuntimeUsageBinding):CanonicalMeasurementBaselineReference{
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('Canonical measurement baseline reference is invalid');
  const r=value as CanonicalMeasurementBaselineReference;
  const keys=['schema','status','work_item_id','agent_id','epoch','phase','runtime','procurement_source','authority_ref',
    'execution_surface','measurement_epoch_ref','baseline_binding_sha256','baseline_sha256','active_runtime_binding_sha256'];
  if(Object.keys(r).sort().join('\0')!==keys.sort().join('\0')
    ||r.schema!=='pulse_canonical_measurement_baseline_reference/v1'||r.status!=='open'
    ||binding.schema!=='pulse_runtime_usage_binding/v3'||r.runtime!=='pi'||binding.runtime!=='pi'
    ||r.work_item_id!==binding.work_item_id||r.agent_id!==binding.agent_id||r.epoch!==binding.epoch
    ||r.phase!==binding.phase||r.procurement_source!==binding.procurement_source
    ||r.authority_ref!==binding.authority_ref||r.execution_surface!==binding.execution_surface
    ||r.active_runtime_binding_sha256!==binding.binding_sha256
    ||!/^measurement_epoch_v1_[a-f0-9]{64}$/.test(r.measurement_epoch_ref)
    ||![r.baseline_binding_sha256,r.baseline_sha256].every(v=>SHA.test(v)))
    throw new Error('Canonical measurement baseline reference does not match this runtime');
  return r;
}

export function measurementBaselineReference(binding:RuntimeUsageBinding,measurement:CanonicalMeasurementReadback):CanonicalMeasurementBaselineReference|undefined{
  const m=parseCanonicalMeasurement(measurement);
  if(m.work_item_id!==binding.work_item_id||m.agent_id!==binding.agent_id||m.execution_epoch!==binding.epoch
    ||m.phase!==binding.phase||m.execution_surface!==binding.execution_surface)
    throw new Error('Canonical measurement does not identify this runtime interval');
  if(m.status==='baseline_pending')return;
  // 参考只供 Pi 扩展续接原点；CC 由 Host 按 binding 产出 baseline，无需也不能复用 Pi 参考。
  if(binding.runtime!=='pi')return;
  if(m.status!=='open'||m.final_sha256||m.seal_ref||m.seal_sha256
    ||m.runtime!=='pi'||m.authority_ref!==binding.authority_ref||m.procurement_source!==binding.procurement_source
    ||!m.runtime_binding_sha256||!m.baseline_sha256)
    throw new Error('Canonical measurement origin cannot be reopened');
  if(m.runtime_binding_sha256===binding.binding_sha256)return;
  return validateMeasurementBaselineReference({schema:'pulse_canonical_measurement_baseline_reference/v1',status:'open',
    work_item_id:binding.work_item_id,agent_id:binding.agent_id,epoch:binding.epoch,phase:binding.phase,runtime:'pi',
    procurement_source:binding.procurement_source,authority_ref:binding.authority_ref!,execution_surface:binding.execution_surface!,
    measurement_epoch_ref:m.measurement_epoch_ref,baseline_binding_sha256:m.runtime_binding_sha256,
    baseline_sha256:m.baseline_sha256,active_runtime_binding_sha256:binding.binding_sha256},binding);
}
