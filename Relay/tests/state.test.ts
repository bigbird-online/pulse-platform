import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createStateManager } from '../src/state.js';

describe('StateManager', () => {
  let tempDir: string;
  let statePath: string;

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'relay-state-'));
    statePath = join(tempDir, 'state.json');
  });

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true });
  });

  describe('load()', () => {
    it('returns empty state when file does not exist', () => {
      const mgr = createStateManager(statePath);
      const state = mgr.load();
      expect(state.version).toBe(1);
      expect(state.sessions).toEqual({});
      mgr.dispose();
    });

    it('parses valid JSON correctly', () => {
      const data = {
        version: 1,
        updatedAt: '2026-04-09T00:00:00Z',
        sessions: {
          'owner:ken': {
            poolKey: 'owner:ken',
            agentId: 'ken',
            sessionId: 'abc-123',
            turnCount: 42,
            maxTurns: 100,
            lastActivity: 1000,
            status: 'active' as const,
          },
        },
      };
      writeFileSync(statePath, JSON.stringify(data));
      const mgr = createStateManager(statePath);
      const state = mgr.load();
      expect(state.sessions['owner:ken'].turnCount).toBe(42);
      expect(state.sessions['owner:ken'].agentId).toBe('ken');
      expect(state.sessions['owner:ken'].runtime).toBe('cc');
      mgr.dispose();
    });

    it('returns empty state on corrupted JSON with console warning', () => {
      writeFileSync(statePath, '{not valid json!!!');
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const mgr = createStateManager(statePath);
      const state = mgr.load();
      expect(state.version).toBe(1);
      expect(state.sessions).toEqual({});
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
      mgr.dispose();
    });
  });

  describe('updateSession()', () => {
    it('merges partial update correctly', () => {
      const mgr = createStateManager(statePath);
      mgr.load();
      mgr.updateSession('owner:ken', {
        poolKey: 'owner:ken',
        agentId: 'ken',
        sessionId: 'abc',
        turnCount: 10,
        maxTurns: 100,
        lastActivity: 1000,
        status: 'active',
      });
      mgr.updateSession('owner:ken', { turnCount: 20 });
      const session = mgr.getSession('owner:ken');
      expect(session?.turnCount).toBe(20);
      expect(session?.agentId).toBe('ken');
      expect(session?.runtime).toBe('cc');
      mgr.dispose();
    });

    it('triggers debounced write (file updated after 2s)', async () => {
      const mgr = createStateManager(statePath);
      mgr.load();
      mgr.updateSession('owner:ken', {
        poolKey: 'owner:ken',
        agentId: 'ken',
        sessionId: 'abc',
        turnCount: 5,
        maxTurns: 100,
        lastActivity: 1000,
        status: 'active',
      });
      // Not written yet
      expect(existsSync(statePath)).toBe(false);
      // Wait for debounce
      await new Promise(r => setTimeout(r, 2500));
      expect(existsSync(statePath)).toBe(true);
      const written = JSON.parse(readFileSync(statePath, 'utf-8'));
      expect(written.sessions['owner:ken'].turnCount).toBe(5);
      expect(written.sessions['owner:ken'].runtime).toBe('cc');
      mgr.dispose();
    });

    it('coalesces multiple rapid updates into one write', async () => {
      const mgr = createStateManager(statePath);
      mgr.load();
      mgr.updateSession('owner:ken', {
        poolKey: 'owner:ken',
        agentId: 'ken',
        sessionId: 'abc',
        turnCount: 1,
        maxTurns: 100,
        lastActivity: 1000,
        status: 'active',
      });
      mgr.updateSession('owner:ken', { turnCount: 2 });
      mgr.updateSession('owner:ken', { turnCount: 3 });
      await new Promise(r => setTimeout(r, 2500));
      const written = JSON.parse(readFileSync(statePath, 'utf-8'));
      expect(written.sessions['owner:ken'].turnCount).toBe(3);
      mgr.dispose();
    });
  });

  describe('flush()', () => {
    it('writes immediately without debounce', () => {
      const mgr = createStateManager(statePath);
      mgr.load();
      mgr.updateSession('owner:ken', {
        poolKey: 'owner:ken',
        agentId: 'ken',
        sessionId: 'abc',
        turnCount: 99,
        maxTurns: 100,
        lastActivity: 1000,
        status: 'active',
      });
      expect(existsSync(statePath)).toBe(false);
      mgr.flush();
      expect(existsSync(statePath)).toBe(true);
      const written = JSON.parse(readFileSync(statePath, 'utf-8'));
      expect(written.sessions['owner:ken'].turnCount).toBe(99);
      mgr.dispose();
    });

    it('strict flush exposes an authority-critical persistence failure', () => {
      const mgr = createStateManager(join(tempDir, 'missing-parent', 'state.json'));
      mgr.load();
      mgr.updateSession('owner:pulse', {
        poolKey: 'owner:pulse',
        agentId: 'pulse',
        runtime: 'pi',
        sessionId: 'planned-target',
        turnCount: 0,
        maxTurns: 1000,
        lastActivity: 1000,
        status: 'active',
      });
      expect(() => mgr.flushStrict()).toThrow();
      mgr.dispose();
    });
  });

  describe('removeSession()', () => {
    it('removes the specified session', () => {
      const mgr = createStateManager(statePath);
      mgr.load();
      mgr.updateSession('owner:ken', {
        poolKey: 'owner:ken',
        agentId: 'ken',
        sessionId: 'abc',
        turnCount: 0,
        maxTurns: 100,
        lastActivity: 1000,
        status: 'active',
      });
      mgr.removeSession('owner:ken');
      expect(mgr.getSession('owner:ken')).toBeUndefined();
      mgr.dispose();
    });
  });

  describe('getAllSessions()', () => {
    it('returns all sessions', () => {
      const mgr = createStateManager(statePath);
      mgr.load();
      mgr.updateSession('owner:ken', {
        poolKey: 'owner:ken',
        agentId: 'ken',
        sessionId: 'a',
        turnCount: 0,
        maxTurns: 100,
        lastActivity: 1000,
        status: 'active',
      });
      mgr.updateSession('owner:duke', {
        poolKey: 'owner:duke',
        agentId: 'duke',
        sessionId: 'b',
        turnCount: 0,
        maxTurns: 100,
        lastActivity: 1000,
        status: 'active',
      });
      const all = mgr.getAllSessions();
      expect(Object.keys(all)).toHaveLength(2);
      expect(all['owner:ken']).toBeDefined();
      expect(all['owner:duke']).toBeDefined();
      mgr.dispose();
    });
  });

  describe('dispose()', () => {
    it('cancels pending debounced write', async () => {
      const mgr = createStateManager(statePath);
      mgr.load();
      mgr.updateSession('owner:ken', {
        poolKey: 'owner:ken',
        agentId: 'ken',
        sessionId: 'abc',
        turnCount: 0,
        maxTurns: 100,
        lastActivity: 1000,
        status: 'active',
      });
      mgr.dispose();
      await new Promise(r => setTimeout(r, 2500));
      // File should NOT have been written since dispose cancelled the timer
      expect(existsSync(statePath)).toBe(false);
    });
  });
});
