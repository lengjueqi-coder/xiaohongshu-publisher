# 小红书发布助手：Mac M 芯片安装教程

本软件按 macOS + Apple Silicon（M1/M2/M3/M4）设计。程序和每台电脑自己的 SQLite 队列、账号画像与配置分开保存。每台电脑需要手动安装一次运行环境；完成首次安装后，软件可检查并安装后续版本。

本地递归扫描、年份/月目录识别、SQLite 队列、定时控件值校验、平台确认判定和批量任务串行逻辑由自动化测试覆盖。自动化测试不代表小红书页面已真实发布成功；发布结果必须以已登录 Chrome 会话中的平台确认或创作者后台记录为准。

## 一、准备环境

每台电脑需要：

- macOS；推荐 Apple Silicon Mac。
- Python 3.11 或更高版本。
- Google Chrome。
- 可以访问小红书和 AI 接口的网络。

首次安装请从 [GitHub Releases](https://github.com/lengjueqi-coder/xiaohongshu-publisher/releases/latest) 下载 `xhs-assistant-mac.zip` 并解压。发布包包含：

```text
app/
moments/
start.command
install.command
stop.command
requirements-mac.txt
README.md
```

发布包不会包含你的数据库、API Key、素材、浏览器登录态或校准文件。每个人首次启动后会生成自己的本地数据；不要把包含 API Key 的 `data/assistant.sqlite3` 发到公开仓库或不信任的人手里。

## 二、一键安装

1. 把完整的 `xhs-assistant` 文件夹复制到目标 Mac，例如桌面。
2. 双击 `install.command`。
3. 如果 macOS 提示无法打开，右键 `install.command`，选择“打开”，再确认打开。
4. 安装脚本会创建本地 `.venv`，安装小红书 Bridge 运行依赖和 Pillow。

安装过程需要联网下载 Python 依赖。安装完成后，脚本会提示扩展目录位置。Chrome 扩展不能由普通脚本静默安装，这是 Chrome 的安全限制，所以这一步仍需在每台电脑上手动完成一次。

## 三、安装 XHS Bridge 扩展

这是每台电脑必须做一次的浏览器准备：

1. 打开 Chrome，访问 `chrome://extensions/`。
2. 打开右上角“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择：

```text
xhs-assistant/app/vendor/xiaohongshu-skills/extension
```

5. 保持 XHS Bridge 扩展启用。
6. 在 Chrome 中登录小红书。

Chrome 的登录态绑定在具体电脑的 Chrome Profile 中，不能只靠复制 SQLite 数据库迁移。每个目标 Mac 需要分别登录一次；登录后软件可以识别 Default、Profile 1 等多个 Chrome Profile。

建议先在要使用的小红书 Chrome Profile 中打开创作者平台发布页，再启动软件。Bridge 连接的是当前浏览器 Profile，不要在提交任务时切换 Profile 或关闭 Chrome。

## 四、启动软件

双击：

```text
start.command
```

软件会启动本地服务并打开：

```text
http://127.0.0.1:5818
```

以后通常只需要双击 `start.command`。脚本会检查版本探针；如果 `5818` 上仍是旧版本，会提示先双击 `stop.command`，避免误用旧代码。

### 后续一键更新

软件启动时、前台每 10 分钟和重新回到软件时会自动检查 GitHub 上的正式 Release，也可点击顶部固定栏的“检查更新”按钮。提示条和按钮不受分栏拖动影响。发现新版本后点击“一键更新并重启”，软件会下载更新包、校验 SHA-256、保留本机 `data/`、`config/`、`.venv/` 和坐标校准文件，然后替换程序并重新启动。队列运行或正在提交时会拒绝更新；先暂停队列并等待当前操作结束。

`2026.11.6` 可通过原有下载图标升级到本版；更早的版本须从 Releases 手动安装一次新版。软件检查的是带 ZIP 和 SHA-256 文件的正式 Release，仅推送源码不会出现新版本。安装更新时需要能连接 GitHub；不要在更新过程中强制退出软件或关机。

## 五、首次设置

1. 在“后台设置”中修改素材根目录。
2. 素材根目录可以是：
   - 根目录下面直接放多个发布文件夹；
   - 根目录下面按年份、月份、发布文件夹分层；
   - 直接选择某一个发布文件夹。
3. 软件会递归寻找“直接包含图片的文件夹”，只读取图片格式，视频和其他格式会被忽略。
4. 在当前 Chrome 账号下点击“一键生成画像”，软件会读取最近 20 条笔记并结合账号关键词、长期话题和补充说明生成账号画像。
5. 确认画像后保存。画像按 Chrome Profile 分开保存，切换账号后不会混用。
6. 设置每日上限和发布时间段。

## 六、批量审核和定时提交

推荐流程：

1. 点击“推荐一组”或“换一组”。
2. 检查图片和封面顺序。
3. 确认标题、正文、话题、专辑、地点和原创声明。
4. 点击“定时发布（入队）”，确认本条实际发布时间。
5. 重复操作，逐条审核并入队。
6. 全部确认后，点击“开始提交”。

点击“开始提交”后，后台会把已确认任务逐条填入并提交到小红书的定时发布流程。每条任务只有收到平台 URL、成功提示或管理页跳转等确认后，才会写成成功；没有平台确认会写成“状态不明”并暂停队列，不会自动重复点击。任务保存在 SQLite 中，刷新页面或重启软件不会丢失。

这里的“定时”是把发布时间交给小红书平台：软件运行期间负责完成填表和提交，平台接受后由平台按计划时间发布。批量提交阶段需要保持软件、Chrome 和 Bridge 连接正常；所有任务都显示“已提交”后，可以关闭软件，已被平台接受的定时任务不会因为关闭本地软件而撤回。

遇到登录失效、Bridge 断开、网络错误、页面结构异常或点击超时，队列会自动暂停并把任务标记为“状态不明”，不会自动重复点击。请先人工检查小红书页面，再决定是否处理。

## 七、迁移设置和 API Key

如果确实要把个人设置迁移到自己的另一台电脑，可在停止软件后复制整个 `data` 文件夹，至少保留：

```text
data/assistant.sqlite3
data/assets/
data/backup/
```

其中 `assistant.sqlite3` 保存软件设置、账号画像、话题模板、排期记录和本地 API 配置。复制数据库也会复制历史记录、队列状态和已保存的 API Key；迁移前请暂停队列、暂停所有自动计划，并确认没有“提交中”或“状态不明”任务。素材目录要改成目标电脑上的实际路径。给可信的内部同事使用时，可以离线复制数据库携带 API Key 和配置；这些电脑会共用对应 API 额度。不要把数据库上传到公开 GitHub 或 Release。没有迁移数据库时，各自在后台填写 API Key。

不要复制目标电脑的 `.venv` 来代替安装。Python 虚拟环境通常包含原电脑的绝对路径，移动电脑后可能失效。每台电脑运行一次 `install.command` 更可靠。

### 自动发布与声音提醒

左侧“自动发布”可保存每个账号的素材目录、选图 / 拼图 / 水印、成套文案、每日数量和时间段。先保存规则、绑定账号、制作预览，再选择批量审核确认或启用全自动计划。

同一页的“声音与语音提醒”支持系统提示音、中文语音、导入自己的录音；可设置单次三声、总共三次、每 10 分钟或每小时重复。先保存，再试听；事项处理后点击“已处理，停止重复提醒”。此版本只在 Mac 上提醒，没有连接手机。

“Mac 后台运行”可选登录后启动和防闲置睡眠，默认关闭。关机、合盖或手动睡眠会中断本地任务。目标电脑必须重新绑定自己的 Chrome / 小红书账号；复制数据库不会迁移浏览器登录态。自动计划、已固定任务图片、水印资产和录音都保存在 `data/` 下，迁移时保留整个目录，并注意旧电脑的绝对路径不能在新电脑直接使用。

完整步骤和异常处理见 [自动发布与提醒使用说明](docs/自动发布与提醒使用说明.md)。本轮新增功能尚未发布 GitHub Release，公开旧安装包不包含本轮改动。

## 八、常见问题

### 页面打不开

双击 `start.command`，或在终端执行：

```bash
cd /你的路径/xhs-assistant
./start.command
```

### 提示缺少 Python

安装 Python 3.11 或更高版本后，再运行 `install.command`。安装地址：

```text
https://www.python.org/downloads/macos/
```

### Bridge 未连接

确认 Chrome 扩展已加载并启用，且 Chrome 中已经打开并登录小红书。切换 Chrome Profile 后，需要在该 Profile 中单独安装或启用扩展。

### 账号切换失败

先暂停后台队列，再切换账号。软件切换 Profile 时会重启 Chrome；切换期间不要手动操作 Chrome，也不要同时提交其他任务。

### 队列状态不明

先查看当前 Chrome 的小红书页面，确认这条笔记是否已经发布或已经进入定时列表。不要直接重复点击。确认没有提交后，再回到软件重新建立任务。

### 端口 5818 已被旧进程占用

先双击 `stop.command`，再双击 `start.command`。停止脚本只处理本软件约定的 `5818` 端口。不要同时启动多个副本共用同一个 `data/assistant.sqlite3`。如果无法判断哪个进程占用端口，重启 Mac 是最简单的处理方式。

### 真实测试没有页面确认

这不是“成功发布”。检查 Chrome 是否仍停留在创作者平台、XHS Bridge 是否启用，以及系统是否允许自动化访问 `creator.xiaohongshu.com`。确认前不要重复点击发布按钮，避免产生重复笔记。

## 九、备份

停止软件后，定期备份整个 `data` 文件夹，至少备份：

```text
data/assistant.sqlite3
data/assets/
data/backup/
```

不要在多个电脑上同时使用同一份 SQLite 数据库或同一套 Chrome Profile。每台电脑应当有自己的运行目录和浏览器登录态。

## 十、验收命令

安装完成后，可在软件目录执行：

```bash
python3 -m unittest discover -s app -p '*tests.py' -v
python3 -m py_compile app/server.py app/tests.py app/vendor/xiaohongshu-skills/scripts/cli.py app/vendor/xiaohongshu-skills/scripts/xhs/publish.py
node --check moments/engine.js
node --check moments/cli.js
```

自动化测试通过只代表本地逻辑和队列状态机通过；真实发布仍必须在已登录的 Chrome Profile 中看到小红书平台确认。
