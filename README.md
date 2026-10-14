# ComicScript

这是一个用于下载 `rouman5.com` 已完结漫画的 Python 爬虫。列表入口已经使用
`continued=false` 过滤，因此脚本会扫描完结漫画列表的所有分页，然后按下面的目录保存：
当前网站类型的代码放在 `rounman/` 目录中，后续增加其他漫画网站时可以在这里新增对应目录。

```text
download/
└── rouman5/
    ├── progress.json             # 总页数、各页状态、当前页和待处理页
    └── 第0页/                    # 无 page 参数的入口页
        ├── progress.json         # 本页漫画、章节和图片下载进度
        └── 漫画名/
            └── 第1话/
                ├── 0001.jpg
                ├── 0002.jpg
                └── manifest.json
```

## 安装

建议使用 Python 3.10 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r rounman/requirements.txt
```

使用 PyCharm 时，将项目解释器设置为：

```text
<项目目录>/.venv/bin/python
```

本项目中的实际路径是 `/Users/songzhouzhang/Mine/Github/ComicScript/.venv/bin/python`。不要继续使用缺少 HTTP/3 依赖的 `/usr/bin/python3`。

## 下载

先限制范围做一次小规模检查：

```bash
python rounman/comic_scraper.py --max-pages 1 --max-books 1 --verbose
```

确认目录和图片都正常后，执行完整下载：

```bash
python rounman/comic_scraper.py --workers 4 --delay 0.5
```

部分网络会重置该站点的普通 TCP HTTPS 连接，但浏览器仍可通过 HTTP/3（QUIC）访问。脚本已使用 `curl_cffi` 优先走 HTTP/3，因此更新代码后需要重新安装依赖：

```bash
python -m pip install -r rounman/requirements.txt
```

如果浏览器依靠代理扩展才能打开网站，再将同一代理显式传给脚本：

```bash
python rounman/comic_scraper.py --proxy http://127.0.0.1:实际端口 --workers 4 --delay 0.5
```

`--proxy` 参数不会安装或启动代理服务。也可以使用 `HTTP_PROXY`、`HTTPS_PROXY` 环境变量。请求失败的列表页会保留在 `progress.json`，网络恢复后再次运行即可从断点继续。

程序可以重复执行。每话目录中的 `manifest.json` 保存了图片顺序和远端地址；再次运行时会逐张检查图片是否能被 Pillow 正常打开。完整图片会跳过，空文件、损坏图片、缺失图片和上次中断留下的临时文件会被重新下载。下载采用临时文件写入，只有校验成功后才替换正式文件。

网站根目录下的 `download/rouman5/progress.json` 只记录总页数、各页状态、当前页和待处理分页。每个 `第N页/progress.json` 单独记录该页的漫画、章节和图片下载进度。旧版集中在根文件中的进度会在下次运行时自动拆分。程序中断后再次执行相同命令，会从对应页面的进度继续；即使进程在单话中断，也会结合每话的 `manifest.json` 继续补齐缺失或损坏图片。

列表页按页码单向向后扫描。某一页里的漫画或章节请求失败时，该页会标记为 `incomplete`，但不会重新加入总队列；脚本会继续处理后面的新页面。所有新页面处理完后直接结束，不会自动回头补前面页面的失败项。

## 逐项压缩

漫画下载完成后，可以将某一页目录下的每部漫画分别压缩。无 `page` 参数的入口页保存为 `第0页`，`page=1` 保存为 `第1页`。

打开 `rounman/zip_items.py`，修改文件顶部的路径变量：

```python
TARGET_DIRECTORY = Path(
    "/Users/songzhouzhang/Mine/Github/ComicScript/rounman/download/rouman5/第1页"
)
```

然后在 PyCharm 中直接运行 `zip_items.py`，不需要填写命令行参数。脚本会生成 `漫画名.zip`。每个 ZIP 写入并校验成功后，才会删除对应的漫画目录；已有同名 ZIP 时默认跳过并保留源目录。

页面目录里的 `progress.json` 是该页的断点配置，压缩脚本会自动保留，不会压缩或删除它。

需要先预览且不修改任何文件时，将配置改为：

```python
DRY_RUN = True
```

需要替换已有 ZIP 时，将 `OVERWRITE_EXISTING_ZIP` 改为 `True`。压缩应在该页下载完成后执行；之后再次运行下载脚本，会重新创建已删除的漫画目录。

如果站点结构发生变化，可以先用更详细日志定位：

```bash
python rounman/comic_scraper.py --max-pages 1 --max-books 1 --verbose
```

请遵守目标网站的服务条款、robots 规则以及漫画版权要求，并合理设置 `--delay` 和 `--workers`。
