// One explicit instance map for production readers and writers. Development
// without PULSE_RELAY_PATHS retains isolated, explicitly supplied test paths.
import { readFileSync, realpathSync, existsSync } from 'node:fs';
import { dirname, isAbsolute, relative, resolve } from 'node:path';

export const RELAY_PATHS_ENV = 'PULSE_RELAY_PATHS';
export type RelayPathKey = 'relayConfig' | 'cronConfig' | 'cronState' | 'piManifest'
  | 'models' | 'pricing' | 'runtimePriority' | 'dataDir' | 'modelOverridesDir'
  | 'piSessionsDir' | 'cronRuntimeStateDir' | 'meteringDir' | 'observabilityDir' | 'logsDir' | 'environment';
export interface RelayPathMap {
  schema: 'pulse_relay_paths/v1';
  root: string;
  paths: Partial<Record<RelayPathKey, string>>;
  requiredChannels?: Array<{ agentId: string; channel: 'telegram' | 'feishu' }>;
}
let loaded: { file: string; value: RelayPathMap } | undefined;

function contained(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel !== '..' && !rel.startsWith('../') && !isAbsolute(rel);
}

export function readRelayPathMap(): RelayPathMap | undefined {
  const file = process.env[RELAY_PATHS_ENV];
  if (file === undefined) return undefined;
  if (!isAbsolute(file) || /[\r\n\0]/.test(file)) throw new Error('invalid PULSE_RELAY_PATHS');
  if (loaded?.file === file) return loaded.value;
  const value = JSON.parse(readFileSync(file, 'utf8')) as RelayPathMap;
  if (value.schema !== 'pulse_relay_paths/v1' || typeof value.root !== 'string'
      || !isAbsolute(value.root) || !value.paths || typeof value.paths !== 'object') {
    throw new Error('invalid managed Relay path map');
  }
  const root = realpathSync(value.root);
  if (root === '/' || !contained(root, realpathSync(file))) throw new Error('Relay map escapes platform root');
  for (const path of Object.values(value.paths)) {
    if (typeof path !== 'string' || !isAbsolute(path) || /[\r\n\0]/.test(path)
        || !contained(resolve(value.root), resolve(path))) throw new Error('Relay path escapes platform root');
    let ancestor = path;
    while (!existsSync(ancestor) && dirname(ancestor) !== ancestor) ancestor = dirname(ancestor);
    if (!contained(root, realpathSync(ancestor))) throw new Error('Relay path alias escapes platform root');
  }
  for (const key of ['relayConfig', 'cronConfig', 'dataDir'] as const) {
    if (!value.paths[key]) throw new Error(`managed Relay map missing ${key}`);
  }
  if (value.requiredChannels !== undefined && (!Array.isArray(value.requiredChannels)
      || value.requiredChannels.some(item => !item || typeof item.agentId !== 'string'
        || !item.agentId || !['telegram', 'feishu'].includes(item.channel)))) {
    throw new Error('invalid required Relay channel inventory');
  }
  // A running process keeps its configuration location stable. Root changes
  // require the same managed service restart as other instance bindings.
  loaded = { file, value };
  return value;
}

export function managedRelayPath(key: RelayPathKey, selected?: string): string | undefined {
  const map = readRelayPathMap();
  if (!map) return selected;
  const expected = map.paths[key];
  if (!expected) {
    if (selected) throw new Error(`undeclared managed Relay path: ${key}`);
    return undefined;
  }
  if (selected && resolve(selected) !== resolve(expected)) {
    throw new Error(`managed Relay ${key} differs from selected configuration`);
  }
  return expected;
}
