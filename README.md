# Home Cinema · 本机影音库

A local, Infuse-style media library app for macOS: scans your movie / TV folders, fetches posters and overviews from TMDB, shows a dark poster wall, and plays everything in-app via libmpv — with resume, "continue watching", auto next episode, and automatic intro / end-credits skipping for TV shows.

只在本机（macOS）用的类 Infuse 影音库：扫描本地电影、剧集目录 → 从 TMDB 拉海报和简介 → 深色海报墙 → 在 App 窗口里用 libmpv 播放，记进度、继续观看、剧集自动下一集、电视剧自动跳过片头片尾。

- **Home Cinema.app**：Swift 外壳（WKWebView 显示海报墙 + 内嵌 libmpv 播放），启动时自动拉起本地 Python 服务。
- 后端 Python（FastAPI + SQLite），前端原生 HTML/CSS/JS（无构建、无 CDN），服务只监听 `127.0.0.1:8770`。
- 也可以只用浏览器打开 `http://127.0.0.1:8770`，此时播放交给 IINA。

## 依赖

- macOS 13+，Apple Silicon（Intel 未测）
- Homebrew：`brew install python mpv ffmpeg chromaprint`（libmpv 来自 mpv；chromaprint 提供 `fpcalc`，用于识别片头片尾）
- Xcode Command Line Tools（`swiftc`，编译 App 用）
- 可选：IINA（浏览器模式播放，或内嵌播放器不可用时的后备）
- 可选：TMDB API key（免费申请：https://www.themoviedb.org/settings/api ），没有也能用，只是没有海报和简介

## 安装

```bash
git clone https://github.com/faithpricejp-source/home-cinema.git
cd home-cinema
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx pytest
cp config.example.toml config.toml   # 然后按下一节编辑
macapp/build.sh                      # 编译 Home Cinema.app 并拷到 /Applications
```

TMDB key 存成一个只有 key 的文本文件（默认 `~/.config/tmdb/api-key.txt`），不要写进仓库。

## 配置

```bash
cp config.example.toml config.toml
```

编辑 `config.toml`：

```toml
movie_roots = ["/path/to/Movie"]                 # 电影根目录
tv_roots = ["/path/to/TV/已完结", "/path/to/TV/未完结"]  # 剧集根目录
db_path = "~/Library/Application Support/HomeCinema/library.db"
cache_dir = "~/Library/Caches/HomeCinema"        # 海报/剧照缓存
tmdb_key_file = "~/.config/tmdb/api-key.txt"     # TMDB v3 API key 只从这个文件读
tmdb_language = "zh-CN"
port = 8770
iina_cli = "/Applications/IINA.app/Contents/MacOS/iina-cli"
```

- `config.toml` 已在 `.gitignore` 里，不要提交。
- 路径支持 `~`；配置文件默认读项目根的 `config.toml`，可用环境变量
  `HOMECINEMA_CONFIG=/path/to/config.toml` 覆盖。
- 没有 TMDB key 也能用：扫描和播放正常，条目显示为「未匹配」+ 占位海报。

## 目录约定

- 电影：`<movie_root>/<标题> (年份)/<标题> (年份).<ext>`，一个文件夹一部电影。
- 剧集：`<tv_root>/<剧名>[ (年份)]/Season NN/<剧名> SxxExx.<ext>`；
  没有 `Season NN` 层时，视频直接放 `<剧名>/` 下（季号从文件名 SxxExx 取）。
- 视频扩展名：`mp4 mkv avi rmvb m4v mov ts wmv flv webm`（大小写不敏感）。
- 电影文件夹里可放 `.nfo`（含 `<tmdbid>` 则直接用该 id，不再搜索）、
  `poster.jpg` / `folder.jpg` / 与视频同名的 `.jpg`/`.png`（本地海报优先于 TMDB）、`.srt`/`.ass` 字幕（IINA 自动加载）。

## 运行（三条命令）

```bash
.venv/bin/python -m homecinema scan             # 扫描片库入库（增量；消失的文件标 missing，进度保留）
.venv/bin/python -m homecinema fetch-metadata   # 拉 TMDB 元数据、下载海报/剧照到缓存
.venv/bin/python -m homecinema serve            # 启动网页服务，浏览器开 http://127.0.0.1:8770
```

