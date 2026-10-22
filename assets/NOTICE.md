# 字体说明

`fonts/*.woff2` 从此前的 `calendar-sans.woff2` 离线分片制作，原字体来自本机已有的 Noto Sans S Chinese Regular。21,113个字符完整保留，使用 WOFF2 压缩，保留原字形与字体度量。衍生字体族名为 Checkin Calendar Sans。`fonts/index.json` 记录原字体 SHA-256、各分片的 Unicode 范围及字符总数；运行时只上传当前文字所需分片。

源字体版权属于 Adobe 和 Google，适用 SIL Open Font License 1.1，完整文本见同目录 `OFL-NotoSansSC.txt`。字体不适用插件代码的 MIT 许可证。

# 装饰立绘

`arcana.webp` 为《胜利女神：妮姬》阿尔卡娜（Arcana，资源 ID 581 / c581）的默认立绘，由此前的 PNG 进行无损编码，尺寸仍为1320×3724，解码后的RGBA像素完全一致。原PNG SHA-256 为 `10ef6067bf2653de47d1fce6c21990a3dc929d65afd9ff9ada6238f818ad992c`，WebP SHA-256 为 `3bad8cb27ce98a19d41276e4ac09b267304fadd28013dffa36e01b89247ef15c`。透明度与左侧渐隐仍由 HTML/CSS 控制。

角色及立绘权益归其原权利人所有，不适用插件代码的 MIT 许可证。
