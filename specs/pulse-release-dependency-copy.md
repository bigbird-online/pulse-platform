# Relay 发布依赖复制契约

归属：wi_20260826_825。生产入口：`Scripts/pulse-release-writer.py` 的 `copy_release`。

Mac 发布候选的 `Relay/node_modules` 使用逐文件 `clonefile`，共享未变化的数据块，文件身份独立。Linux、系统没有该 API、跨卷或文件系统不支持时保留 `copy2`。权限、容量或 I/O 错误使候选创建失败，走既有候选清理路径。

目录与符号链接仍由 `copytree(symlinks=True)` 处理；字节、时间与权限沿用原复制语义。源文件与候选修改互不影响，候选封存不改变源权限。不得使用硬链接或修改现用制品。

制品 manifest 和安装结果记录实际 `cloned_files` / `copied_files`。这些计数不代表释放字节；容量准入估算继续按完整复制计算，保留集、构建、原子发布、激活和回滚门保持原契约。

验收：真实 Mac APFS 克隆、双向修改隔离、独立 inode、权限封存隔离、符号链接；不支持时复制；权限/空间/I/O 失败不退为普通复制、候选不发布。Linux 真实部署契约 CI 验复制分支。源码与测试通过不等正式发布采用；后续真实发布须读回 manifest 计数及安装结果。
