# 小红书发布软件

本地 Web 工作台，负责：

- 递归扫描素材根目录，识别直接包含图片的发布文件夹，并保留年份 / 月份 / 相对层级信息。
- 自动选一组图片。
- 生成 5 种小红书文案：夸赞型、专业展示型、过程分享型、观点讨论型、案例故事型。
- 将已确认的定时任务、账号 Profile、排期和执行状态保存到 SQLite，支持刷新和重启后恢复。
- 支持 DeepSeek 文案生成，失败时自动回落本地模板。
- 支持按当前 Chrome Profile 保存独立账号画像，可读取最近 20 条笔记并结合人工关键词生成画像。
- 支持超过 15MB 图片自动生成压缩副本。
- 调用 `tool-eval/xiaohongshu-skills` 自动填入小红书发布页。
- 定时发布兼容原生 `datetime-local` 和旧版文本控件，并在提交前校验页面实际保留的时间。

当前版本：`2026.11.6`。自动化测试覆盖目录扫描、SQLite 队列、发布状态确认、定时控件、账号画像和更新包校验。真实小红书页面测试记录单独保留在开发机，不随公开版本发布。

## 公开下载与更新

- GitHub 项目：[小红书发布软件](https://github.com/lengjueqi-coder/xiaohongshu-publisher)
- 首次安装：从 [Releases](https://github.com/lengjueqi-coder/xiaohongshu-publisher/releases/latest) 下载 `xhs-assistant-mac.zip`，解压后运行 `install.command`，再运行 `start.command`。
- 后续更新：软件启动时会检查新版本；也可以点工作台的下载图标手动检查。点击“下载并重启”后会验证 SHA-256、保留本机数据、替换应用文件并重启服务。
- 本仓库和 Release 是公开的，下载不需要 GitHub 账号。公开仓库不包含 `data/`、`config/`、API Key、SQLite 数据库、浏览器登录态、校准数据或发布素材。
- 每次发布新版本时，先同步更新 `app/server.py`、`start.command` 和本节版本号，提交并推送 `main`，再推送形如 `v2026.11.7` 的版本标签。GitHub Actions 会运行测试、构建 ZIP 与 SHA-256 文件并创建 Release。`2026.11.6` 之前安装的版本须手动安装一次 `2026.11.6`，之后即可在软件内更新。

安全边界：

- 不保存小红书账号密码。
- API Key 不再硬编码在源码中；可信设备之间复制 `data/assistant.sqlite3` 会携带本地已保存配置，因此不要把该文件发给不信任的人。
- 普通“填入发布页”不会点击最终发布；只有用户明确点击“开始提交”后，后台队列才会提交已逐条确认的任务。
- 不绕过验证码、安全验证或平台风控。
- 不以随机点击或伪装保证“绕过”平台风控；软件采用账号级上限、串行提交、随机间隔、失败暂停和状态不明不重试等合规的风险控制。
- 当前已稳定填入：图片、标题、正文、话题、原创声明、可见范围。
- 专辑和地点已做 best-effort 自动尝试；如果小红书页面未响应，先由用户手动选择并用于后续校准。
- 真实发布必须以小红书页面确认或管理页结果为准，CLI 返回成功本身不算平台已接收。

## 启动

双击：

```text
start.command
```

或命令行：

```bash
cd /你的解压路径/xhs-assistant
python3 app/server.py
```

新电脑首次使用请先运行 `install.command`，它会创建本地虚拟环境并按照 `requirements-mac.txt` 安装已验证的 Bridge 依赖。完整迁移与 Mac M 芯片安装步骤见：`安装教程-Mac-M芯片.md`。

打开：

```text
http://127.0.0.1:5818
```

如果更新文件后仍看到旧界面，先双击 `stop.command`，再运行 `start.command`。启动脚本会通过 `/api/version` 检查服务版本，避免把旧进程误当成新版本。

## 使用前准备

1. Chrome 扩展页保持 `XHS Bridge` 已启用。
2. Chrome 里保持小红书已登录。
3. 如果登录失效，在工作台点击“检测登录”，按提示扫码。

## 验证

```bash
cd /你的解压路径/xhs-assistant
python3 -m unittest discover -s app -p '*tests.py' -v
```

## 文件位置

- 工作台代码：当前解压目录
- 数据库：`data/assistant.sqlite3`
- 小红书桥接工具：`app/vendor/xiaohongshu-skills`
