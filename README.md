# 打卡月历

独立 AstrBot 插件。手动记录个人打卡次数，并返回一张暖色月历图片。每天可以记录多次，相邻两次至少间隔120分钟；次数越多，颜色越深；图片始终显示实际次数，最高颜色代表 5 次及以上。

月历左侧使用低透明度阿尔卡娜立绘作背景，图片底部不显示命令和时区说明。命令仍可通过 `🦌 帮助` 查看，日期规则仍由插件时区配置决定。

## 使用

| 命令 | 效果 |
| --- | --- |
| `🦌` | 记录一次，返回本月月历 |
| `🦌 月历` | 只查看本月月历，不增加次数 |
| `🦌 月历 2026-10` | 只查看指定月份 |
| `🦌 状态` | 只查看本月月历 |
| `🦌 撤销` | 撤销今天最近一次尚未撤销的记录 |
| `🦌 排行` | 查看整个机器人本月前十名，含所有群聊和私聊记录 |
| `🦌 排行 2026-10` | 查看指定月份的排行 |
| `🦌 帮助` | 查看命令说明 |

直接发送 `🦌` 即可触发，无需斜杠或唤醒词。原 `/打卡` 命令不再注册，普通聊天中夹带 emoji 不会触发。

标题显示为“用户名本月的🦌馆足迹”。

无需绑定账号。群聊和私聊均可使用。每条满足120分钟间隔的新 `🦌` 消息增加一次；平台重复投递同一消息时只计一次。平台没有提供消息 ID 时无法跨投递识别重复，此时按每次命令调用计数。

发送日历图片时，在同一条消息中使用平台原生艾特标记记录者；以用户 ID 为准。成功记录、只读查询和撤销后的日历都适用。

## 安装

在 AstrBot 插件管理中上传本插件 ZIP，或将目录放到 `data/plugins/astrbot_plugin_checkin_calendar` 后加载。支持 AstrBot 4.24 至 5.0 之前的版本。

月历由 **AstrBot 所在服务器的 Chromium 本地渲染**，不调用宿主公共图片节点，不上传昵称、记录、字体或立绘。所有图片与字体随包内嵌，页面禁用网络请求。浏览器按需启动并复用，每次使用独立上下文；请求串行执行以限制内存，排队与渲染共最多30秒。异常时返回文字，次数保留，可用 `🦌 月历` 只读重试。插件卸载时关闭浏览器。

运行依赖为 `tzdata`、`jinja2`、`playwright`。安装 Python 依赖后，还必须在 **运行 AstrBot 的同一环境、同一用户** 下安装 Chromium。Linux 执行：

```sh
python -m pip install -r requirements.txt
python -m playwright install --with-deps --only-shell chromium
```

Docker 用户需在容器中执行；只重启容器会保留安装，删除并重建容器则需要重新安装。生产建议构建派生镜像，避免重建丢失依赖。不要把宿主机的 Windows 浏览器复制到 Linux 容器。可在 Linux Dockerfile 中追加：

```dockerfile
RUN python -m pip install playwright==1.62.0 'jinja2>=3.1,<4' tzdata \
 && python -m playwright install --with-deps --only-shell chromium
```

Windows 本地预览执行 `python -m playwright install chromium`。缺少浏览器时会明确退回文字，不会悄悄转用远程节点。

随包提供 Noto Sans 衍生中文 WOFF2 字体分片，原有21,113个字符全部保留。立绘使用无损 WebP。许可证见 `assets/OFL-NotoSansSC.txt`；未收录的生僻字和 emoji 仍依赖服务器备用字体。

## 记录规则与隐私

- 默认按 `Asia/Shanghai`（北京时间）划分日期，可在插件配置的 `timezone` 修改 IANA 时区。配置失效时插件拒绝加载，避免悄悄用错日期。
- 更改时区只影响后续记录，已有记录的日期不移动。
- 同一平台实例、同一机器人下的同一用户共用记录，换群不会新建一套。不同用户、机器人或平台实例相互隔离。
- 个人月历只允许查看本人；排行榜按当前平台实例及机器人隔离，包含该机器人所有用户的群聊和私聊打卡。总次数和参与人数覆盖整个月，展示前十名；按次数降序、活跃天数降序、稳定用户标识排序，不按昵称合并。撤销记录不计入排行。
- 排行兼容旧记录。昵称优先使用最近一次命令保存的展示名；OneBot 对榜上缺失昵称进行有超时限制的只读查询，获取不到显示稳定匿名标识，不展示完整账号。
- 成功记录后按真实经过时间等待120分钟，跨午夜、重启和切换时区都不会重置。拒绝的尝试不延长等待时间。
- 冷却期间原生艾特发送者本人并回复文字：“注意身体，歇会儿吧！（每120分钟只能记录一次哦）”，补充剩余时间，不增加次数、不生成或发送日历图片。即使宿主开启自动文转图，这条提醒仍保持文字。
- 撤销仅作用于今天最后一次记录，且不重置120分钟间隔。重复投递的撤销消息不会再撤销一次。
- 保存异常时不宣称成功，也不自动重试写入；先通过只读月历核对。
- 没有自动签到、补签、后台通知或任何外部账号操作。

数据保存在 AstrBot 专用目录 `data/plugin_data/astrbot_plugin_checkin_calendar/calendar.sqlite3`，包含用于隔离的用户标识、日期、时间戳和消息去重标记。备份时应暂停插件后复制该数据库。插件升级不应删除此目录；卸载插件前按需备份或清除自己的数据。

旧版 `render-preference.json` 已不再读取，可以删除。`rendered/` 保存待发送的临时图片，后续渲染会清理超过24小时的图片；数据库不在清理范围内。

## 开发与本地验证

在插件目录执行 `python -m pytest -q`。开发测试额外需要 `pytest`、`pytest-asyncio`、`jinja2` 与 AstrBot SDK；执行预览脚本还需本机已安装的 Playwright Chromium。

```powershell
$env:ASTRBOT_ROOT = "E:/DevCache/checkin-calendar-review/sdk-test"
python -m pytest -q
python scripts/preview.py --output E:/DevCache/checkin-calendar-review/previews
```

预览脚本直接渲染正式模板，使用合成记录。GitHub CI 在 Python 3.12、3.13 下运行回归，并检查安装包和 Chromium 页面布局。部署与真实消息验收分别记录，CI 不会向真实用户或群发消息。

`scripts/build_font_shards.py --source <原始calendar-sans.woff2路径>` 可重新生成字体分片，使用开发依赖 `fonttools[woff]`。原始字体可从 v0.1.7 对应 Git 历史获取，SHA-256 记录在 `assets/fonts/index.json`；运行插件不需要安装 FontTools，也不现场切割字体。

排行榜使用本地 Chromium，与月历共用串行渲染器。`scripts/preview_ranking.py --output <目录>` 覆盖正常、空榜、超长昵称与大数值。
