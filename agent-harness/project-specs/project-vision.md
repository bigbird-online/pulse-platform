---
name: project-vision
version: 1.0.0
status: specification
work_item: wi_20260922_018
modified: 2026-10-07
---

# agent-harness 产品目标

让人和 Agent 沿同一 WI 完成从问题探索、立项、方案、执行到验收和迭代的工作；换会话、投影丢失和外部暂时故障由产品处理，不由人类维护内部状态。

标准流程：交流探索 → 确认 WHY/WHAT 并创建 Draft → 确认 HOW 并采用 Graph → 按图执行并自动进入 UAT → 接受成果，或返回执行、方案、目标继续迭代。

快捷流程：Agent 判断是否需要落盘 → 创建 lightweight WI → 完成工作、写 WS、closeout；需要更多组织时在原 WI 升级 standard。

内核是唯一生命周期写者。所有账本、登记解析视图、缓存和队列都是可丢弃、可重建的投影；真实人类决定、成果、原始输入和外部执行结果保持可追溯。

成功由全部功能实现、派生状态全失后的正常接续、旧写者退出，以及真实用户入口的完整旅程证明。代码规模、旧回归数量、安装成功和单条流程通过都不能单独证明完成。

范围是 agent-harness 及其必要消费者接入，保留原有公开功能；模型推理、渠道通信、外部产品业务由相应系统承担。生命周期重写不要求重造这些系统。

文档入口：

- [功能要求](project-requirements.md)：AH 编号及能力边界。
- [架构](project-architecture.md)：唯一内核、领域规则、投影、外部动作与迁移。
- [接口与数据合同](project-protocol.md)：业务意图、输入、Graph、响应及旧入口去向。
- [验收](project-acceptance.md)：每项要求怎样独立证明。
- [质量规范](project-standards.md)：规格、代码、测试与交付规则。
- [实施顺序](project-roadmap.md)：Spec 先行，再逐项实现。

这些文档定义替换目标。旧 `specs/` 是能力与历史依据，不再把旧存储、恢复分支或原测试文件强加给新架构。
