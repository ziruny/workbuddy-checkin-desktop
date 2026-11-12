# WorkBuddy 签到助手（扫码登录 / 多账号 / 定时签到）

## 用哪个

**Windows**

| 方式 | 说明 |
|---|---|
| 双击 `dist\WorkBuddySignin.exe` | 单文件，免装 Python，拷走就能用（推荐） |
| 双击 `启动签到面板.bat` | 有 exe 就起 exe，没有就跑源码 |
| 重新打包 | 双击 `打包exe.bat`（约 30 秒，产物进 `dist\`） |

**macOS**

| 方式 | 说明 |
|---|---|
| 双击 `dist/WorkBuddySignin.app` | 打包产物，免装 Python |
| 双击 `启动签到面板.command` | 有 .app 就开 .app，没有就跑源码（首次需 `chmod +x`） |
| 重新打包 | `bash 打包mac.sh`（产物进 `dist/`） |

依赖：源码运行 `pip install pywebview`；打包再加 `pip install pyinstaller`（macOS 上 pywebview
会自动带上 pyobjc）。签到引擎只用标准库。

## 它做什么

| 能力 | 说明 |
|---|---|
| 扫码登录 | 点「扫码添加账号」→ 弹出官方登录页 → 微信扫码确认，账号自动进入列表 |
| 多账号 | 想加几个就重复扫几次；每个账号一份独立会话，登录态互不共享 |
| 一键签到 | 顶部「一键签到」把列表里的账号依次签一遍，单行也可单独签 |
| 定时签到 | 打开「每日自动」并选时间，面板开着就每天自动跑（错过会自动补） |
| 开机自启 | Windows 写 `HKCU\...\Run`；macOS 写 `~/Library/LaunchAgents/com.workbuddy.signin.plist` |

签到逻辑没有重写：仍然调用仓库根目录的 `signin.py`（通过 `_runner.py`），
接口变更时只更新 `signin.py`，面板不用动。

## 登录态怎么存

```
Windows: %APPDATA%\WorkBuddySignin\
macOS:   ~/Library/Application Support/WorkBuddySignin/
Linux:   ~/.config/WorkBuddySignin/
├── accounts.json          # 账号索引：备注、uid、有效期、上次结果
├── creds/<uid>.json       # 每个账号一份会话
├── settings.json          # 定时开关 / 时间 / 自启
└── app.log                # 运行日志
```

- 令牌默认能用 7 天左右，refreshToken 约 30 天；**过期前会自动续期**并回写，
  正常使用不需要重复扫码。
- 服务端若作废了某个账号的登录态（改密码、换设备、主动踢下线），该账号会提示
  「登录已过期」，重新扫一次即可，其它账号不受影响。
- Windows 上令牌走 DPAPI 加密（绑当前 Windows 用户）；macOS/Linux 没有标准库等价接口，
  退化为明文但文件权限收紧到 600，且放在用户私有目录。
- 「从本机导入」是备用入口：直接读取当前 WorkBuddy 桌面端的登录凭据，适合不想扫码时使用。

## 排查

- 面板打不开：命令行跑 `python desktop/app.py`（macOS 用 `python3`）看报错，或看上面的 `app.log`。
- 扫码后没反应：确认手机微信点了「允许」；二维码约 5 分钟过期，过期就取消重来。
- 提示缺少 pywebview：`pip install pywebview`（面板用它显示界面）。
- 想单独验证签到是否正常（不点界面）：
  `dist\WorkBuddySignin.exe --run-signin <uid> --out 结果.json`，uid 见 `accounts.json`。
- exe / .app 被杀软或 Gatekeeper 拦：PyInstaller 未签名产物的常见现象。
  Windows 加信任即可；macOS 提示「无法验证开发者」时（不是报毒，是没公证）：
  系统设置 → 隐私与安全性 → 拉到底点「仍要打开」，或执行
  `xattr -dr com.apple.quarantine /Applications/WorkBuddySignin.app`。
- 窗口关了但进程还在（macOS 历史问题）：现在关窗即退出，单实例锁会一起释放。
