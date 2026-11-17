# MVS: 修复 tenant session 文件 existsSync 权限穿透问题

## 目标

解决 tenant CC 在 Relay 重启后反复 crash 的问题。根因：`findExistingSessionFile` 和 `claudeSessionIdArgv` 使用 `existsSync()`（pulse 用户身份）检查 tenant session 文件，但 tenant 目录 `drwx------` 导致 pulse 无法 stat → 误判 session 不存在 → 使用 `--session-id` 而非 `--resume` → CC 报 "Session ID already in use" → exit code 1 → crash 循环。

## 边界

- 修改 `Bigbird/Relay/src/pool.ts` 中 `findExistingSessionFile` 和 `claudeSessionIdArgv`
- 将 `existsSync` 替换为已有的 `statFileAsTenant`（sudo stat），与 `resolveInteractiveSessionFilePath` 保持一致
- `statFileAsTenant` 需要 tenantId 参数，`findExistingSessionFile` 需要新增此参数

## 验收

1. 以 pulse 用户对 `drwx------` tenant 目录下的 session 文件调用修改后的检查 → 返回正确存在性
2. tenant CC spawn 使用 `--resume` 而非 `--session-id`
3. Relay 重启后 tenant 不再触发 "Session ID already in use" crash 循环
4. 现有 Relay 测试通过
5. 非 linuxUser 场景（pulse 自己的 agent）行为不变

## 风险

- `statFileAsTenant` 需要 spawn `sudo` 子进程，比 `existsSync` 慢。但仅在 spawn CC 时调用一次，不在热路径上，可接受。
- `claudeSessionIdArgv` 目前无 tenantId 上下文，需要从调用链传入。检查所有调用点确保不遗漏。

## 明确不做

- 不修复通知风暴问题（shutdown 阶段的多条消息），那是独立问题
- 不改动 `repairSessionJsonl` 的权限逻辑（它也用 `existsSync`，但影响较小，可后续处理）
- 不修改 tenant 目录权限（保持隔离语义）