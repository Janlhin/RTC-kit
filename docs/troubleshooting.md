# 排查（troubleshooting）

先记住一条顺序：**状态 → 触发 → 投递**。

"我什么都没收到"这句话里藏着三种完全不同的故障，从第 3 步开始查，会浪费一晚上。
下面四类失败，每类给一条命令，照顺序做完基本能定位到底坏在哪一层。

文中的 `<CODE_DIR>` 是 `rtc.py` 所在目录（Hermes 默认 `~/.hermes/scripts`，
Windows 是 `%LOCALAPPDATA%\hermes\scripts`）；`<WORKSPACE_DIR>` 是你的工作区
（默认 `~/.hermes` 之外的独立目录，例如 `C:\Users\<你>\RTC`）。

---

## 0. 先看全貌（30 秒）

```bash
python <CODE_DIR>/rtc.py --workspace <WORKSPACE_DIR> brief
```

这一页会告诉你：现在在响什么、接下来还有几点、今天已过了什么。

- 有内容、时刻也对 → 状态层是好的，问题在第 2 或第 3 层
- 一片空白 / 报错 → 直接跳到第 3 类（状态坏了）

---

## 1. 没触发：到点了，但什么都不响，也没有报错

**一条命令：**

```bash
hermes cron list
```

**看什么：**

| 现象 | 含义 | 修法 |
|---|---|---|
| 只有 `rtc-plan`，没有 `rtc-due` | 每 5 分钟那半条根本没建 | 重跑 `install.py --setup-cron`，或手动 `hermes cron create "*/5 * * * *" --name rtc-due ...` |
| 有两条，但 `Next run` 是几小时前 | 网关没在跑，cron 不会自己走 | `hermes gateway status`，不跑就 `hermes gateway restart` |
| `Last run` 显示 `ran, but ... catch-up` | 补跑发生在窗口之外，等于没响 | 正常，等下个窗口即可 |

`Last run` 是判断"触发层活不活"的唯一权威，别用"我觉得它应该响"代替。

---

## 2. 触发了但没投递：日志说跑了，手机没收到

**一条命令：**

```bash
hermes cron runs <rtc-due 的任务 id> --limit 5
```

**看什么：**

- `ok` + 你确实没收到 → 看渠道那边，见下
- `ran, but the result was not delivered (… iLink sendmessage session not ready: ret=-2 …, prepare failed)` → **微信侧会话过期**。RTC 没错，是适配器没拿到可用的会话。

**微信这一类的固定解法：** 用你自己的微信**主动给机器人发一句话**（随便一个字），
这会刷新 `context-tokens.json` 的会话，出站就恢复。发完再等一个 5 分钟刻度。

**为什么是这个原因：** iLink 的出站会话靠"用户先说过话"维持，纯机器人单向推送会过期。
这是渠道特性，不是提醒系统的缺陷——但产品必须把它变成 install 的一步自检，
而不是让用户自己去猜。

需要看原始证据时：

```bash
# Windows：把它换成你的日志路径
grep "delivered to" %LOCALAPPDATA%\hermes\logs\agent.log
```

出现 `delivered to weixin:... via live adapter` 就是真送出去了。

---

## 3. 状态坏了：所有命令都报错，或行为诡异

**一条命令：**

```bash
python <CODE_DIR>/rtc.py --workspace <WORKSPACE_DIR> status --json
```

**看退出码：**

| 退出码 | 含义 | 下一步 |
|---|---|---|
| `0` | 正常 | 不是这一类问题 |
| `3` | 某个文件坏了（会点名文件与行号） | 按报错里的路径去看那个文件 |
| `2` | 并发写冲突（`rev` 对不上） | 停手，别手改；等下一次 `plan` 覆盖 |
| `4` | 没有这个窗口 | 命令写错了，或那天确实没这个窗口 |

**`3` 的两条铁律：**

