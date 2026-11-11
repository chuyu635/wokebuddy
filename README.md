# WorkBuddy 每日自动签到

每天自动领 WorkBuddy 的 100 积分，**不需要开着电脑**——签到跑在 GitHub 的服务器上，到点自己执行，成功或失败都会留下记录（失败还会发邮件给你）。

```
GitHub Actions 定时触发（北京时间 09:10 主跑 / 19:30 兜底）
        │
        ├─ 1. POST /v2/billing/meter/checkin-activity-status   先问：今天签了吗？
        │      已签 → 直接退出（幂等，绝不重复调用领取接口）
        │      未签 ↓
        ├─ 2. POST /v2/billing/meter/daily-checkin            领取 100 积分
        ├─ 3. 再查一次状态                                     复核真的到账
        └─ 4. 发通知（可选）；失败时退出码非 0，GitHub 自动发邮件
```

## 接口（从客户端 5.11.2 里实测出来的）

| 项目 | 值 |
| --- | --- |
| 域名 | `https://www.workbuddy.cn` |
| 查状态 | `POST /v2/billing/meter/checkin-activity-status` |
| 领取 | `POST /v2/billing/meter/daily-checkin` |
| 认证 | `Authorization: Bearer <JWT>` + `X-User-Id` + `X-Domain: www.workbuddy.cn` |
| 已领过 | `HTTP 400` + `{"code":10001,"msg":"今天已签到，请明天再来"}` |
| 登录态有效期 | 约 30 天（JWT 的 `exp`） |

> 网上流传的 `copilot.tencent.com` + `/billing/meter/checkin-status` 是旧版，实测已经不对了。

## 目录

| 文件 | 作用 |
| --- | --- |
| `wb_checkin.py` | 主程序，单文件零依赖 |
| `1-抓取登录态.bat` | 双击：拉起客户端 → 抓 token → 写 `config.json` → 复制到剪贴板 |
| `2-试跑签到.bat` | 双击：本机跑一次完整签到（查状态 → 领取 → 复核） |
| `3-启动调试模式.bat` | 手动用调试端口启动客户端（`1-` 内部也会用） |
| `tools/` | 抓取与调试用的脚本（CDP 协议、钩子注入等） |
| `.github/workflows/daily-checkin.yml` | 云端定时任务 |
| `scf_handler.py` | 腾讯云函数入口（备选方案） |
| `tests/` | 18 个离线用例，用本地假接口覆盖各分支 |
| `config.json` | 本机凭证，自动生成，**已在 .gitignore，别提交** |

---

## 第一步：抓一次登录态（约 1 分钟）

1. **先完全退出 WorkBuddy**：右下角托盘图标 → 右键 → 退出（只关窗口不算，单实例会让调试端口起不来）
2. 双击 **`1-抓取登录态.bat`**

它会自动：用调试端口拉起客户端 → 等端口就绪 → 注入钩子并重载页面（界面闪一下，正常）→ 抓出 `Authorization` 头 → 写入 `config.json` → **把 token 复制到剪贴板**。

成功时会打印：

```
[+] 抓取成功，已写入 ...\config.json
    昵称     : 长江二桥
    user_id  : c8976dcd-...
    token    : eyJhbGciOiJS…EUZw (1574 字符)
    到期时间 : 2026-12-11 19:31
```

## 第二步：本机验证（约 30 秒）

双击 **`2-试跑签到.bat`**，正常输出：

```
[OK] 服务端状态：today_checked_in=False streak_days=5 today_credit=100 total_credits=500
[OK] 领取成功：+100 积分，连签 6 天
[OK] 复核通过：today_checked_in=true ...
```

如果今天已经领过，会输出 `今天已经签过了（连签 N 天），不重复领取` —— 这同样算成功（退出码 0）。

---

## 第三步：搬到云端（GitHub Actions，免费）

**3.1 建仓库**：https://github.com/new → 名字 `workbuddy-checkin` → 选 **Private** → Create

