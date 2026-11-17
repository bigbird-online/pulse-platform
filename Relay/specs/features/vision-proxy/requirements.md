---
feature_name: "vision-proxy"
version: "0.3.0"
created: "2026-05-12T10:50:00+0800"
modified: "2026-10-19T12:00:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md", "cc-process-pool/requirements.md"]
tags: [requirements, l2-spec, vision, multimodal]
changelog:
  - "v0.3.0: 视觉代理只走 Anthropic Messages 格式单次描述"
  - "v0.2.0: 150-spec-sync — 图片处理改为文件路径优先（CC Read 工具原生识图）、vision-proxy 降为 fallback；FR-VP-001 更新为两级路径"
  - "v0.1.0: 初始版本 — bug-20260512-001 修复，从 cc-process-pool 拆出独立功能"
---

# vision-proxy - 功能需求文档

> **功能概述**：当主模型不支持图片理解时，自动调用辅助视觉模型将图片"翻译"为文本描述，然后传递给主模型。对用户完全透明。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 技术设计：`features/vision-proxy/design.md`
> - L2 上游功能：`features/cc-process-pool/requirements.md`

---

## 1. 用户故事（User Stories）

### 主要用户故事

**故事1：自动图片描述**
作为纯文本主模型 agent 的用户，我希望发送图片时能被正确理解和回复。

**故事2：透明处理**
作为用户，我不需要关心视觉模型配置细节，系统应自动判断是否需要图片预处理。

## 2. 功能需求（Functional Requirements）

### FR-VP-001：图片预处理
- **文件路径优先（primary）**：当 image content block 含 `filePath` 字段时，直接构建文本 prompt（如 "用户发了一张图片：/tmp/relay-images/xxx.jpg"），CC 使用内置 Read 工具原生查看图片，无需额外 API 调用
- **vision-proxy 降级（fallback）**：仅当 image block 无 `filePath`（如 print mode 或 filePath 获取失败）且配置了 vision model 时，才调用辅助视觉模型将图片翻译为文本描述
- 仅在 interactive mode 触发（print mode 由 Claude 原生处理图片）
- 替换后的消息包含：原始文本 blocks + 图片路径文本 block（primary）或 图片描述文本 block（fallback）

### FR-VP-002：配置三级优先
1. per-agent `visionModel` 配置（relay.json）
2. 全局环境变量（`RELAY_VISION_*`）
3. 未配置则不预处理

### FR-VP-003：凭证回落链
1. `visionModel.apiKey`（per-agent）
2. `RELAY_VISION_API_KEY`（全局）
3. 主模型凭证：模型目录中主模型所属 provider 的端点与 key；无 provider 时用 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`

### FR-VP-004：API 格式
- 视觉模型一律经 Anthropic Messages 格式调用（`/v1/messages`，x-api-key，`source.base64`），每张图片一次请求

## 3. 非功能需求（Non-Functional Requirements）

### NFR-VP-001：可靠性
- 视觉模型 API 调用失败时，用占位文本替代，不阻塞主模型
- 占位格式：`[图片描述失败：{error}]` 或 `[图片内容无法识别]`

### NFR-VP-002：性能
- 单张图片 API 调用超时 30 秒
- 多张图片逐张处理

### NFR-VP-003：无新依赖
- 使用 Node.js 内置 `fetch`
- 不新增 npm 依赖

## 4. 验收标准（Acceptance Criteria）

### AC-VP-001：图片描述准确性
- 发送已知图片，返回的描述与图片实际内容一致（非幻觉）
- 同一图片多次发送，描述基本一致

### AC-VP-002：配置回落
- 未配置 visionModel 但设了 `RELAY_VISION_MODEL` → 使用 env 配置
- 两者都未配置 → 不触发视觉代理

### AC-VP-003：错误降级
- API 调用失败 → 占位文本，主模型仍可回复
- 多张图片部分失败 → 成功的描述正常展示，失败的显示占位文本