1. **原文不会被覆盖。** 坏文件留在原地供你看，修复靠你改它，不要删了重建。
2. **不要手改 `state/alarms.json`。** 它有 `rev` 校验，手改会让下一次写入判成冲突。

常见触发：`routines.json` 被写成 `[]`（JSON 合法但形状不对）、
用了中文引号、`dateTypes` 里放了不存在的日期类型。这三种都会被点名到行。

---

## 4. 模型乱答：问它"我今天要干嘛"，它说"你没配置"

这是**四类里最误导人的一类**——系统其实好好的，只是模型看错了文件。

**一条命令：**

```bash
hermes project list
```

**判断：**

| 现象 | 含义 | 修法 |
|---|---|---|
| `No projects yet` | 会话没有工作区，模型只能在宿主默认目录里翻，翻到 `routines.example.md` 这类模板，于是告诉你"未配置" | `hermes project create rtc "<WORKSPACE_DIR>" --use`，然后 `hermes gateway restart` |
| 有项目但 `primary` 不是你的工作区 | 绑错地方了 | `hermes project set-primary <项目> "<WORKSPACE_DIR>"` |
| 项目对、仍答错 | 模型没读 `AGENTS.md` 的行程区块 | 见下一节 |

**为什么模板会骗人：** 仓库里 `rules/routines.example.md` 是**说明书**（11 KB，
带各种提醒类型的写法），不是运行配置。真配置只有一份：`rules/routines.json`。
模型读到前者会非常自信地宣布"你什么都没配"——这句话本身没有任何依据。

**硬化办法（安装器已做，手工部署时要补）：**

1. 把 `routines.example.md` 从宿主目录里挪走，别让它躺在模型够得着的地方；
2. 让工作区有 `AGENTS.md`，里面有一块会自动刷新的行程摘要（`brief` 写的）；
3. 项目绑定 + 重启网关，会话 cwd 才会落在真实数据旁边。

---

## 5. 别按页脚提示停掉 rtc-due

每一条提醒末尾宿主都会自动加一段英文页脚：

```
To stop or manage this job, send me a new message (e.g. "stop reminder rtc-due").
```

**照它做会把这个系统关掉。** `rtc-due` 是每 5 分钟检查一次的那半条——它停了，
所有提醒一起不响；`rtc-plan` 停了，第二天干脆没有时刻表。

- 想说「这条我看到了」→ 回「知道了 / 好了」，走 `ack`，只停今天这一次；
- 想说「以后都别提醒我」→ 用 `disable --window <id>`，只停这一类；
- 真想整个卸载 → 走 `install.py` 的反向步骤，别一条条停 cron。

页脚是宿主的行为，RTC 改不了它；能做的只是在这里写清楚，并把这条写进技能禁令。

---

## 附：一张对照表

| 你看到的 | 真正坏的层 | 先跑这条 |
|---|---|---|
| 到点没动静，日志也没有 | 触发（cron / 网关） | `hermes cron list` |
| 日志说跑了，手机没收到 | 投递（渠道会话） | `hermes cron runs <id> --limit 5` |
| 命令报错 / 状态诡异 | 状态（文件 / rev） | `rtc.py status --json` |
| 问行程它说"没配置" | 模型（找错文件） | `hermes project list` |

装完想一次性验证四层都通，跑：

```bash
python install.py --workspace "<WORKSPACE_DIR>" --smoke
```

它会在两分钟后给你一条测试提醒；收不到就按上面四步走。

---

## 6. 时刻表被"改过"或"写坏了"

两种症状，同一层原因：**有东西绕过了 `rtc.py` 直接写 `rules/routines.json`**。
实机上这就是模型干的——它不去跑命令，而是照着技能里的 JSON 模板直接改文件。
现在技能里已经没有模板了，你还可以再加一道锁（见本节末）。

### 6.1 文件被改过（内容还在，但窗口数不对）

`plan` 会当场告诉你：

