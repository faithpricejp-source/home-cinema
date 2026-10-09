// Home Cinema.app 外壳：拉起本地 Python 服务，用 WKWebView 显示海报墙，退出时关掉服务。
// 编译：macapp/build.sh（swiftc 直接编，不需要 Xcode 工程）。

import Cocoa
import WebKit

// 项目目录由 build.sh 编译时写进 Info.plist（App 要用项目里的 .venv 起 Python 服务）
let projectRoot = Bundle.main.object(forInfoDictionaryKey: "HCProjectRoot") as? String
    ?? FileManager.default.currentDirectoryPath
let port = 8770
let baseURL = URL(string: "http://127.0.0.1:\(port)/")!
let bgColor = NSColor(srgbRed: 0x1F / 255.0, green: 0x1E / 255.0, blue: 0x1D / 255.0, alpha: 1)

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                         WKScriptMessageHandler, NSWindowDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?

    // 内嵌播放状态（网页 postMessage 驱动；详见下方「内嵌播放」）
    var playerView: MPVPlayerView?
    var progressTimer: Timer?
    var playback: (type: String, id: Int, title: String)?
    var isEndingPlayback = false
    var endedByNaturalEOF = false  // 本次结束是 mpv 自然 EOF（core 已 idle），区别于跳过片尾
    var switchingEpisode = false

    // 跳过片头片尾状态（每一集开始时重置）
    var introRange: (start: Double, end: Double)?
    var creditsStart: Double?
    var introSkipped = false
    var creditsHandled = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        DispatchQueue.global().async {
            // 已有服务在跑（比如手动 serve 的）就直接用，不再起第二个
            if self.serverReady() { self.restartIfStale() }
            if !self.serverReady() { self.startServer() }
            var ok = false
            for _ in 0..<60 {
                if self.serverReady() { ok = true; break }
                Thread.sleep(forTimeInterval: 0.25)
            }
            DispatchQueue.main.async {
                if ok {
                    self.webView.load(URLRequest(url: baseURL))
                } else {
                    self.showError("本地服务没有起来，日志在 ~/Library/Logs/HomeCinema/server.log")
                }
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        // 内嵌播放中：先 POST 最后一次进度，再释放 mpv，然后走原有的服务关停逻辑
        if playerView != nil {
            postFinalProgressSync()
            playerView?.stop()
            playerView?.removeFromSuperview()
            playerView = nil
            progressTimer?.invalidate()
            progressTimer = nil
        }
        // 只关自己起的服务。IINA 还在放片时服务要留着记进度，下次打开 App 会接着用这个服务
        guard let p = server, p.isRunning, playingCount() == 0 else { return }
        p.terminate()
        p.waitUntilExit()
    }

    // MARK: 服务

    func serverReady() -> Bool {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/scan/status"))
        req.timeoutInterval = 1
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            ok = (resp as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2)
        return ok
    }

    /// 已在跑的服务如果是在 Python 代码更新之前起的（比如上次退出时 IINA 还在放片，服务被留下），
    /// 而且没有在放片，就关掉它，让下面重新起一个跑新代码的。
    func restartIfStale() {
        guard let st = statusJSON(), let started = st["server_started"] as? Double,
              let pid = st["pid"] as? Int32, (st["playing"] as? Int ?? 0) == 0 else { return }
        let pkg = URL(fileURLWithPath: projectRoot + "/homecinema")
        var newest = 0.0
        if let e = FileManager.default.enumerator(at: pkg, includingPropertiesForKeys: [.contentModificationDateKey]) {
            for case let f as URL in e where f.pathExtension == "py" {
                let m = (try? f.resourceValues(forKeys: [.contentModificationDateKey]))?
                    .contentModificationDate?.timeIntervalSince1970 ?? 0
                newest = max(newest, m)
            }
        }
        guard newest > started else { return }
        kill(pid, SIGTERM)
        for _ in 0..<40 where serverReady() { Thread.sleep(forTimeInterval: 0.25) }
    }

    func statusJSON() -> [String: Any]? {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/scan/status"))
        req.timeoutInterval = 1
        let sem = DispatchSemaphore(value: 0)
        var out: [String: Any]?
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data = data { out = try? JSONSerialization.jsonObject(with: data) as? [String: Any] }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2)
        return out
    }

    func playingCount() -> Int {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/scan/status"))
        req.timeoutInterval = 1
        let sem = DispatchSemaphore(value: 0)
        var n = 0
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                n = obj["playing"] as? Int ?? 0
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2)
        return n
    }

    func startServer() {
        let logDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/HomeCinema")
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        let logURL = logDir.appendingPathComponent("server.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try? FileHandle(forWritingTo: logURL)
        log?.seekToEndOfFile()

        let p = Process()
        p.executableURL = URL(fileURLWithPath: projectRoot + "/.venv/bin/python")
        p.arguments = ["-m", "homecinema", "serve"]
        p.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = log
        p.standardError = log
        do {
            try p.run()
            server = p
        } catch {
            DispatchQueue.main.async { self.showError("启动服务失败：\(error.localizedDescription)") }
        }
    }

    // MARK: 窗口

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.applicationNameForUserAgent = "HomeCinemaApp"
        config.userContentController.add(self, name: "player")  // 网页据此判定走内嵌播放
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")  // 加载前不闪白

        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1600, height: 1000)
        let w = min(1600, screen.width * 0.85), h = min(1000, screen.height * 0.85)
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: w, height: h),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "影院"
        window.delegate = self
        window.titlebarAppearsTransparent = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = bgColor
        window.minSize = NSSize(width: 960, height: 640)
        window.contentView = webView
        window.setFrameAutosaveName("HomeCinemaMain")
        if !window.setFrameUsingName("HomeCinemaMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func showError(_ msg: String) {
        let html = "<body style='background:#1F1E1D;color:#ECEAE3;font:15px -apple-system;padding:48px'>\(msg)</body>"
        webView.loadHTMLString(html, baseURL: nil)
    }

    // 验收用：环境变量 HC_DEBUG_PLAY=movie:12 / episode:34 时，页面首次加载完自动点播放，
    // 走的是和用户点击完全相同的网页 → /api/play → postMessage → libmpv 路径
    var debugPlayDone = false
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard !debugPlayDone, let spec = ProcessInfo.processInfo.environment["HC_DEBUG_PLAY"] else { return }
        let parts = spec.split(separator: ":")
        guard parts.count == 2, ["movie", "episode"].contains(String(parts[0])),
              let id = Int(parts[1]) else { return }
        debugPlayDone = true
        webView.evaluateJavaScript("playItem('\(parts[0])', \(id), '')")
    }

    // 外部链接（如 TMDB）用默认浏览器开，不在窗口里跳走
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url, let scheme = url.scheme?.lowercased() else {
            decisionHandler(.allow)
            return
        }
        if scheme == "http" || scheme == "https" {
            if url.host != "127.0.0.1" {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
            return
        }
        // 10-05 审计 S07：只放行本机页面与 about:blank，其他 scheme 一律拒绝
        decisionHandler(scheme == "about" ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // MARK: 内嵌播放（网页发 embedded /api/play 后经 player message handler 进来）

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "player", let body = message.body as? [String: Any],
              let type = body["type"] as? String, let id = body["id"] as? Int,
              let path = body["path"] as? String, let title = body["title"] as? String
        else { return }
        let startAt = body["start_at"] as? Double ?? 0
        startEmbeddedPlayback(type: type, id: id, path: path, startAt: startAt, title: title,
                              skip: Self.parseSkip(body["skip"]),
                              subs: body["subs"] as? [String] ?? [])
    }

    /// 解析 /api/play 返回里的 skip：{"intro": [s,e] 或 null, "credits_start": 秒 或 null}。
    static func parseSkip(_ value: Any?) -> (intro: (Double, Double)?, creditsStart: Double?) {
        guard let skip = value as? [String: Any] else { return (nil, nil) }
        var intro: (Double, Double)?
        if let arr = skip["intro"] as? [Double], arr.count == 2 {
            intro = (arr[0], arr[1])
        }
        let creditsStart = skip["credits_start"] as? Double
        return (intro, creditsStart)
    }

    func startEmbeddedPlayback(type: String, id: Int, path: String, startAt: Double, title: String,
                               skip: (intro: (Double, Double)?, creditsStart: Double?),
                               subs: [String] = []) {
        // 播放中又点了另一个条目：不补发进度（周期上报最多差 5 秒），直接换片
        if let current = playerView {
            progressTimer?.invalidate(); progressTimer = nil
            current.stop()
            current.removeFromSuperview()
            playerView = nil
            isEndingPlayback = false
            switchingEpisode = false
        }

        guard let view = MPVPlayerView.create() else {
            fallbackToIINA(type: type, id: id)
            return
        }
        playerView = view
        playback = (type: type, id: id, title: title)

        view.onEscape = { [weak self] in self?.handleEscape() }
        view.onPlaybackEnded = { [weak self] reason in
            guard let self = self else { return }
            if reason != .eof && self.switchingEpisode {
                // 自动下一集 loadfile replace 会对旧文件发非 EOF 的 END_FILE，忽略一次
                self.switchingEpisode = false
                return
            }
            if reason == .eof { self.endedByNaturalEOF = true }
            // Kimi-F-1：mpv 侧打不开文件（服务端 missing 校验之后才失效）要给一句提示，
            // 不再和「用户按 ESC」一样静默回海报墙
            let failMessage = reason == .error ? "无法播放：" + (self.playback?.title ?? "") : nil
            self.playbackDidEnd(eof: reason == .eof, message: failMessage)
        }
        view.onMpvShutdown = { [weak self] in self?.playbackDidEnd(eof: false) }
        view.onPosition = { [weak self] position in self?.handlePosition(position) }
        resetSkipState(intro: skip.intro, creditsStart: skip.creditsStart, startAt: startAt)

        if let content = window.contentView {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view, positioned: .above, relativeTo: nil)  // 网页留在底下不销毁
            NSLayoutConstraint.activate([
                view.leadingAnchor.constraint(equalTo: content.leadingAnchor),
                view.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                view.topAnchor.constraint(equalTo: content.topAnchor),
                view.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            ])
        }
        window.makeFirstResponder(view)
        window.title = title
        applyFloatWhilePlaying()
        view.load(path: path, start: startAt, title: title, subs: subs)

        progressTimer?.invalidate()
        progressTimer = Timer.scheduledTimer(timeInterval: 5, target: self,
                                             selector: #selector(progressTick),
                                             userInfo: nil, repeats: true)
    }

    /// libmpv 起不来（mpv_create/render context 失败）：弹窗说明并让这一次走 IINA。
    func fallbackToIINA(type: String, id: Int) {
        let alert = NSAlert()
        alert.messageText = "内嵌播放器不可用"
        alert.informativeText = "已改用 IINA 播放。"
        alert.addButton(withTitle: "好")
        alert.runModal()
        postJSON(path: "api/play", body: ["type": type, "id": id])  // 不带 embedded → v1 路径
    }

    @objc func progressTick() {
        guard let view = playerView else { return }
        postProgress(position: view.currentPosition, duration: view.currentDuration)
    }

    /// ESC 的结束语义：全屏先退全屏，第二次 ESC 才结束播放。
    func handleEscape() {
        guard playerView != nil else { return }
        // 10-05 审计 S03：自动接下一集途中（等服务回应）按 ESC 直接收尾，在途回调会自然作废
        if isEndingPlayback {
            teardownPlayerAndRefresh()
            return
        }
        if window.styleMask.contains(.fullScreen) {
            window.toggleFullScreen(nil)
            return
        }
        playbackDidEnd(eof: false)
    }

    /// 播放结束（ESC / 用户退出 / 播完）：先 POST 最后一次进度，剧集播完尝试自动下一集，
    /// 否则移除播放层回到网页并刷新。
    func playbackDidEnd(eof: Bool, message: String? = nil) {
        guard playerView != nil, !isEndingPlayback else { return }
        isEndingPlayback = true
        progressTimer?.invalidate(); progressTimer = nil
        playerView?.pause()  // 10-05 审计 S05：收尾期间（等服务回应）先停住音画

        let finished = playback
        let view = playerView
        let duration = view?.currentDuration
        // 播完（EOF）就按看到结尾记，保证判成已看；最后一次周期上报可能还差几秒到几分钟
        let position = eof ? (duration ?? view?.currentPosition) : view?.currentPosition
        postProgress(position: position, duration: duration) { [weak self] in
            guard let self = self, self.isEndingPlayback else { return }
            if eof, finished?.type == "episode", let episodeId = finished?.id {
                self.autoNextAfter(episodeId: episodeId)
            } else {
                self.teardownPlayerAndRefresh(message: message)  // Kimi-F-1：把失败原因带到海报墙
            }
        }
    }

    /// 剧集按 EOF 播完后接下一集；没有下一集（或换片失败）就回到网页。
    func autoNextAfter(episodeId: Int) {
        var req = URLRequest(url: URL(string: "api/next?type=episode&id=\(episodeId)",
                                      relativeTo: baseURL)!)
        req.timeoutInterval = 5
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self = self else { return }
            var nextId: Int?
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                nextId = obj["id"] as? Int
            }
            DispatchQueue.main.async {
                guard self.isEndingPlayback else { return }  // 期间已被别的路径结束
                guard let nextId = nextId else {
                    self.teardownPlayerAndRefresh()
                    return
                }
                self.playNextEpisode(id: nextId)
            }
        }.resume()
    }

    func playNextEpisode(id: Int) {
        postJSON(path: "api/play",
                 body: ["type": "episode", "id": id, "embedded": true]) { [weak self] data in
            guard let self = self else { return }
            guard self.isEndingPlayback else { return }  // 期间已被别的路径结束
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let path = obj["path"] as? String,
                  let title = obj["title"] as? String,
                  let startAt = obj["start_at"] as? Double,
                  let view = self.playerView
            else {
                // 10-05 审计 S06：下一集起不来（如 409 文件不在原位）给一句提示，不再静默回海报墙
                var msg = "下一集无法播放"
                if let data = data,
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let detail = obj["detail"] as? String { msg += "：" + detail }
                self.teardownPlayerAndRefresh(message: msg)
                return
            }
            self.isEndingPlayback = false
            // loadfile replace 会对旧文件发非 EOF 的 END_FILE，要忽略一次——但自然播完时 core 已进 idle，
            // 不会再发，这时置 true 会把下一集的加载失败吞掉（10-05 审计 A1）
            self.switchingEpisode = !self.endedByNaturalEOF
            self.endedByNaturalEOF = false
            self.playback = (type: "episode", id: id, title: title)
            self.window.title = title
            let skip = Self.parseSkip(obj["skip"])
            self.resetSkipState(intro: skip.intro, creditsStart: skip.creditsStart, startAt: startAt)
            view.load(path: path, start: startAt, title: title, subs: obj["subs"] as? [String] ?? [])
            self.progressTimer = Timer.scheduledTimer(timeInterval: 5, target: self,
                                                      selector: #selector(self.progressTick),
                                                      userInfo: nil, repeats: true)
        }
    }

    /// 移除播放层、恢复窗口标题、通知网页刷新「继续观看」和进度条。
    func teardownPlayerAndRefresh(message: String? = nil) {
        progressTimer?.invalidate(); progressTimer = nil
        endedByNaturalEOF = false
        if let view = playerView {
            view.stop()
            view.removeFromSuperview()
        }
        playerView = nil
        playback = nil
        isEndingPlayback = false
        switchingEpisode = false
        introRange = nil
        creditsStart = nil
        introSkipped = false
        creditsHandled = false
        window.makeFirstResponder(webView)
        window.title = "影院"
        window.level = .normal  // 回到海报墙就不再置顶
        webView.evaluateJavaScript("window.homecinemaRefresh && window.homecinemaRefresh()")
        if let message = message,
           let data = try? JSONSerialization.data(withJSONObject: [message]),
           let arr = String(data: data, encoding: .utf8) {
            webView.evaluateJavaScript("window.homecinemaToast && window.homecinemaToast(\(arr)[0])")
        }
    }

    // MARK: 跳过片头片尾（菜单「显示」开关，默认开；跳过逻辑见 handlePosition）

    var skipIntroOutro: Bool {
        get { UserDefaults.standard.object(forKey: "skipIntroOutro") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "skipIntroOutro") }
    }

    @objc func toggleSkipIntroOutro(_ sender: NSMenuItem) {
        skipIntroOutro.toggle()
        sender.state = skipIntroOutro ? .on : .off
        // 关掉开关时无需额外动作：handlePosition 每次都看开关，正在播放的这一集立刻不再跳过
        if skipIntroOutro {
            // Kimi-F-2：打开开关不追溯正在播放的这一集。否则位置已在片尾区时（关着开关看到片尾
            // 再打开），下一个 time-pos 就进 handlePosition 的片尾分支 → playbackDidEnd(eof: true)：
            // 按看到结尾记进度、标成已看、自动切下一集。只对之后的集生效。
            introSkipped = true
            creditsHandled = true
        }
    }

    /// 每集（含自动下一集）开始时重置跳过状态。起播位置已经进了片头就视为已跳过，
    /// 不再自动跳；用户之后自己往回拖进片头也不再跳。
    func resetSkipState(intro: (Double, Double)?, creditsStart: Double?, startAt: Double) {
        introRange = intro
        self.creditsStart = creditsStart
        creditsHandled = false
        introSkipped = (intro != nil && startAt >= intro!.0)
    }

    /// time-pos 变化（限流后）驱动跳过；开关关掉就什么都不做。
    func handlePosition(_ position: Double) {
        guard let view = playerView, skipIntroOutro else { return }
        if !introSkipped, let intro = introRange,
           position >= intro.start, position < intro.end - 1 {
            introSkipped = true
            view.seekAbsolute(intro.end)
            view.showText("已跳过片头 · 按 ← 回看", duration: 2500)
            return
        }
        if !creditsHandled, let creditsStart = creditsStart, position >= creditsStart {
            creditsHandled = true
            view.showText("已跳过片尾", duration: 1500)
            playbackDidEnd(eof: true)  // 复用记已看 + 自动下一集
        }
    }

    // MARK: 播放时置顶（菜单「显示 → 播放时置顶」切换，记在 UserDefaults，默认开）

    var floatWhilePlaying: Bool {
        get { UserDefaults.standard.object(forKey: "floatWhilePlaying") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "floatWhilePlaying") }
    }

    func applyFloatWhilePlaying() {
        window.level = (playerView != nil && floatWhilePlaying) ? .floating : .normal
    }

    @objc func toggleFloat(_ sender: NSMenuItem) {
        floatWhilePlaying.toggle()
        sender.state = floatWhilePlaying ? .on : .off
        applyFloatWhilePlaying()
    }

    // 菜单 ⌘F / ⌃⌘F 进全屏也走这里：全屏前降回普通层级，退出后恢复播放时置顶
    func windowWillEnterFullScreen(_ notification: Notification) { window.level = .normal }
    @objc func toggleFullScreenMenu(_ sender: Any?) {
        if !window.styleMask.contains(.fullScreen) { window.level = .normal }
        window.toggleFullScreen(nil)
    }
    func windowDidExitFullScreen(_ notification: Notification) { applyFloatWhilePlaying() }

    // MARK: 进度上报

    /// 播放期间每 5 秒、结束时各一次：POST 当前位置和时长到 /api/progress。
    func postProgress(position: Double?, duration: Double?,
                      completion: (() -> Void)? = nil) {
        guard let pb = playback, let position = position else { completion?(); return }
        var body: [String: Any] = ["type": pb.type, "id": pb.id, "position": position]
        if let duration = duration { body["duration"] = duration } else { body["duration"] = NSNull() }
        postJSON(path: "api/progress", body: body) { _ in completion?() }
    }

    /// 退出 App 前的最后一次进度：同步等最多 2 秒，保证落库后再退。
    func postFinalProgressSync() {
        guard let view = playerView, let pb = playback,
              let position = view.currentPosition else { return }
        var req = URLRequest(url: baseURL.appendingPathComponent("api/progress"))
        req.httpMethod = "POST"
        req.timeoutInterval = 3  // 10-05 审计 S04：与下面的等待时长对齐
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["type": pb.type, "id": pb.id, "position": position]
        if let duration = view.currentDuration {
            body["duration"] = duration
        } else {
            body["duration"] = NSNull()
        }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        let semaphore = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in semaphore.signal() }.resume()
        _ = semaphore.wait(timeout: .now() + 3)
    }

    @discardableResult
    func postJSON(path: String, body: [String: Any],
                  completion: ((Data?) -> Void)? = nil) -> URLSessionDataTask? {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.timeoutInterval = 10
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        let task = URLSession.shared.dataTask(with: req) { data, _, _ in
            // 统一切回主线程再回调，回调里都是 UI/状态操作
            if let completion = completion {
                DispatchQueue.main.async { completion(data) }
            }
        }
        task.resume()
        return task
    }

    // MARK: 菜单（没有 Edit 菜单的话，搜索框里 ⌘C/⌘V 不起作用）

    @objc func reload(_ sender: Any?) { webView.reload() }
    @objc func goHome(_ sender: Any?) { webView.load(URLRequest(url: baseURL)) }

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于影院",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏影院", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出影院", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "显示")
        view.addItem(withTitle: "首页", action: #selector(goHome(_:)), keyEquivalent: "1")
        view.addItem(withTitle: "刷新", action: #selector(reload(_:)), keyEquivalent: "r")
        view.addItem(withTitle: "进入全屏", action: #selector(toggleFullScreenMenu(_:)), keyEquivalent: "f")
        let floatItem = view.addItem(withTitle: "播放时置顶", action: #selector(toggleFloat(_:)), keyEquivalent: "t")
        floatItem.state = floatWhilePlaying ? .on : .off
        let skipItem = view.addItem(withTitle: "跳过片头片尾", action: #selector(toggleSkipIntroOutro(_:)), keyEquivalent: "")
        skipItem.state = skipIntroOutro ? .on : .off
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "窗口")
        win.addItem(withTitle: "最小化", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "关闭", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win

        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
