# 青元子拟人化适配

实现位于 `qingyuanzi-new-source/`，运行时仅使用 Python 标准库和项目已有的
HTTP／浏览器依赖。共用算法在 `xiuxian_humanize/`，青元子参数与输入适配在
`qyz_humanize.py`。不需要安装新的包。

## 已接入的调用点

| 调用点 | 行为 |
| --- | --- |
| `PreparedNativeTurnstileBrowser.command` | 三阶 Bézier 鼠标轨迹、控制点高斯扰动、采样点高斯微抖动、弧长 ease-in-out；实际落点在半径 3px 内 |
| 浏览器左键点击 | 实际 down/up 使用截断正态：均值 85ms、标准差 15ms，范围 30～150ms；每次完整点击后等待 80～600ms |
| 浏览器连续操作 | 每 20～40 次完整点击后插入 2～5 秒微休息；计数属于该浏览器实例，重建实例后重置 |
| `HttpTransport` request hook | httpx 完成默认头合并后，每次请求重新打乱头顺序；名字、值、JSON 和身份字段保持不变 |
| 首次 Telegram 连接 | 与之后的重连一样经 `transport.ensure_connected` 的全舰队连接闸门；失败由账号 supervisor 按其退避重试（原 `connect_with_retry` 及其重试装饰器已删除） |
| `ProtocolRuntime._post_until_reply` | 原有指数等待下限，加 Full Jitter 和正态延迟；仍由协议层决定可重试错误和动作截止时间 |
| 动态窗口轮询 | 从请求开始时间算起，截断正态 360～390ms；慢响应后立即继续，不累积多余等待、不产生追赶请求突发 |
| 入场扫描、共享入口轮询、健康心跳、读连接保温 | 每次重新采样截断正态，均值为原等待上限的 90%，范围为原上限的 75%～100% |
| 读连接重建、账号 supervisor | 现有重试循环内加入有上限的随机退避 |

参考脚本的哈希保护保留。浏览器适配器在参考层的 `mousePressed` 调用中完成
一次真实的 down/hold/up，再消费其后配对的 `mouseReleased`，所以没有重复点击。
参考脚本原有的 120ms 等待仍存在于真实按压区间之外。休息和按压等待若被中断，
成功按下的按钮会通过 `finally` 尝试释放。浏览器每次移动为 25 个采样点，
耗时采用 250～700ms 截断正态，避免大量同步 DevTools 消息挤占战斗线程。

蓄力计划、真实 holdMs、命中绝对时刻、结束时刻和账号归属继续由现有战斗协议
控制；普通鼠标的 85ms 不用于服务器战斗蓄力。HTTP 层不新增隐式重试。
针对持续失败的动作请求，保留旧等待下限，是为了避免纯 Full Jitter 将平均
等待减半、使 600 秒结算窗口内的请求数翻倍。其总等待上限为 10 秒，且下一次
等待若会越过动作截止时间就直接退出。

这是既有 live automation 的本地适配，不增加 cron、LaunchAgent、入口群或发送器。
账号 `world_boss_state.json` 和共享入口状态仍由既有宿主写入，新增随机状态只驻留
内存。运行版本为 `qyz-reference-v28`，心跳包含
`humanization_version=qyz-humanization-v1`，用于确认实际进程已加载此适配。
用户已授权测试后直接运行，部署沿用既有 KeepAlive 监控，在没有进行中战斗时
优雅重新加载。测试和示例不发起真实游戏请求。

## 可复用接口

```python
import random
from xiuxian_humanize import (
    DelayDistribution, TimingJitter, full_jitter, generate_mouse_path,
)

rng = random.Random(42)  # 测试复现；生产默认使用独立 RNG，不固定种子
path = generate_mouse_path((100, 100), (640, 400), radius=6, rng=rng)
# path.samples: time（秒）, x, y；path.controls: 四个 Bézier 控制点

timing = TimingJitter(
    interval=DelayDistribution(.3, .1, .08, .8, kind="lognormal"), rng=rng,
)
hold_seconds = timing.hold_duration()
plan = timing.after_operation()  # 每次已完成操作调用一次；plan.total 是总等待
# timing.press_and_release(key_down, key_up) 同样适用于键盘

retry_wait = full_jitter(attempt=2, base=.5, cap=10, rng=rng)  # 第 3 次重试前的等待秒数
```

各函数的 `rng` 参数可注入以便测试。同一个外部 RNG 不应同时交给多个无共同锁的线程使用。

二维正态分布本来没有有限边界，因此这里采用“条件为落在圆内”的截断二维正态，
默认 sigma=R/3；使用截断 Rayleigh 半径和随机角度精确采样，不把越界值压在圆周。
R=0 或 sigma=0 时精确落在中心。采样点噪声在起终点衰减至零；先加速后减速
描述的是基础曲线，局部微抖动会带来小幅速度变化。

延迟的 mean/stddev 是截断前的秒数参数；对数正态也使用算术均值／标准差。
普通时间抖动使用 Gaussian 拒绝采样，窄区间使用反 CDF 回退，避免边界堆积；
数值不可表示的极端截断区间明确报错。Full Jitter 按标准定义使用
`U(0, min(cap, base * 2**attempt))`，不是正态分布。HTTP/1.1 实现可能固定 Host
在首位，HTTP/2 及中间代理也可能重排头；此功能不保证网络上的所有头顺序。

## 离线验证

在 `qingyuanzi-new-source/` 运行：

```sh
../.venv/bin/python -m unittest discover -s . -p 'test_*.py'
```

（早期的 `humanize_demo.py` 示例和宿主 `qingyuanzi_host.py --check` 已删除，下文
2026-09-30 的记录保留当时的验证步骤。）

测试覆盖二维落点径向 CDF／方差、时间分布均值／方差、曲线加减速、落点和
按压坐标一致、一次真实点击、打断时释放、微休息计数、HTTP 合并后的头顺序、
动作重试期限和既有战斗回归。离线通过只证明代码和
模拟契约；服务器 PERFECT 数、实际通过率和战绩仍以官方回执为准。

2026-09-30 验证结果：青元子完整目录 232 项测试通过（新增拟人化 21 项），
根目录 `test_qyz_health_collection` 与 `test_miniapp_registry` 共 11 项通过；
离线示例、`--check` 和 `git diff --check` 通过。仓库及父工作区均未提供
`scripts/ops-alignment-check.sh`，因此该脚本未运行。

## 2026-09-30 生产加载

用户要求测试后直接运行，已在确认六账号无进行中战斗、最新事件均已结算后，
向既有 LaunchAgent 的 PID 492 发送 SIGTERM，由 KeepAlive 正常拉起新版。
新 PID **4126** 于 **2026-09-30 18:21:32 +0800** 启动，运行 **v28**。
本次版本标记修改后再次通过完整 232 项青元子测试与 11 项健康／注册表测试。

六账号连续两轮真实心跳均为同一 PID、`qyz-reference-v28` 和
`qyz-humanization-v1`；连接、身份、成员、来源和验证服务检查全通过。
事件数量与最新事件 ID 均未因重载改变，未重放旧战斗。既有自动监听和调度已运行，
下一场事件按融合后的流程处理，无需另行启动示例或独立拟人化进程。

业务健康如实保留旧战绩：四账号上一场未达到全 PERFECT、一账号上一场伤害倍率
0.55，另一个账号正常。上述五项 BAD 是已有官方回执的结果，部署没有清除它们，
也不宣称新版本已经取得下一场实战成功。生产加载证据保存在
`qingyuanzi-new-source/state/humanization-rollout/20260930-v28.json`。
