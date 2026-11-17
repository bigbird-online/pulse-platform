# Oikos → Stats：平台概览与资源状态

关联 WI300 / WI825；成本仍归 ComputeCost，执行权仍归 SCL。此接口不修改两者真值。

## 要回答的问题

常驻节点（ownermac、tcloud-node2，以及新纳管的 tcloud-node3）与可信 Linux Runner 池能否继续承载工作？
读取端要求原四台（C1-ownermac、tcloud-node2、C3-ecs、bigbird-sg）齐全、各出现一次，额外节点只接受 tcloud-node3；四台与五台集合都有效，其余集合整份拒收。
展示磁盘、内存余量、swap、可写性、inode；Runner 同时展示实际与预期运行状态。
Runner 池目前包含 CWA、SG 两台物理主机；不是把“四台机器”改称“四个常驻节点”。
资源健康只证明上述实测维度，不等于 Relay 全渠道、SCL 全流程或所有业务验收通过。

## 唯一生产者和读取链

既有 Oikos `p0-health-probe/probe.sh` 的同一轮真实采样，经 `platform-health.py` 发布
`~/.local/state/oikos/hestia/platform-health-v1.json`。不新增探测任务，不解析人类日志，
不让 Stats 重新判断阈值或伪造处理进度。原有三次失败确认、恢复、路由和通知机制不变。

`oikos_platform_health/v1` 包含四个确定节点、原始指标、Oikos 状态、确认次数、
`observed_at/completed_at/expires_at`、Git SHA 和生产源文件摘要。
状态：`ok`、`at_limit`、`pending`、`incident`、`unknown`。
80% 是已有容量上限的边界提示，不是阈值越界事故。高 swap 不能单独升级事故。
`accepted_or_notified` 只表示处置入口接受或兜底通知送达，不表示正在处理或已修复。

只有完整、同一轮四节点报告才原子替换；旧轮次不能覆盖新轮次。部分或失败发布保留
旧报告原始时间和有效期。报告有效期 1500 秒，与已有五分钟探针及心跳静默契约一致。

ownermac 本机的 Stats exporter 以同一账户直接读取该文件，固定路径、只读、
64 KiB 上限，输出 `oikos.platform.nodes` metric。失败只使该指标 unavailable。
即使旧事故来源全部不可用，新鲜 Oikos 数据仍可展示；整体监控覆盖仍明确 unknown。
App 对缓存继续按原始 expiry 检查，不把下载时间或总快照生成时间当作资源采样时间。
沿既有唯一App刷新loop，白天结合资源有效期在到期前读取，失败间隔有界；保留夜间安静窗口、半小时通用策略及打开面板主动读取。收到报告后再核对时间，不把取数途中完成的新报告误判未来。过期样本仅灰色历史可读，真实未来样本不能作历史参考；不延长读数寿命。详见[来源可靠性](stats-source-reliability.md)。

## 页面归属与旧探针退役

资源实况移入仪表盘默认收起的“Oikos 平台概览”，位于四项总览下方、WI 账本核验上方。
原“未闭环事故”位置由“自动任务”替代；九项业务任务的折叠、暂停与结果状态见
[自动任务设计](stats-automation-card.md)。不再常驻节点指标墙或“监控待修复”列表。

用户已决定六个旧探针全部退役。原事故事件与证据保留在原状态及快照 retired_incidents
历史区，不生成恢复事件，不参与当前事故计数与健康降级。当前 Oikos 资源采集继续工作。

## 迁移契约

生产 Stats 的心跳与 obs 目录必须读取 Relay 已安装的 `pulse_relay_paths/v1` 映射。
映射失效不得回退到 `.relay` 中的旧成功记录。本机读取端不登记心跳注册表：仍启用的
Hestia 心跳由 TCloud Relay 接收，Stats 不读取，心跳能力报告为未实现，不以缺心跳报警。
来源版本使用 reader 的 `.stats-source-revision`，不再误报另一份旧 checkout 的版本。
事故账本保留原单写者路径与历史；这次接入不重建或清空它。

## 验收

合成回归覆盖旧路径漂移、三轮失败/恢复、80% 边界、高 swap、按安排停用 Runner、
缺节点/重复节点、过期/未来时间、未送达处置、旧轮覆盖和监控记录守恒。
正式 Linux 与 macOS 测试通过后，核对 Oikos 生产发布、ownermac reader、App 精确版本。
至少两轮自然探针输出与 App 原始指标逐项比对；证据未完成前不声称已部署或验收。

可选 SCL 扫描策略目录不可读（含 stat、列目录和文件解析失败）必须维持未知覆盖，
不能解释为“无人负责”，也不能让整个资源快照失败。LiveFixture 的所有文件来源必须
位于本测试临时目录；正式 Linux 回归断言这一边界，不靠生产目录存在或权限碰巧通过。
