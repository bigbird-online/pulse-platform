import { createHash } from 'node:crypto';
import type { ConversationRuntimeId } from './types.js';
import { parseConversationPoolKey } from './conversation-address.js';

const ROOT_REF_DOMAIN: Record<ConversationRuntimeId, 'cc_root_session' | 'codex_run' | 'pi_rpc_session' | 'agy_conversation'> = {
  cc: 'cc_root_session',
  'cc-sdk': 'cc_root_session',
  codex: 'codex_run',
  pi: 'pi_rpc_session',
  agy: 'agy_conversation',
};

export interface RelayInteractiveLineage {
  root_agent: string;
  execution_mode: 'human';
  session_role: 'main';
  root_run_ref: string;
  parent_run_ref: 'unknown';
  cron_job_ref: 'unknown';
  scan_ref: 'unknown';
}

function rootAgent(tenantId: string): string {
  const agentId = parseConversationPoolKey(tenantId)?.agentId.trim();
  return agentId || 'unknown';
}

function rootRunRef(sessionId: string, runtime: ConversationRuntimeId | undefined): string {
  const normalized = sessionId.trim();
  if (!normalized || normalized === 'unknown' || !runtime) return 'unknown';
  const material = [
    'pulse.compute_cost.lineage.v1',
    ROOT_REF_DOMAIN[runtime],
    normalized,
  ].join('\0');
  return `lineage_v1_${createHash('sha256').update(material, 'utf8').digest('hex')}`;
}

/** Build the seven-field contract at Relay's human-interactive response seam. */
export function buildRelayInteractiveLineage(input: {
  tenantId: string;
  sessionId: string;
  runtime?: ConversationRuntimeId;
}): RelayInteractiveLineage {
  return {
    root_agent: rootAgent(input.tenantId),
    execution_mode: 'human',
    session_role: 'main',
    root_run_ref: rootRunRef(input.sessionId, input.runtime),
    parent_run_ref: 'unknown',
    cron_job_ref: 'unknown',
    scan_ref: 'unknown',
  };
}
