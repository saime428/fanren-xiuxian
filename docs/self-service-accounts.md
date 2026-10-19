# 用自己的账号：新增账号、选功能、装定时任务

仓库里的六个账号（`tg9100000001`…`06` / `demo_player_01`…`06`）只是示例，测试都跑在它们上面。
自己的账号和每个账号开哪些功能，写在一份配置里；定时任务、包装脚本和任务注册表都从这份配置生成。
不用手抄脚本，也不用改代码。

支持 macOS（LaunchAgent）和 Linux（systemd 用户单元）。Windows 请装 WSL2（应用商店里的 Ubuntu），
在 Ubuntu 里按 Linux 的步骤做。

## 准备

```sh
git clone <本仓库> xiuxian && cd xiuxian
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

- Python 3.11 或更高（依赖里的 networkx 3.6 要求 3.11）。
- 每个账号要有 Telegram API 凭据：在 <https://my.telegram.org> 的 API development tools 里建一个应用，拿到 `api_id` 和 `api_hash`。
- 每个账号都要设 Telegram 用户名：游戏回包靠 @用户名 认人。

## 最简单：本地网页

```sh
.venv/bin/python3 scripts/xiuxian_web.py
```

浏览器会打开 <http://127.0.0.1:8765>，在页面上按 1~5 的顺序填写：

1. **游戏群**：群 @用户名、群 id、指令话题 id、只读公告群 id。
2. **新增账号**：填 api_id、api_hash、手机号，点「发送验证码」；验证码会发到这个账号的 Telegram，填进页面（开了两步验证的再填密码），点「登录」。
   账号 id 和用户名会自动读取，API 凭据存进 `config/accounts/tg<id>.env`，会话存进 `state/tg<id>.session`。
3. **账号与功能**：给每个账号勾选功能，点「保存功能」。有选项的功能会多出下拉框，比如极阴祖师回复哪句、双修找哪个号。
4. **通知**（可选）。
5. **保存并应用**：生成任务并装到本机（macOS 或 Linux，页面会按本机系统自动选好）。

网页只监听本机，因为页面里会经手 API hash 和验证码；每次启动页面都会带一个新口令，别的网站无法替你操作它。
程序跑在 VPS 上时，在自己电脑执行 `ssh -L 8765:127.0.0.1:8765 <服务器>`，再在浏览器打开同一个地址。

网页做的事和下面的命令行完全一样，两种方式可以混用。

## 命令行

下面的命令都在仓库根目录执行，`X` 代表 `.venv/bin/python3 scripts/xiuxian_accounts.py`。
账号号写成 `tg<Telegram 数字用户 id>`，例如 `tg123…`。回执校验用的就是这个数字，不能随便起名。
不知道自己的 id 也没关系：先随便填一个加上，`login` 时会报出真实 id，再删掉重加即可。

### 1. 告诉程序游戏群在哪

```sh
X game --group <游戏群 @用户名，不带 @> --chat-id <-100 开头的群 id> \
       --topic-id <指令话题 id> --second-chat-id <只读公告群的 -100 id>
```

所有指令只会发到这个群的这个话题。话题 id 是话题里任意一条消息链接
`https://t.me/<群>/<话题 id>/<消息 id>` 中间那一段。没设置时用的是仓库里的占位值，什么都发不出去。

### 2. 新增账号

```sh
X add tg<你的用户id> --username my_name
```

这一步会建好 `config/accounts/tg<你的用户id>.env`。在里面填 `TG_API_ID` 和 `TG_API_HASH`，然后登录：

```sh
X login tg<你的用户id>
```

按提示输入手机号、Telegram 发来的验证码，开了两步验证的再输密码。
会话存在 `state/tg<你的用户id>.session`。登录后会核对 id 和用户名是否与配置一致。

几个账号共用一个 API 应用时，加 `--credential-source tg<那个账号>`，
这样就读那个账号的 `.env`，会话文件也用它的名字。

### 3. 选功能

```sh
X features                                   # 列出全部功能和可选项
X enable tg<你的用户id> shendubiguan yuanyingchuqiao tower journey fishing
X enable tg<你的用户id> 'jiyin_choice={"reply": ".献上魂魄"}'
X disable tg<你的用户id> fishing
X list
```