也可以在网页右上角点「重新扫描」（后台跑 scan + 补元数据，按钮上显示进度）。

## 使用

- 首页：继续观看（宽卡片带进度条）、最近添加、电影/剧集海报墙。
- 点「播放/继续播放」→ 调 `iina-cli` 打开；后台每 5 秒经 mpv IPC 记进度。
- 看过 90%（或剩余 <3 分钟）自动标已看；下次从头播。
- 没看完再点播放，从上次位置继续；位置 <30 秒当作没看过。
- 剧集详情页可切季、点任意一集播放；顶部「继续：SxxExx」直达下一集。

## 内嵌播放器（App 内播放）

在 Home Cinema.app 里点播放，画面直接在本窗口播放（Swift 外壳经 libmpv 渲染，
控制条用 mpv 自带 OSC），不再另开 IINA 窗口；播完回到海报墙，「继续观看」和进度条
自动刷新，剧集播完自动接下一集。浏览器访问时行为不变（仍交给 IINA）。进度落库、
已看判定（90% 或剩 <3 分钟）、续播起点两条路共用同一套逻辑。

### 快捷键（App 内，mpv 默认键位）

- 空格：暂停/继续
- ← / →：快退/快进 5 秒；↑ / ↓：快退/快进 1 分钟
- m：静音；j：切字幕轨；#：切音轨
- `[` / `]`：减速/加速播放；滚轮：快退/快进
- f：窗口全屏（走 App 菜单）；q：结束播放
- ESC：全屏时先退全屏，再按一次结束播放回到海报墙
- 鼠标移动唤出 OSC 控制条，可点按钮、拖进度条

内封字幕没有默认轨时按语言自动选（中文各变体优先，其次英文）。

### 后备机制

libmpv 初始化失败（如 dylib 被改名/损坏、render context 创建失败）时，弹窗提示
「内嵌播放器不可用，改用 IINA」，本次播放自动回退到 v1 的 IINA 路径。

## 跳过片头片尾

只对**电视剧**生效（电影不受影响）。App 内嵌播放时，默认自动跳过每集的片头和片尾；
片尾跳过会复用「播完」逻辑，照常标记已看并自动接下一集。

片头/片尾的位置预先由识别命令算好、存进库（`segments` 表）：

```bash
.venv/bin/python -m homecinema detect-segments                    # 处理所有还没检测的季
.venv/bin/python -m homecinema detect-segments --show 12          # 只处理剧集 ID=12
.venv/bin/python -m homecinema detect-segments --limit-seasons 3  # 最多处理 3 季
```

- 章节优先：每集先用 `ffprobe` 取章节，能定出片头/片尾就用章节结果（`source='chapters'`）。
- 其余集用声纹整季比对（`source='fingerprint'`）：片头取开头 `min(600, 时长×0.35)` 秒，
  片尾取最后 300 秒；一季只有 1 集时跳过声纹。两者都没有的记 `source='none'`。
- 每季做完立即落库：中途中断后重跑会跳过已完成的季。
- 调 `GET /api/segments?episode_id=N` 可查看某一集的识别结果（调试用）。

App 菜单「显示 → 跳过片头片尾」是总开关（默认开，记在 UserDefaults 的 `skipIntroOutro`）。
跳过片头时画面提示「已跳过片头 · 按 ← 回看」，手动往回拖进片头不会再被跳；关掉开关时，
正在播放的这一集立即停止跳过。

## 测试

```bash
.venv/bin/pytest -q
```

测试全部使用虚构片名与假 HTTP 客户端/假 mpv socket，不访问外网、不启动播放器。

## 说明

本产品使用 TMDB API 但未经 TMDB 认证；元数据与图片来自
[themoviedb.org](https://www.themoviedb.org)。

匹配不准的条目可以在 `~/Library/Application Support/HomeCinema/overrides.toml` 里手动指定：

```toml
[movie]
"Some Movie (1999)" = 12345   # 文件夹名 = TMDB 编号
[tv]
"Some Show" = 67890
```

`tools/audit_matches.py` 会回查每条匹配的英文片名和年份，列出可疑的。

片头片尾识别的思路参考了 Jellyfin 的 [Intro Skipper](https://github.com/intro-skipper/intro-skipper) 插件（Chromaprint 声纹整季比对）。

## License

GPL-3.0（App 链接 GPL 构建的 libmpv）。
