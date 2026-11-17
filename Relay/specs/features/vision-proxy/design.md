---
feature_name: "vision-proxy"
version: "0.3.0"
created: "2026-05-12T10:50:00+0800"
modified: "2026-10-19T12:00:00+0800"
upstream_dependency: ["project-architecture.md", "requirements.md"]
tags: [design, l2-spec, vision, multimodal]
changelog:
  - "v0.3.0: 视觉代理只走 Anthropic Messages 格式单次描述"
  - "v0.2.0: 150-spec-sync — Flow-001 更新为两级路径（filePath primary → vision-proxy fallback）；补充选型理由"
  - "v0.1.0: 初始版本 — bug-20260512-001 修复后同步 Spec"
---

# vision-proxy - 技术设计文档

> **设计概述**：辅助视觉模型代理模块。当主模型不支持图片时，拦截 image content blocks，经 Anthropic Messages 格式调用辅助视觉模型获取文本描述，替换后传递给主模型。
>
> **关联文档**：
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 功能需求：`features/vision-proxy/requirements.md`
> - Bug 报告：`/Volumes/workdisk/BBCC/工程项目/PULSE/bugs/bug-20260512-001/`

---

## 1. 技术栈（Tech Stack）

### 技术栈引用

**基于 L1 项目架构**：`project-specs/project-architecture.md`

| 组件 | 选型 | 说明 |
|------|------|------|
| HTTP 客户端 | Node.js 内置 `fetch` | 无新依赖 |
| 图片编码 | Buffer base64 | Channel → base64 → API |

## 2. 数据模型（Data Model）

### VisionModelConfig（types.ts）

```typescript
interface VisionModelConfig {
  model: string;                         // 视觉模型 ID
  baseUrl?: string;                      // 独立 API 端点
  apiKey?: string;                       // 独立凭证
  systemPrompt?: string;                 // 覆盖默认描述 prompt
}
```

### VisionConfig（内部解析结果）

```typescript
type VisionConfig = {
  model: string;
  baseUrl: string;
  apiKey: string;
  systemPrompt: string;
};
```

## 3. 模块架构

### 源文件

| 文件 | 职责 |
|------|------|
| `src/vision-proxy.ts` | 视觉代理核心：配置解析 + API 调用 |
| `src/types.ts` | `VisionModelConfig` 类型定义 |
| `src/pool.ts` | 集成点：interactive mode send 路径中调用 `describeImages()`；`resolveVisionFallbackAuth()` 解析主模型凭证 |

### 导出接口

```typescript
// 配置解析
resolveVisionConfig(agentVisionModel?: VisionModelConfig): VisionConfig | null

// 图片描述
describeImages(
  imageBlocks: ImageContentBlock[],
  config: VisionConfig,
  fallbackAuth: { baseUrl: string; apiKey: string },
): Promise<string>

// Content block 工具
extractImageBlocks(content: unknown[]): ImageContentBlock[]
replaceImagesWithText(content: unknown[], description: string): TextContentBlock[]
```

## 4. 核心流程

### Flow-001：Interactive mode 图片预处理（两级路径）

```
pool.send(tenantId, message)
  → mode === 'interactive' && content has image blocks?
    → YES:
      → 检查 image blocks 是否带 filePath 字段?
        → YES（primary - 文件路径）:
          → 构建文本 prompt："用户发了图片：<filePath>"
          → CC 使用内置 Read 工具原生查看图片
          → 无额外 API 调用
        → NO（fallback - vision-proxy）:
          → visionConfigs.get(tenantId) → VisionConfig
          → describeImages(imageBlocks, config, fallbackAuth)
            → per image: POST {baseUrl}/v1/messages（source.base64 + systemPrompt）
          → replaceImagesWithText(content, description)
      → processedMessage → 继续原有发送逻辑
    → NO: 直接发送
```

> **选型理由**：文件路径方案让 CC 使用内置 Read 工具直接查看图片，图片理解精度最高（CC 原生 multimodal），且无额外 API 调用成本。飞书 channel 已实现图片下载后保存至 `RELAY_IMAGE_DIR`（`/tmp/relay-images/`）临时文件并附加 `filePath` 字段。vision-proxy 仅作为无 filePath 场景的降级路径。

### 凭证解析

`baseUrl` / `apiKey` 为空时取 `fallbackAuth`：主模型在模型目录中有 provider 时，用该 provider 的 `base_url_env` 与 key pool 选中的 key；否则用 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`。

## 5. 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RELAY_VISION_MODEL` | — | 视觉模型 ID |
| `RELAY_VISION_BASE_URL` | — | Anthropic 兼容端点基础 URL |
| `RELAY_VISION_API_KEY` | — | 独立凭证 |
| `RELAY_VISION_SYSTEM_PROMPT` | `请描述这张图片的内容。` | 描述 prompt |

## 6. 错误处理

| 错误场景 | 处理方式 |
|---------|---------|
| 视觉模型 HTTP 429（限流） | 占位文本 `[图片描述失败：HTTP 429: ...]` |
| 视觉模型 HTTP 4xx/5xx | 占位文本 `[图片描述失败：HTTP {status}: ...]` |
| 视觉模型返回空内容 | 占位文本 `[图片内容无法识别]` |
| 网络超时（30s） | 占位文本 `[图片描述失败：Network timeout]` |
| 多图部分失败 | 成功的展示描述，失败的展示占位文本 |
