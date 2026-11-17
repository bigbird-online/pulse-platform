# degen-verify — 解码降解检测器离线复验（B' 好词豁免版）

Duke 二裁 B'+E 落地的**改代码前离线硬门**复验脚本（2026-07-05）。

- `extract.mjs` — 遍历全机 `/home/pulse/.claude/projects/**/*.jsonl`，抽 assistant text
  block 逐行，落 `/tmp/degen-verify/lines.jsonl`（中间品）。
- `judges-line.mjs` — CURRENT（枚举坏词）vs NEW（好词豁免）两个行级判据，与
  `src/outbound-leak-guard.ts` 的 `isDegenLine` 逻辑同步（改一处两处对齐）。
- `final.mjs` — 行级上下文 ground truth 打分：TP/FN(miss)/FP(prose damage) + recall/precision。

跑法（vultr，OUT 路径写死 `/tmp/degen-verify/`）：

```bash
mkdir -p /tmp/degen-verify
node extract.mjs         # 先产 lines.jsonl（约 943 jsonl / 38k 块）
cp judges-line.mjs final.mjs /tmp/degen-verify/ && cd /tmp/degen-verify && node final.mjs
```

硬门基线（943 jsonl，7561 候选行，true-degen 6992 / normal 569）：

| 判据 | recall | precision(FP) |
|------|--------|---------------|
| CURRENT（枚举坏词） | 95.7% | 99.9%（4 FP）|
| NEW-strict（出站闸） | 98.6% | **100%（0 FP）** |
| NEW-relaxed（spawn 扫盘）| 99.0% | 94.5% |

去留由主身 Pulse 决定；这是一次性复验器材，非常驻测试。
