# XIUXIAN 统一目录

唯一项目根目录：`/Users/operator/.hermes/workspace/projects/xiuxian`。

| 目录 | 用途 |
| --- | --- |
| 根目录的 Python 模块 | 共用发送、回执解析、观察器、状态和传输逻辑 |
| `scripts/` | 账号命令入口、调度、健康检查、恢复和运维脚本 |
| `scripts/lib/` | 共用 Shell 函数和账号路径逻辑 |
| `qingyuanzi-new-source/` | 青元子独立实现、协议、测试及运行状态 |
| `fishing/` | 垂钓独立实现、测试及运行状态 |
| `config/` | 账号配置和启用任务注册表 |
| `state/` | 主项目运行状态、日志及本次整理记录 |
| `references/` | 两份独立上游/参考仓库，各自保留 Git 历史 |
| `archive/` | 旧项目副本、迁移材料及历史备份 |
| `docs/` | 当前维护文档 |

## 迁入路径

| 原路径 | 统一根目录下的新位置 |
| --- | --- |
| `~/.hermes/scripts/xiuxian_active_healthcheck.sh` | `scripts/xiuxian_active_healthcheck.sh` |
| `~/.hermes/scripts/xiuxian_staging_healthcheck.sh` | `scripts/xiuxian_staging_healthcheck.sh` |
| `~/.hermes/workspace/upstream-xiuxian-mian/` | `references/upstream-xiuxian-mian/` |
| `~/.hermes/workspace/xianxia-companion/` | `references/xianxia-companion/` |
| `~/.openclaw/workspace/xiuxian/` | `archive/openclaw-xiuxian/` |
| `~/Documents/New project 3/xiuxian/` | `archive/documents-xiuxian/` |
| `~/.hermes/workspace/state/xiuxian-migration/` | `archive/migrations/hermes-cutover/` |
| `~/.hermes/workspace/state/xiuxian-migration-backups/` | `archive/backups/hermes-migration/` |
| `~/.hermes/backups/xiuxian-qyz-before-reference-20260909/` | `archive/backups/qyz-before-reference-20260909/` |

原目录保留指向新位置的兼容软链接。Hermes 的定时脚本校验要求真实入口位于
`~/.hermes/scripts/` 内，因此 `xiuxian_active_healthcheck.sh` 在原处保留一个
仅含 `exec` 的转发入口，完整实现只放在本项目 `scripts/`。
macOS 的已安装 LaunchAgent 文件继续位于 `~/Library/LaunchAgents/`。

## 维护约定

新增实现放入上表对应分类。已有 Python 导入路径、账号状态目录和生产命令入口
保持兼容。生产扫描和测试排除 `archive/`、`references/`；参考仓库自行运行测试。
历史整机备份、压缩备份和聊天记录是恢复材料，不作为新的脚本维护目录。

2026-09-10 整理前核验：109 个已加载 XIUXIAN LaunchAgent 中，108 个直接引用
本项目，另 1 个引用现已迁入本项目的 staging 健康检查入口。没有已加载的
XIUXIAN LaunchAgent 使用旧 OpenClaw 项目。本次目录整理不重载调度或重启发送器。

`state/layout-consolidation/latest-run.txt` 指向整理记录目录，其中保存移动清单、
整理前代码指纹、调度清单和测试结果。`moves.json` 记录每项原路径、新路径及
兼容方式，可据此逐项恢复。后续去重清单保存在 `state/cleanup-commit/`；
已核对并删除的重复文件可从清单指定的保留副本恢复。运行状态、会话文件和
缓存统一由 Git 忽略，完整参考仓库及历史归档保留在本机。

主项目离线回归只载入根目录 `Path(".").glob("test_*.py")` 对应模块，
不能递归发现 `archive/` 或 `references/`。青元子与垂钓分别在
`qingyuanzi-new-source/`、`fishing/` 目录运行
`../.venv/bin/python -m unittest discover -s . -p 'test_*.py'`。
天机使用根目录 `.venv/bin/python -m unittest tianji_trial.test_tianji_trial`。
