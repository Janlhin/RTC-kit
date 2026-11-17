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

### 1.1 表里只剩一次性提醒（今天能响、明天不响）

**一条命令：**

```bash
python rtc.py --workspace <ws> status
```

看到 `⚠ 时刻表里没有日常窗口（一次性 N 条）`，或者 `status --json` 里 `timetable.recurring` 是 0，
就说明时刻表被清成了只剩 `once-*`：今天那一条照响，明天 `rtc-plan` 排不出日常工作窗口。

| 现象 | 含义 | 修法 |
|---|---|---|
| `plan` 的 stderr 有"时刻表里只有一次性提醒" | 同上，只是从计划侧报出来 | 按 `rules/routines.md` 把日常窗口补回去，再 `plan --force` |
| `install.py --smoke` 直接判失败并提示"没有日常窗口" | 自检拒绝给假绿灯 | 先补表，再重跑 `--smoke` |

**注意**：这三个入口都只是"报警"，不会替你补表——时刻表是你的作息，脚本不猜。

### 1.2 周一早报没来，或者数字看不懂

周报**不是单独的定时任务**，它挂在你本来就有的 `rtc-plan` 上（每天 06:00 那次）。
所以第一条永远是先确认 `rtc-plan` 今天跑过没有。

| 现象 | 含义 | 修法 |
|---|---|---|
| 周一没有 `📊 上周（…）` | 那天 `rtc-plan` 没跑，或这一周已经发过 | 看宿主里的 `rtc-plan` 上次运行时间；`state/.weekly-last.json` 里的 `week` 就是"已发过的那一周" |
| 周二到周日都没有 | 正常 | 设计如此：只在周一输出，其他日子一个字都不打 |
| 「上周记录太少（N 个窗口）」 | 上周 `metrics.db` 里少于 3 条记录，算百分比会误导 | 不用管；跑满一周自然就有 |
| 数字和记忆不符 | 周报只读 `metrics.db`，它由每天 `plan` 同步 | 核对 `state/metrics.db` 的 `daily` 表；别手改库，改坏了删掉让 `plan` 重建 |

**口径**：`提醒 N 次` 数的是"该提醒你几件事"（一个窗口一天算一次），不是每个窗口里 5 分钟一响的重复次数。

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
全部 `state/schedule/*.json`、`state/backups/*.json`、**`state/metrics.db` 与它的文本导出
`state/metrics.sql`**、`AGENTS.md`、`tasks.md`。

> metrics 以前要手动跑 `rtc.py metrics sync`，现在 `plan` 每天顺手同步一次，
> 所以「这周漏了几次」这类统计从第一天起就有数据。`.db` 是射频库，`.sql` 是它的文本版
> ——git 里看 `.sql` 才知道今天多了哪几行。

**单独跑 `code/metrics.mjs` 时的两个坑**：

1. **必须带 `--state <工作区>/state`**。脚本默认只认自己所在目录，裸跑会去找 `<代码目录>/alarms.json`，找不到就打印「metrics 失败：找不到 …」并结束。
2. **失败时退出码仍是 0**——消息走 stdout。所以判断它有没有成功，要看输出里有没有「失败」，不能看 `$LASTEXITCODE`。日常不用它：`plan` 已经内置同步，且那条路径有测试覆盖。

**哪些东西不会进备份**（`BACKUP_FILES` 是显式白名单，不是「打包整个目录」）：

| 不进备份 | 为什么 |
|---|---|
| `state/gateway.heartbeat`、`gateway.lifecycle.json`、`gateway.start-attestation.json`、`dashboard_clients.heartbeat` | 这些是**宿主网关写的**，只是目录共享才落在 `state/`。RTC 自己的心跳是 `state/heartbeat.json` |
| `state/backups/routines.<时间>.json` 这类手抄副本 | 只收程序自己写的三类：`routines.program-last.json`、`routines.handedit-*`、`routines.corrupt-*`。否则归档里会出现两份 routines，没人分得清哪份是真的 |
| `state/backups/_model-made/` | 模型或人手动 `cp` 的东西统一隔离在这，永不归档 |
| `state/archive/` | 一次性归档（如模型写的 issues 快照），不进每日备份 |

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

不用去翻文件，`status` 最后一行就是这件事的答案：

```bash
python code/rtc.py --workspace <工作区> status
```

```
  备份  11-17 00:00  本地 2 处 · git 已推送 · 最近无错误
```

没备份过显示「尚未备份」，部分失败会写「错误 1：E: 盘不可写」。
备份状态不影响 `status` 的退出码——提醒是主链路，备份是附属品。

### 7.2 丢了盘怎么恢复

翻出最近一份 zip，解开，把里面的目录按原样盖回工作区即可：

```powershell
Expand-Archive -LiteralPath "D:\RTC-Backup\RTC-20261116.zip" -DestinationPath "C:\Temp\rtc-restore"
Copy-Item -Recurse -Force "C:\Temp\rtc-restore\state" "C:\Users\<你>\RTC\"
Copy-Item -Force "C:\Temp\rtc-restore\rules\routines.json" "C:\Users\<你>\RTC\rules\"
```

