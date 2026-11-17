/** Public startup diagnostics contain stable codes, never raw provider output. */
import { RuntimeUsageSealError } from './runtime-usage-seal.js';

export function runtimeEntryFailureMessage(error:unknown):string {
  if(error instanceof RuntimeUsageSealError){
    const conflict = ['measurement_fact_conflict', 'measurement_epoch_conflict',
      'measurement_authority_conflict', 'seal_conflict'].includes(error.code);
    const next = conflict
      ? '平台核对原用量绑定和封存凭据并修复计量冲突，重复启动不会消除该问题。'
      : error.code === 'command_transport_failed' || error.code === 'measurement_transport_unavailable'
        ? '平台先读回原请求的计量结果，再修复命令通信；不能把未知结果当作失败而重复建立基线。尚未确认后台重试。'
        : '平台核对原请求的用量绑定及准备结果，修复已报告的缺失或校验错误后继续；尚未确认后台重试。';
    return `本次消息未派发：会话计量准备失败 [${error.code}]。已有会话和工作进度保留；${next}`;
  }
  const reason=error instanceof Error?error.message:String(error??'');
  const admission=reason.match(/SCL workspace admission rejected \[([a-z0-9_]{1,64})\]/)?.[1];
  const specific = /Pi ComputeCost binding/.test(reason) ? 'runtime_usage_binding_conflict'
    : /Pi native (provider\/model|thinking level)/.test(reason) ? 'runtime_model_binding_conflict'
    : /Pi bounded usage|Managed Pi baseline/.test(reason) ? 'runtime_usage_unavailable' : undefined;
  const code=admission??specific??(/WorkItem target has not adopted/.test(reason)?'entry_authority_unavailable':undefined)??(/Runtime session binding mismatch|Persisted (runtime usage authority|WorkItem session reference|workspace runtime identity)/.test(reason)
    ?'runtime_binding_conflict':'runtime_start_failed');
  const next=code==='runtime_model_binding_conflict'
    ?'平台核对原会话指定的模型、提供方和实际运行读回，修复配置后沿原请求继续。'
    :/usage|measurement/.test(code)
      ?'平台核对原计量绑定、执行代际和用量凭据；保留历史，修复计量冲突或缺失，不重复建立基线。'
      :admission||code==='entry_authority_unavailable'
        ?'平台核对当前 Agent 的工作区登记和授权范围，先落实已有授权；未就绪范围不执行，已有获准工作不受影响。'
        :'平台核对原会话绑定与启动结果，保留已完成步骤，仅恢复未完成部分。';
  return `本次消息未派发：运行会话启动失败 [${code}]。会话历史保留；${next}不要求直接更换会话；尚未确认后台重试。`;
}
