import {expect,it} from 'vitest';
import {runtimeEntryFailureMessage} from '../src/runtime-entry-error.js';
import {RuntimeUsageSealError} from '../src/runtime-usage-seal.js';

it('explains canonical measurement conflict without claiming the Pi module failed to load',()=>{
  const text=runtimeEntryFailureMessage(new RuntimeUsageSealError('measurement_fact_conflict','private details'));
  expect(text).toContain('[measurement_fact_conflict]');
  expect(text).toContain('重复启动不会消除');
  expect(text).not.toContain('private');
  expect(text).not.toContain('runtime_start_failed');
});

it.each([
  ['Pi ComputeCost binding does not match native session/surface authority','runtime_usage_binding_conflict'],
  ['Pi native thinking level binding mismatch','runtime_model_binding_conflict'],
  ['SCL workspace admission rejected [workspace_admission_denied]: synthetic path','workspace_admission_denied'],
  ['Persisted runtime usage authority does not match the resumed Session','runtime_binding_conflict'],
  ['Runtime session binding mismatch: persisted=cc:private-old selected=cc:private-other','runtime_binding_conflict'],
  ['synthetic provider failure with private material','runtime_start_failed'],
])('gives ordinary messages a safe explicit startup diagnostic', (reason,code)=>{
  const text=runtimeEntryFailureMessage(new Error(reason));
  expect(text).toContain(`[${code}]`);
  expect(text).toContain('未派发');
  expect(text).not.toContain('synthetic');
  expect(text).not.toContain('自动重试');
  expect(text).not.toContain('需要完成会话恢复');
});


it.each(['command_transport_failed','measurement_transport_unavailable'] as const)(
  'does not diagnose transport failure %s as a measurement conflict', (code) => {
    const text = runtimeEntryFailureMessage(new RuntimeUsageSealError(code, 'private transport detail', code === 'measurement_transport_unavailable'));
    expect(text).toContain(`[${code}]`);
    expect(text).not.toContain('计量冲突');
    expect(text).not.toContain('重复启动不会消除');
    expect(text).not.toContain('private');
    expect(text).toContain('原请求');
  },
);