恢复完跑一次 `rtc.py --workspace <工作区> status` 和 `history --month <当月>`，确认窗口和
历史都对得上。

完整的恢复流程（含「这份备份是不是我的」验证、只读试跑、对账、演练记录）见
[RESTORE.md](RESTORE.md)。

> 备份是**整份快照**，不是增量。恢复时先解开看一眼 `state/schedule/` 里有哪些日期，
> 再决定整体盖回还是只挑需要的日子复制过去。

## 8. 第二种备份：把档案提交进私人 git 仓库

zip 只防「盘坏」，不防「机器没了 / 换电脑」。想再稳一层，就把档案按天提交进一个**私人**仓库。
远端随便写——GitHub、Gitee、GitLab、自建 Gitea、局域网裸库、甚至 U 盘上的 `git init --bare`，
用的都是同一条命令。

### 8.1 配一次

在 `rules/backup.json` 里加一段 `git`（和 `targets` 并存，两个都会跑）：

```json
{
  "targets": ["D:\\RTC-Backup", "E:\\RTC-Backup"],
  "keep": 30,
  "git": {
    "dir": "C:\\Users\\<你>\\RTC-archive",
    "remotes": ["git@github.com:<你>/rtc-archive.git"],
    "branch": "main"
  }
}
```

`dir` 是本地镜像仓库（每天往 `archive/<日期>/` 里放一份档案，提交后推送）。
`remotes` 可以写多个，一次推多处。

里面除了 `rules/`、`state/schedule/`，还包括 `state/metrics.db` 和它的文本导出
`state/metrics.sql`——**你要的「SQL 与 DB 日志」就在这两份里**：`.db` 可以直接
`sqlite3` 打开，`.sql` 在 git 里能看出每天多了哪几行。

### 8.2 各种仓库怎么写地址

| 仓库 | remotes 里写什么 | 认证 |
|---|---|---|
| GitHub 私有库 | `https://github.com/<你>/rtc-archive.git` | Fine-grained token，或 SSH |
| Gitee | `https://gitee.com/<你>/rtc-archive.git` | 私人令牌，或 SSH |
| GitLab | `https://gitlab.com/<你>/rtc-archive.git` | Project access token，或 SSH |
| 自建 Gitea | `https://你的域名/<你>/rtc-archive.git` | 同上 |
| SSH（任意平台） | `git@github.com:<你>/rtc-archive.git` | 已配好的 ssh key |
| 局域网/本机裸库 | `D:\\RTC-archive.git` 或 `\\\\nas\\rtc\\archive.git` | 无 |

> https 走的是系统 git 的凭据：先把 token 存进 Git Credential Manager，或在 URL 里带
> `https://<user>:<token>@host/...`（后者会留在配置文件里，别提交到任何仓库）。

### 8.3 跑一次 / 看结果

```bash
python code/rtc.py --workspace <工作区> gitarchive
python code/rtc.py --workspace <工作区> gitarchive --remote "git@gitee.com:<你>/rtc-archive.git"
```

成功会说「已提交并推送」；内容没变化会说「内容没变化，跳过」；推不上去会返回**退出码 3**
并说明原因——**不会假装成功**。

每天 `rtc-plan` 会自己跑一次（和 zip 同一处，同一天只跑一次），平时完全静默。

### 8.4 从仓库里恢复

```bash
git clone <仓库地址> C:\Temp\rtc-restore
# archive/2026-11-16/ 就是一个完整的当天快照
```

挑需要的日期，把 `rules/routines.json` 和 `state/` 盖回工作区即可。

> 隐私提醒：这个仓库里会有你的作息、提醒文案、确认原文（`alarms.json` 的 `ackText`）。
> **务必用私有库**；公开库等于把这些发到互联网上。

---

## 9. 出差 / 生病 / 开会：整段静音

逐条 `disable` 再逐条 `enable` 是最容易忘的方式。要停一片日子，用 `pause`：

```bash
python code/rtc.py --workspace <工作区> pause --days 3
python code/rtc.py --workspace <工作区> pause --until 2026-11-20 --reason 出差
python code/rtc.py --workspace <工作区> resume
python code/rtc.py --workspace <工作区> status
```

| 行为 | 说明 |
|---|---|
| 停在哪 | `state/paused.json`（**不写 `routines.json`**，所以印章和审计不会把暂停当成手改时刻表） |
| 暂停期间 | 当天窗口直接收掉，`due` 什么都不发；`plan` 每天照跑、照写快照 |
| 到期 | `until` 那天自动恢复，不需要手动 `resume` |
| 恢复时 | 补一句「11-17 ~ 11-19 暂停了 3 天，跳过 7 次提醒」，数字从每日快照里数 |
| 标记坏了 | 当作没暂停，stderr 提示一句，照常响——宁可多响，不因坏文件集体静默 |
| 想先看一眼 | `simulate --date 2026-11-18` 会显示 0 次，并在「跳过」里写「这段时间已暂停」 |

**注意**：`pause` 只影响提醒，不影响 `tasks.md` 和档案备份。


