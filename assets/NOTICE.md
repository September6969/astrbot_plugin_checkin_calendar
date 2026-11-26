# 字体说明

`fonts/*.woff2` 从此前的 `calendar-sans.woff2` 离线分片制作，原字体来自本机已有的 Noto Sans S Chinese Regular。21,113个字符完整保留，使用 WOFF2 压缩，保留原字形与字体度量。衍生字体族名为 Checkin Calendar Sans。`fonts/index.json` 记录原字体 SHA-256、各分片的 Unicode 范围及字符总数；运行时只上传当前文字所需分片。

源字体版权属于 Adobe 和 Google，适用 SIL Open Font License 1.1，完整文本见同目录 `OFL-NotoSansSC.txt`。字体不适用插件代码的 MIT 许可证。

# 装饰立绘

`arcana.webp` 为《胜利女神：妮姬》阿尔卡娜（Arcana，资源 ID 581 / c581）的默认立绘，由此前的 PNG 进行无损编码，尺寸仍为1320×3724，解码后的RGBA像素完全一致。原PNG SHA-256 为 `10ef6067bf2653de47d1fce6c21990a3dc929d65afd9ff9ada6238f818ad992c`，WebP SHA-256 为 `3bad8cb27ce98a19d41276e4ac09b267304fadd28013dffa36e01b89247ef15c`。透明度与左侧渐隐仍由 HTML/CSS 控制。

角色及立绘权益归其原权利人所有，不适用插件代码的 MIT 许可证。

`arcana-roulette.png` 是用户提供的阿尔卡娜透明贴图。

`roulette-sans.ttf` 是 [Noto Sans SC](https://github.com/google/fonts/tree/main/ofl/notosanssc) 的轮盘文案字形子集，采用 SIL Open Font License 1.1。

# NIKKE 招募小游戏素材与卡池快照

- `gacha/jp_pool_snapshot.json` 记录由本地核验构建的日本服务器（JP）卡池快照及 SHA-256 摘要，仅包含角色元数据、官方 BlaBlaLink CDN 散列映射及卡池资格规则说明，不含玩家数据或运行缓存。
- `gacha/intro_r.gif`、`gacha/intro_sr.gif`、`gacha/intro_ssr.gif` 为通过 `scripts/build_gacha_intro.py` 离线绘制的程序化几何门闸过场动画，随插件源码适用 MIT 许可证。
- 运行时按需同步缓存的角色头像（BLA MI `256×512` / SI `128×128` WebP）属于《胜利女神：妮姬》（GODDESS OF VICTORY: NIKKE）及其权利人 SHIFT UP / Level Infinite 所有，不随插件安装包分发，亦不适用本仓库 MIT 代码许可证。