| 功能 | 说明 |
| --- | --- |
| `shendubiguan` / `yuanyingchuqiao` | 深度闭关、元婴出窍，到点自动续 |
| `tanxunliefeng` / `wendao` / `xuexishanlin` / `zhaohuanmoying` | 探寻裂缝、问道、血洗山林、召唤魔影 |
| `shenji_budao` | 神迹·布道 |
| `fumofabao_xuantianzhanlingjian` / `fumofabao_qingzhufengyunjian_shenlei` | 抚摸法宝（玄天斩灵剑只能一个账号开） |
| `jiaohuan_fabao` / `jiaohuan_gongfa` | 南陇侯交易，二选一 |
| `jiyin_choice` | 极阴祖师抉择；选项 `reply` 为 `.收敛气息`（默认）或 `.献上魂魄` |
| `xuangu_zuoda` | 玄骨上人答题 |
| `shuangxiu_wenyang` | 双修温养；必填 `partner`（另一个已添加的账号），只能一个账号发起 |
| `treasure_hunt` / `tianxing_exploration` | 洞府寻宝、天星宗深入游历（后者只能一个账号开） |
| `yuanshen` | 第二元神修炼与镇魔（只能一个账号开） |
| `qingyuanzi` | 青元子世界 Boss |
| `fishing` / `star_observatory` / `tower` / `journey` / `fate_cards` / `sect_daily` / `pavilion` | 垂钓、星宫灵圃、琉璃问心塔、游历、天机命脉、宗门日常、藏娇阁侍妾 |
| `account_lifecycle` | 肉身碎裂监控：碎裂后暂停该账号的群指令，夺舍成功后恢复 |

共用的日常（垂钓、问心塔、游历等）由一个调度进程按配置顺序轮流处理所有开了它的账号，
相邻账号错开几分钟进场。

有些设置命令行里没有，需要直接改 `config/accounts/accounts.json`：

- `bound_identity`：只让洞府里的某个角色跑星宫和游历，写在账号对象里，例如 `{"player_id": 123, "label": "道号"}`；
- `pavilion` 的 `routes`：给侍妾固定航线，例如 `{"南宫婉·月影": "月殿寻痕"}`；
- `fishing` 的 `special_bait`：设为 `true` 后，才允许在 `fishing/state/miniapp_fishing.json` 里给这个账号配特殊饵。

改完执行任一命令（比如 `X list`），配置就会被校验。

### 4. 生成并安装

```sh
X render                 # 每次改完配置都要执行
X install --launchd      # macOS
X install --systemd      # Linux；之后再执行一次 loginctl enable-linger $USER
```

`render` 会生成以下文件（都不会进 Git）：

- `config/accounts/active_flows.json`：这台机器的任务注册表；
- `config/accounts/game.env`：游戏群设置；
- `config/accounts/launchd/*.plist`：每个账号的任务；
- `scripts/*_tg<账号>.sh`：每个账号的包装脚本。

这些文件都照着仓库里跑同一功能的示例账号复制，只替换账号、用户名、凭据来源和功能选项，
所以行为和示例部署完全一致。

`install` 只装注册表里有的任务，上次装过而这次没有的会卸掉。

- macOS：任务写到 `~/Library/LaunchAgents`，用 `launchctl bootstrap` 加载。LaunchAgent 按**本机时区**触发，
  而游戏日按北京时间算，所以请把系统时区设为 Asia/Shanghai。
- Linux：任务写到 `~/.config/systemd/user`，一个任务对应一个 service；不常驻的再配一个 timer。
  每日任务的触发时刻固定按 Asia/Shanghai 计，和本机时区无关。

`install --dry-run` 只写文件、打印要执行的命令，不会加载。

## 通知（可选）

日报、战报这类汇总要通过 [Hermes](https://github.com/NousResearch/hermes-agent) 的 `hermes send` 发出：

```sh
X notify --target telegram:<你的 Telegram 用户 id> --hermes-bin ~/.hermes/hermes-agent/venv/bin/hermes
```

不设置就不发通知，自动化本身照常跑。

## 暂停、停用

- 临时停一个账号：`touch state/accounts/<账号>/pause_all`，详见 README 的 *Pausing* 一节。
- 不再跑某个功能：先 `X disable`，再 `X render`，最后执行一次 `X install`。

## 文件与隐私

账号、凭据、会话和运行状态只放在 `config/accounts/`（除 `account.env.example` 外整个目录被 gitignore）
和 `state/` 里。仓库里只放示例值。提交前运行 `python3 scripts/privacy_guard.py --all-history`。

## 没有纳入的部分

以下是原作者自用的流程，代码还在，但不在可选功能里：

- 天机试炼：调度在仓库外；
- 斗法轮转：对手和配额都写死了；
- 配方/稀有物品通知：接收人写死；
- `xiuxian_healthcheck`：只认 `launchctl`。

游历和天机命脉的 plist 里按六个账号写了六个整点触发。账号超过六个也能跑，多出来的由每 5 分钟一次的轮询接上。

## 测试

```sh
.venv/bin/python3 -m pytest -q
```

测试只用示例配置（见 `conftest.py`），不会读你的 `accounts.json`。
`test_account_config.py` 会拿每个功能的示例账号重新生成一遍，核对结果和仓库里的脚本、注册表逐字一致。
