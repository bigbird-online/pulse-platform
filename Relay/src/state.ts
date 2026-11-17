import { readFileSync, writeFileSync, renameSync, existsSync, openSync, closeSync, fsyncSync, unlinkSync } from 'node:fs';
import {randomUUID} from 'node:crypto';
import {dirname} from 'node:path';
import type { StateManager, PersistedState, PersistedSession, ChannelIntakeRecord } from './types.js';

const DEBOUNCE_MS = 2000;

function emptyState(): PersistedState {
  return { version: 1, updatedAt: new Date().toISOString(), sessions: {} };
}

function normalizeState(parsed: PersistedState): PersistedState {
  const sessions: Record<string, PersistedSession> = {};
  for (const [poolKey, session] of Object.entries(parsed.sessions ?? {})) {
    sessions[poolKey] = { ...session, runtime: session.runtime ?? 'cc' };
  }
  // Retired top-level records (for example legacy pending SCL intents) are not loaded.
  const channelIntake = parsed.channelIntake && typeof parsed.channelIntake === 'object' ? parsed.channelIntake : undefined;
  return { version: parsed.version, updatedAt: parsed.updatedAt, sessions, ...(channelIntake ? { channelIntake } : {}) };
}

export function createStateManager(statePath: string): StateManager {
  let state: PersistedState = emptyState();
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;

  function writeToDisk(strict = false): void {
    state.updatedAt = new Date().toISOString();
    const serialized = JSON.stringify(state, null, 2);
    if(strict){
      const tmp=`${statePath}.${randomUUID()}.tmp`;
      try{
        const fd=openSync(tmp,'wx',0o600);
        try{writeFileSync(fd,serialized);fsyncSync(fd);}finally{closeSync(fd);}
        renameSync(tmp,statePath);
        const directory=openSync(dirname(statePath),'r');
        try{fsyncSync(directory);}finally{closeSync(directory);}
      }finally{if(existsSync(tmp))unlinkSync(tmp);}
      return;
    }
    const tmp = statePath + '.tmp';
    try {
      writeFileSync(tmp, serialized);
      renameSync(tmp, statePath);
    } catch {
      // Legacy non-authority writes retain their direct-write availability fallback.
      try { writeFileSync(statePath, serialized); } catch { /* best effort */ }
    }
  }

  function scheduleSave(): void {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      writeToDisk();
    }, DEBOUNCE_MS);
  }

  return {
    statePath,
    load(): PersistedState {
      if (!existsSync(statePath)) {
        state = emptyState();
        return state;
      }
      try {
        const raw = readFileSync(statePath, 'utf-8');
        const parsed = JSON.parse(raw) as PersistedState;
        state = normalizeState(parsed);
      } catch {
        console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'Failed to parse state.json, using empty state', path: statePath });
        state = emptyState();
      }
      return state;
    },

    getSession(poolKey: string): PersistedSession | undefined {
      return state.sessions[poolKey];
    },

    updateSession(poolKey: string, update: Partial<PersistedSession>): void {
      const existing = state.sessions[poolKey];
      let next: PersistedSession;
      if (existing) {
        next = { ...existing, ...update };
      } else {
        next = update as PersistedSession;
      }
      state.sessions[poolKey] = { ...next, runtime: next.runtime ?? 'cc' };
      scheduleSave();
    },

    removeSession(poolKey: string): void {
      delete state.sessions[poolKey];
      scheduleSave();
    },

    getAllSessions(): Record<string, PersistedSession> {
      return state.sessions;
    },

    getChannelIntake(key: string): ChannelIntakeRecord[] {
      return [...(state.channelIntake?.[key] ?? [])];
    },

    setChannelIntake(key: string, records: ChannelIntakeRecord[]): void {
      const journals = { ...state.channelIntake };
      if (records.length > 0) journals[key] = [...records];
      else delete journals[key];
      if (Object.keys(journals).length > 0) state.channelIntake = journals;
      else delete state.channelIntake;
      scheduleSave();
    },


    flush(): void {
      if (debounceTimer) {
        clearTimeout(debounceTimer);
        debounceTimer = null;
      }
      writeToDisk();
    },

    flushStrict(): void {
      if (debounceTimer) {
        clearTimeout(debounceTimer);
        debounceTimer = null;
      }
      writeToDisk(true);
    },

    dispose(): void {
      if (debounceTimer) {
        clearTimeout(debounceTimer);
        debounceTimer = null;
      }
    },
  };
}
