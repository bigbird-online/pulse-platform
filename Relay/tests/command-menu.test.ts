import { describe, it, expect, vi } from 'vitest';
import {
  getAllMenuCommands,
  registerCommandMenu,
} from '../src/command-menu.js';

describe('command-menu', () => {
  describe('getAllMenuCommands()', () => {
    it('returns all commands', () => {
      const cmds = getAllMenuCommands();
      expect(cmds).toHaveLength(23);
    });

    it('includes all relay commands', () => {
      const cmds = getAllMenuCommands();
      const names = cmds.map(c => c.command);
      expect(names).toContain('start');
      expect(names).toContain('stop');
      expect(names).toContain('status');
      expect(names).toContain('new');
      expect(names).not.toContain('rollover');
      expect(names).toContain('wi');
      expect(names).toContain('reset');
      expect(names).toContain('session');
      expect(names).toContain('runtime');
      expect(names).toContain('model');
      expect(names).toContain('models');
      expect(names).toContain('think');
      expect(names).toContain('compact');
      expect(names).toContain('context');
      expect(names).toContain('yolo');
      expect(names).toContain('usage');
      expect(names).toContain('usage_seal');
      expect(names).toContain('kill');
      expect(names).toContain('restart');
      expect(names).toContain('help');
    });

    it('does not include commands that are unavailable in fixed-bot mode', () => {
      const cmds = getAllMenuCommands();
      const names = cmds.map(c => c.command);
      expect(names).not.toContain('list');
      expect(names).not.toContain('switch');
    });

    it('all commands have non-empty descriptions', () => {
      const cmds = getAllMenuCommands();
      for (const cmd of cmds) {
        expect(cmd.description.length).toBeGreaterThan(0);
      }
    });
  });

  describe('registerCommandMenu()', () => {
    it('calls bot.api.setMyCommands with all commands', async () => {
      const mockBot = {
        api: {
          setMyCommands: vi.fn().mockResolvedValue(undefined),
        },
      };
      await registerCommandMenu(mockBot as never);
      expect(mockBot.api.setMyCommands).toHaveBeenCalledOnce();
      const args = mockBot.api.setMyCommands.mock.calls[0][0];
      expect(args).toHaveLength(23);
    });

    it('does not throw when API call fails', async () => {
      const mockBot = {
        api: {
          setMyCommands: vi.fn().mockRejectedValue(new Error('TG API error')),
        },
      };
      // Should not throw
      await expect(registerCommandMenu(mockBot as never)).resolves.toBeUndefined();
    });
  });
});
