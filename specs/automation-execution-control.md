# 自动 AI 执行控制

## 真源与边界

Relay 读取实际 cron 配置旁的 automation-control.json，Stats 只读展示。生产 Relay 按已采用的路径映射定位。文件为账户所有的普通文件，禁止写权限扩大和链接。当前没有受管写入命令；只由 PULSE 依人类决定写入：持同目录 .automation-control.lock 排他锁，核对读回 SHA-256 未变，在同目录写 0600 临时文件后原子替换，revision 加一，decision_ref 指向该决定，写后读回。Relay 每次执行前读回，写入无需重载。自动分身不能自行改执行许可；其他 Agent 经协作请求交 PULSE。

Schema 为 pulse_automation_control/v1，持有 automatic_enabled、正整数 revision、updated_at、reason、decision_ref 和 entries。每个入口为 job:<id>、dispatch:<owner> 或 resource:<owner>，包含 owner 和显式 enabled。

## 执行语义

自动执行须全局启用、入口登记启用、owner 一致；定时任务还须当前任务存在且 enabled。缺失、损坏、未知入口默认暂停。出队、交接派发、资源 worker 和模型重试前读回，不缓存许可。暂停保留事件和待办，不增加失败计数，不标处置成功，不终止已在执行的进程。扫描、接收和必要通知继续。

人工运行使用原 runJob 或 CLI 的显式 --manual，继续遵守 site、政策和 WI 权限；cron 进程不得声明人工运行。采集不授予 AI 执行权，入口启用也不授予生产修改权。

## 模型与展示

后台 platform_default 从中央 Relay/config/compute-profiles.json 解析 codex-sol-latest，不覆盖桌面用户设置。Astra 由显式 frontier 等配置选择。执行回执记录计算配置、别名、实际模型和来源。Stats 的业务标题目录只提供文案；实际任务和执行入口从节点读取，附观测时间、配置状态和决定来源。陈旧快照不当作当前状态。

## 验收

- 暂停期间资源采集和入队成功；重复 drain、交接及定时触发不启动模型。
- 等待执行或限流重试期间暂停，下一次尝试不运行；已有运行不被强杀。
- 新进程重新读取持久暂停；未知或损坏登记拒绝自动执行。
- 显式人工处理可执行且不越原权限。
- 默认解析 Sol，显式 Astra 不变；实际回执可追溯。
- Stats 包含新增资源入口、状态与时间；正常刷新后已解决事件离开当前告警。