**3.2 传文件**：把 [workbuddy-checkin.zip](workbuddy-checkin.zip) 解压，在仓库页点 `uploading an existing file`，把解压出来的内容拖进去（根目录要能看到 `wb_checkin.py` 和 `.github`），Commit。

> 千万别传 `config.json`——里面有你的登录态。

**3.3 加密钥**：仓库 → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`

| Name | 值 |
| --- | --- |
| `WB_ACCESS_TOKEN` | `1-抓取登录态.bat` 已复制到剪贴板，直接粘贴 |
| `WB_USER_ID` | `c8976dcd-b5ab-404a-8699-6c628881c0f5`（脚本也会打印） |

**3.4 启用并试跑**：仓库 → `Actions` → 左侧 `WorkBuddy 每日签到` → `Run workflow`。几十秒后日志出现 `领取成功` 或 `今天已经签过了` 即成功。

之后每天北京时间 09:10 自动跑，19:30 再兜一次。

---

## 续期：每 30 天一次（2 分钟）

token 到期前 7 天，脚本会在日志里打 `[WARN] 登录态只剩 N 天`，通知里也会带到期日；**过期当天任务会失败，GitHub 会给你发邮件**，不会静默失败。

续期步骤：

1. 托盘退出 WorkBuddy → 双击 `1-抓取登录态.bat`（token 自动进剪贴板）
2. 打开 `Settings → Secrets → WB_ACCESS_TOKEN` → Update → 粘贴 → 保存

> 想彻底免掉这步，需要客户端主进程里的 refreshToken；它被加密存放、只走内部 IPC，脚本拿不到，所以这一步只能手动。这是官方登录态的限制。

## 通知（可选）

在 Secrets 里再加三项即可：

| `WB_NOTIFY_TYPE` | `WB_NOTIFY_URL` | 备注 |
| --- | --- | --- |
| `wecom` | 企业微信群机器人 webhook | |
| `dingtalk` | 钉钉群机器人 webhook | |
| `serverchan` | `https://sctapi.ftqq.com/<SendKey>.send` | |
| `bark` | `https://api.day.app/<Key>` | iOS |
| `telegram` | `https://api.telegram.org/bot<T>/sendMessage` | 另需 `WB_NOTIFY_CHAT_ID` |

不配也不影响签到：GitHub Actions 失败本身就会发邮件。

## 常见问题

**能不开电脑吗？** 能。签到在 GitHub 的机器上跑，你电脑只用于每 30 天抓一次 token。

**会重复领吗？** 不会。先查 `checkin-activity-status`，`today_checked_in=true` 就直接退出；服务端本身也按天幂等（重复领取返回 10001）。

**会封号吗？** 只调用客户端自己用的接口、单账号、每天一次，特征和手动点一下一致，风险很低。但自动化操作可能不完全符合用户协议，自行判断，别拿主账号试。

**401/403 怎么办？** 登录态失效，重做「续期」四步。退出码 3 就是这个含义。

**Actions 没准点跑？** GitHub 的 cron 高峰期会延迟几分钟到一小时，签到不要求准点。

**仓库 60 天没动静会怎样？** GitHub 会自动暂停定时工作流，去 Actions 页面点一次 `Run workflow` 即可恢复。建议每月续期时顺手看一眼。

**想改时间？** 改 `.github/workflows/daily-checkin.yml` 里的 cron。GitHub 用 UTC，北京时间减 8 小时：09:10 → `10 1 * * *`。

## 自测

不联网、不碰真实账号：

```bash
python tests/test_wb_checkin.py
```

18 个用例：正常领取、已签跳过、接口返回 10001、401、500、业务失败、演练模式、只读模式、请求头（`X-Domain`/`X-User-Id`/`X-Request-ID`）、JWT 过期判断、token 脱敏。

## 边界

- 个人自用：只处理你自己的账号，不涉及批量注册、多账号、绕过验证码。
- 依赖客户端内部接口，官方改版可能失效；失效了抓包重测即可。
- 日志里 token 永远脱敏（`前12位…后4位`），不会外发到除官方接口和你的通知 webhook 之外的地址。