```
⚠️ routines.json 被 RTC 之外的东西改过（上次程序写出 4 个窗口，现在是 1 个）；
原件已备份到 state/backups/routines.handedit-20261116-155512.json。
```

- 想知道现在到底有几个窗口：`rtc.py status`
- 想回到程序上一份：`state/backups/routines.program-last.json`
- 动手改之前先看清备份，别二次覆盖

### 6.2 文件被写坏（0 字节 / 缺字段 / 不是 JSON）

`plan` 和 `due` 都会失败，**但程序会自己恢复**：

```
⚠️ routines.json 被写坏了，已从程序底稿自动恢复（6 个窗口），提醒继续正常；
坏件留在 state/backups/routines.corrupt-20261116-155801.json。
```

恢复不了（连底稿都没了）才是真故障，那时会出退出码 3 和一句诊断。

### 6.3 从根上堵：把写文件的工具关掉

模型手改文件靠的是宿主的文件工具。某平台可以单独关掉这一组：

```bash
hermes tools disable --platform weixin file
hermes tools list        # 确认 file 不在启用列表里
hermes gateway restart   # 配置生效
```

关掉之后它只剩命令可用——要么跑 `rtc.py` / `rtc_once.py`（对），
要么说"做不到"（也是对的）；两种情况都比"说已创建、其实没写"强。

> 什么时候不该关：你需要它在对话里替你改 `tasks.md`、读 `AGENTS.md` 做摘要时。
> 那时保留 `file`，靠 6.1 / 6.2 的印章和自愈兜底。

## 7. 档案只有一份：多盘本地备份

`state/schedule/<日期>.json` 是永久档案，但默认只存在**一块盘**上。那块盘坏了、系统重装了，
一年后想查"那天几点出门"就什么都查不到。所以每天 `rtc-plan` 会顺手备份一次。

**存哪儿**：另一块本地盘上的 `RTC-Backup` 目录（不写源盘——同盘不算备份）。
没有 `rules/backup.json` 时，程序自动挑除源盘以外的固定盘：

```
D:\RTC-Backup\RTC-20261116.zip
E:\RTC-Backup\RTC-20261116.zip
```

**里面装了什么**：`rules/routines.json`、`state/alarms.json`、`state/routine-seal.json`、
全部 `state/schedule/*.json`、`state/backups/*.json`、`AGENTS.md`、`tasks.md`。

**多久一次、留几份**：每天一次（同一天重复跑不会重写），每个盘留最近 30 份（约 30 KB/天）。

**想改目标盘或保留天数**，写一份 `rules/backup.json`：

```json
{"targets": ["D:\\RTC-Backup", "E:\\RTC-Backup"], "keep": 30}
```

### 7.1 手动跑一次 / 看看到底备了没

```bash
python code/rtc.py --workspace <工作区> backup
python code/rtc.py --workspace <工作区> backup --to D:\RTC-Backup --keep 60
```

最后跑过哪天、写到哪、有没有失败，都在 `state/.backup-last.json` 里。

### 7.2 丢了盘怎么恢复

翻出最近一份 zip，解开，把里面的目录按原样盖回工作区即可：

```powershell
Expand-Archive -LiteralPath "D:\RTC-Backup\RTC-20261116.zip" -DestinationPath "C:\Temp\rtc-restore"
Copy-Item -Recurse -Force "C:\Temp\rtc-restore\state" "C:\Users\<你>\RTC\"
Copy-Item -Force "C:\Temp\rtc-restore\rules\routines.json" "C:\Users\<你>\RTC\rules\"
```

恢复完跑一次 `rtc.py --workspace <工作区> status` 和 `history --month <当月>`，确认窗口和
历史都对得上。

> 备份是**整份快照**，不是增量。恢复时先解开看一眼 `state/schedule/` 里有哪些日期，
> 再决定整体盖回还是只挑需要的日子复制过去。
