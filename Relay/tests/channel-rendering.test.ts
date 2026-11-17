import { describe, expect, it } from 'vitest';
import { activityMilestone, ChannelTurnRenderingState, TURN_ACCEPTED_STATUS_TEXT, TurnActivityCounter } from '../src/channel-rendering.js';

describe('ChannelTurnRenderingState', () => {
  it('replaces a transient busy status instead of copying it into commentary or final text', () => {
    const state = new ChannelTurnRenderingState('tenant/session/turn/feishu');
    expect(state.status(TURN_ACCEPTED_STATUS_TEXT, 'accepted')).toMatchObject({
      text: '▶️ 已收到，开始处理…',
      final: false,
    });
    expect(state.commentary('先检查现场。', 'commentary-1')?.text).toBe('先检查现场。');
    expect(state.final('结论如下。', 'final-1')?.text).toBe('结论如下。');
  });

  it('overlays bounded activity without copying it into the authoritative final', () => {
    const state = new ChannelTurnRenderingState('tenant/session/turn/telegram');
    state.commentary('先检查现场。', 'commentary-1');

    expect(state.activity('⏳ 仍在工作，已推进 5 个执行步骤…', 'activity-5')?.text).toBe(
      '先检查现场。\n\n⏳ 仍在工作，已推进 5 个执行步骤…',
    );
    expect(state.currentText()).toBe('先检查现场。');
    expect(state.final('最终结论。', 'final-1')?.text).toBe('最终结论。');
  });

  it('keeps the latest activity milestone visible over later non-final body frames', () => {
    const state = new ChannelTurnRenderingState('tenant/session/turn/telegram');
    expect(state.activity('⏳ 仍在工作，已推进 5 个执行步骤…', 'activity-5')?.text).toBe(
      '⏳ 仍在工作，已推进 5 个执行步骤…',
    );
    expect(state.commentary('不应遮住计数。', 'commentary-1')?.text).toBe(
      '不应遮住计数。\n\n⏳ 仍在工作，已推进 5 个执行步骤…',
    );
    expect(state.snapshot('权威中间快照。', 'snapshot-1')?.text).toBe(
      '权威中间快照。\n\n⏳ 仍在工作，已推进 5 个执行步骤…',
    );
    expect(state.final('最终结论。', 'final-1')?.text).toBe('最终结论。');
  });

  it('preserves every actual count; transport owns time coalescing', () => {
    for (let step = 1; step <= 40; step += 1) {
      expect(activityMilestone(step)).toBe(`⏳ 仍在工作，已推进 ${step} 个执行步骤…`);
    }
    for (const invalid of [0, -1, 0.5, NaN, Infinity]) expect(activityMilestone(invalid)).toBeNull();
  });

  it('counts only effects inside the current accepted turn and resets exactly', () => {
    const activity = new TurnActivityCounter();
    expect(activity.advance('tenant:pi')).toBeNull();

    activity.begin('tenant:pi');
    expect(activity.advance('tenant:pi')).toEqual({
      step: 1,
      text: '⏳ 仍在工作，已推进 1 个执行步骤…',
    });
    expect(activity.advance('tenant:pi')).toEqual({ step: 2, text: '⏳ 仍在工作，已推进 2 个执行步骤…' });
    activity.end('tenant:pi');
    expect(activity.advance('tenant:pi')).toBeNull();

    activity.begin('tenant:pi');
    expect(activity.advance('tenant:pi')?.step).toBe(1);
  });

  it('normalizes Pi cumulative snapshots into one evolving message and one final state', () => {
    const state = new ChannelTurnRenderingState('tenant/session/turn/feishu');
    expect(state.snapshot('已开工。')).toMatchObject({ text: '已开工。', final: false });
    expect(state.snapshot('已开工。第一竖切候选已推送。')).toMatchObject({
      text: '已开工。第一竖切候选已推送。', final: false,
    });
    expect(state.final('已开工。第一竖切候选已推送。这还不是完成。')).toMatchObject({
      text: '已开工。第一竖切候选已推送。这还不是完成。', final: true,
    });
    expect(state.final('已开工。第一竖切候选已推送。这还不是完成。')).toBeNull();
    expect(state.final('迟到的重放副本')).toBeNull();
  });

  it('keeps true deltas instead of treating them as repeated snapshots', () => {
    const state = new ChannelTurnRenderingState('tenant/session/turn/tg');
    state.delta('abc', 'event-1');
    expect(state.delta('abc', 'event-2')?.text).toBe('abcabc');
    expect(state.delta('abc', 'event-2')).toBeNull();
  });

  it('deduplicates commentary and final when either contains the other', () => {
    const state = new ChannelTurnRenderingState('scope');
    state.commentary('先检查现场。');
    expect(state.final('先检查现场。结论如下。')?.text).toBe('先检查现场。结论如下。');

    const inverse = new ChannelTurnRenderingState('scope-2');
    inverse.commentary('先检查现场。结论如下。');
    expect(inverse.final('结论如下。')?.text).toBe('先检查现场。结论如下。');
  });

  it('separates independent Codex commentary into readable paragraphs', () => {
    const state = new ChannelTurnRenderingState('scope');
    state.commentary('先检查代码。');
    expect(state.commentary('再运行测试。')?.text).toBe('先检查代码。\n\n再运行测试。');
  });

  it('ignores replayed event ids without persisting a hash of the body', () => {
    const state = new ChannelTurnRenderingState('tenant/session/turn/channel');
    expect(state.delta('敏感正文', 'opaque-sequence-7')).not.toBeNull();
    expect(state.delta('敏感正文', 'opaque-sequence-7')).toBeNull();
    expect(state.identity).toBe('tenant/session/turn/channel');
  });

  it.each(['cc', 'codex', 'cc-sdk', 'pi'])('%s keeps a single authoritative final on both channels', (runtime) => {
    for (const channel of ['telegram', 'feishu']) {
      const state = new ChannelTurnRenderingState(`tenant/session/turn/${runtime}/${channel}`);
      state.commentary('正在处理。', 'commentary-1');
      expect(state.final('正在处理。完整结果。', 'final-1')?.final).toBe(true);
      expect(state.final('正在处理。完整结果。', 'final-1')).toBeNull();
      expect(state.currentText()).toBe('正在处理。完整结果。');
    }
  });

  it('clears body and replay ids after transport reaches a terminal result', () => {
    const state = new ChannelTurnRenderingState('tenant/session/turn/channel');
    state.delta('sensitive active body', 'event-1');
    state.final('sensitive final body', 'event-2');
    state.dispose();

    expect(state.currentText()).toBe('');
    expect(state.final('late replay', 'event-3')).toBeNull();
    expect(state.delta('late delta', 'event-4')).toBeNull();
  });
});
