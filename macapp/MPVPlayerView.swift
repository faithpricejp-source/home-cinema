// 内嵌 libmpv 播放视图：NSOpenGLView + mpv render API（vo=libmpv），本版不自绘控制条，
// 用 mpv 自带 OSC。键盘/鼠标事件转成 mpv 命令（libmpv 没有自己的窗口收输入）。
//
// 线程约定：
// - mpv 事件只在本视图的串行队列里用 mpv_wait_event 取（mpv_wait_event 不许并发调用）；
// - 绘制只在主线程 draw(_:) 里做（NSOpenGLView 的 GL context 在主线程）；
// - stop() 必须主线程调用：先 mpv_render_context_free 再 mpv_terminate_destroy；
// - 对外回调（onEscape / onPlaybackEnded / onMpvShutdown）一律切回主队列再调。

import Cocoa

// OpenGL.framework 的句柄：mpv_render_context_create 要求的 get_proc_address
// 用 dlsym 从这里取函数地址（render_gl.h：libmpv 自己不链接 GL）。
private let glFramework: UnsafeMutableRawPointer? = dlopen(
    "/System/Library/Frameworks/OpenGL.framework/OpenGL", RTLD_LAZY | RTLD_GLOBAL)

final class MPVPlayerView: NSOpenGLView {
    static let subLangs = "chi,zho,zh,chs,zh-Hans,zh-CN,cht,zh-Hant,zh-TW,eng,en"

    // 播放结束原因：播完（EOF）还是用户退出/其他
    enum EndReason { case eof, user }

    var onEscape: (() -> Void)?                 // 主队列回调
    var onPlaybackEnded: ((EndReason) -> Void)? // 主队列回调
    var onMpvShutdown: (() -> Void)?            // 主队列回调
    var onPosition: ((Double) -> Void)?         // 主队列回调，time-pos 变化时限流回调

    private var mpv: OpaquePointer?
    private var renderCtx: OpaquePointer?
    private let eventQueue = DispatchQueue(label: "homecinema.mpv.events", qos: .userInitiated)
    private var drainingStopped = false         // stop() 之后不再进事件循环
    private let stateLock = NSLock()
    private var _position: Double?
    private var _duration: Double?
    private var lastPositionNotify = 0.0        // 只在 eventQueue 上读写，限流每秒最多 4 次

    /// 组装失败（mpv_create/render context 等任一步失败）返回 nil，外壳据此回退 IINA。
    static func create() -> MPVPlayerView? {
        // NSOpenGLPixelFormatAttribute 的常量是 C 宏没进 Swift，按 NSOpenGL.h 的数值写：
        // DoubleBuffer=5、Accelerated=73、OpenGLProfile=99、ProfileVersion3_2Core=0x3200
        let attrs: [NSOpenGLPixelFormatAttribute] = [
            NSOpenGLPixelFormatAttribute(99),
            NSOpenGLPixelFormatAttribute(0x3200),
            NSOpenGLPixelFormatAttribute(5),
            NSOpenGLPixelFormatAttribute(73),
            NSOpenGLPixelFormatAttribute(0),  // 属性表必须以 0 结尾，否则 pixel format 创建失败返回 nil
        ]
        guard let format = NSOpenGLPixelFormat(attributes: attrs) else { return nil }
        guard let view = MPVPlayerView(frame: NSRect(x: 0, y: 0, width: 640, height: 360),
                                       pixelFormat: format) else { return nil }
        // mpv_render_context_create 要求调用线程上已有 current GL context；
        // NSOpenGLView 的 openGLContext 懒创建，这里显式建好
        if view.openGLContext == nil {
            view.openGLContext = NSOpenGLContext(format: format, share: nil)
        }
        guard view.setupMpv() else {
            view.teardownPartial()
            return nil
        }
        return view
    }

    private init?(frame: NSRect, pixelFormat: NSOpenGLPixelFormat) {
        super.init(frame: frame, pixelFormat: pixelFormat)
        wantsBestResolutionOpenGLSurface = true  // 5K/Retina：backing 尺寸 = 物理像素，不糊
    }

    override var acceptsFirstResponder: Bool { true }

    // 鼠标移动事件归窗口管；OSC 的显隐与按钮 hover 靠 mouse move
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        window?.acceptsMouseMovedEvents = true
    }

    required init?(coder: NSCoder) {
        fatalError("不支持从 nib 创建")
    }

    // MARK: mpv 生命周期

    private func setupMpv() -> Bool {
        guard let mpv = mpv_create() else { return false }
        self.mpv = mpv
        mpv_set_option_string(mpv, "vo", "libmpv")
        mpv_set_option_string(mpv, "hwdec", "auto-safe")
        mpv_set_option_string(mpv, "osc", "yes")            // 用 mpv 自带控制条
        mpv_set_option_string(mpv, "input-default-bindings", "yes")
        mpv_set_option_string(mpv, "input-vo-keyboard", "yes")
        mpv_set_option_string(mpv, "keep-open", "no")
        mpv_set_option_string(mpv, "slang", MPVPlayerView.subLangs)
        mpv_set_option_string(mpv, "config", "no")          // 不读用户 mpv 配置
        // 日志：Finder 启动的 App 没有终端，mpv 的报错只能看这个文件
        let logDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/HomeCinema")
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        mpv_set_option_string(mpv, "log-file", logDir.appendingPathComponent("mpv.log").path)
        guard mpv_initialize(mpv) == 0 else { return false }
        guard createRenderContext() else { return false }

        mpv_observe_property(mpv, 0, "time-pos", MPV_FORMAT_DOUBLE)
        mpv_observe_property(mpv, 0, "duration", MPV_FORMAT_DOUBLE)
        mpv_set_wakeup_callback(mpv, { ctx in
            guard let ctx = ctx else { return }
            let view = Unmanaged<MPVPlayerView>.fromOpaque(ctx).takeUnretainedValue()
            view.scheduleEventDrain()
        }, Unmanaged.passUnretained(self).toOpaque())
        return true
    }

    private func createRenderContext() -> Bool {
        guard let mpv = mpv else { return false }
        openGLContext?.makeCurrentContext()

        let apiType = strdup(MPV_RENDER_API_TYPE_OPENGL)!
        defer { free(apiType) }

        var initParams = mpv_opengl_init_params(
            get_proc_address: { _, name in
                guard let name = name else { return nil }
                return dlsym(glFramework, name)
            },
            get_proc_address_ctx: nil)

        return withUnsafeMutablePointer(to: &initParams) { initParamsPtr in
            var params: [mpv_render_param] = [
                mpv_render_param(type: MPV_RENDER_PARAM_API_TYPE,
                                 data: UnsafeMutableRawPointer(apiType)),
                mpv_render_param(type: MPV_RENDER_PARAM_OPENGL_INIT_PARAMS,
                                 data: UnsafeMutableRawPointer(initParamsPtr)),
                // 不开 ADVANCED_CONTROL：开了就必须在每次 update 回调后调
                // mpv_render_context_update() 并按返回的标志决定是否渲染，否则画面会卡住
                mpv_render_param(),  // 参数表以全零项结尾
            ]
            var ctx: OpaquePointer?
            guard mpv_render_context_create(&ctx, mpv, &params) == 0, let gl = ctx else {
                return false
            }
            renderCtx = gl
            mpv_render_context_set_update_callback(gl, { cbCtx in
                guard let cbCtx = cbCtx else { return }
                let view = Unmanaged<MPVPlayerView>.fromOpaque(cbCtx).takeUnretainedValue()
                DispatchQueue.main.async { view.needsDisplay = true }
            }, Unmanaged.passUnretained(self).toOpaque())
            return true
        }
    }

    // MARK: 播放控制

    /// 播放 <path> 并从 <start> 秒开始。replace 语义：切下一集也走这里。
    func load(path: String, start: Double, title: String? = nil) {
        stateLock.lock(); _position = nil; _duration = nil; stateLock.unlock()  // 换片时清掉上一集的值
        // mpv ≥0.38：loadfile <url> <flags> <index> <options>，index 填 -1；选项在第 4 个参数
        var opts = "start=\(Int(start))"
        if let title = title {
            // 控制条上的片名用库里的，不用文件内嵌的标题标签（常常是错的）。
            // %字节数%值 是 mpv 选项列表里转义任意字符（含逗号）的写法
            opts += ",force-media-title=%\(title.utf8.count)%\(title)"
        }
        mpvCommand(["loadfile", path, "replace", "-1", opts])
    }

    /// 跳到绝对秒数（自动跳过片头用）。
    func seekAbsolute(_ seconds: Double) {
        mpvCommand(["seek", String(seconds), "absolute"])
    }

    /// 画面上显示一条限时提示（跳过片头/片尾用）。
    func showText(_ text: String, duration: Int) {
        mpvCommand(["show-text", text, String(duration)])
    }

    /// 结束播放并释放 mpv。必须主线程调用；GL context 当前时先 free render context。
    func stop() {
        eventQueue.sync { drainingStopped = true }  // 等在途事件循环退出
        if let mpv = mpv { mpv_set_wakeup_callback(mpv, nil, nil) }
        mpvCommand(["quit"])
        openGLContext?.makeCurrentContext()
        if let gl = renderCtx {
            mpv_render_context_set_update_callback(gl, nil, nil)
            mpv_render_context_free(gl)
            renderCtx = nil
        }
        if let mpv = mpv {
            mpv_terminate_destroy(mpv)
            self.mpv = nil
        }
        stateLock.lock(); _position = nil; _duration = nil; stateLock.unlock()
    }

    private func teardownPartial() {
        // setupMpv 中途失败时回收半成品（未上屏，无并发）。
        if let gl = renderCtx {
            mpv_render_context_free(gl)
            renderCtx = nil
        }
        if let mpv = mpv {
            mpv_terminate_destroy(mpv)
            self.mpv = nil
        }
    }

    // MARK: 状态读取（供外壳定时 POST 进度）

    var currentPosition: Double? {
        stateLock.lock(); defer { stateLock.unlock() }
        return _position
    }

    var currentDuration: Double? {
        stateLock.lock(); defer { stateLock.unlock() }
        return _duration
    }

    // MARK: mpv 事件

    private func scheduleEventDrain() {
        eventQueue.async { self.drainEvents() }
    }

    private func drainEvents() {
        if drainingStopped { return }
        while true {
            if drainingStopped { return }
            guard let mpv = self.mpv, let event = mpv_wait_event(mpv, 0) else { return }
            switch event.pointee.event_id {
            case MPV_EVENT_NONE:
                return  // 取完了
            case MPV_EVENT_PROPERTY_CHANGE:
                handlePropertyChange(event.pointee.data)
            case MPV_EVENT_END_FILE:
                handleEndFile(event.pointee.data)
            case MPV_EVENT_SHUTDOWN:
                let shutdown = onMpvShutdown
                DispatchQueue.main.async { shutdown?() }
                return
            default:
                continue
            }
        }
    }

    private func handlePropertyChange(_ data: UnsafeMutableRawPointer?) {
        guard let data = data else { return }
        let prop = data.assumingMemoryBound(to: mpv_event_property.self).pointee
        guard let name = prop.name else { return }
        let key: String?
        switch String(cString: name) {
        case "time-pos": key = "time-pos"
        case "duration": key = "duration"
        default: key = nil
        }
        guard let key = key else { return }
        if prop.format == MPV_FORMAT_DOUBLE, let valuePtr = prop.data {
            let value = valuePtr.assumingMemoryBound(to: Double.self).pointee
            stateLock.lock()
            if key == "time-pos" { _position = value } else { _duration = value }
            stateLock.unlock()
            if key == "time-pos" { notifyPosition(value) }
        }
        // 属性变不可用（文件结束）时保留最后已知值：清空的话结束时那次进度上报会被跳过
    }

    /// time-pos 变化回调（切主队列），限流每秒最多 4 次（间隔 >= 0.25 秒）。
    private func notifyPosition(_ value: Double) {
        let now = ProcessInfo.processInfo.systemUptime
        guard now - lastPositionNotify >= 0.25 else { return }
        lastPositionNotify = now
        let callback = onPosition
        DispatchQueue.main.async { callback?(value) }
    }

    private func handleEndFile(_ data: UnsafeMutableRawPointer?) {
        var eof = false
        if let data = data {
            let endFile = data.assumingMemoryBound(to: mpv_event_end_file.self).pointee
            eof = endFile.reason == MPV_END_FILE_REASON_EOF
        }
        let ended = onPlaybackEnded
        DispatchQueue.main.async { ended?(eof ? .eof : .user) }
    }

    // MARK: 命令

    @discardableResult
    private func mpvCommand(_ args: [String]) -> CInt {
        guard let mpv = mpv else { return -1 }
        var cArgs: [UnsafePointer<CChar>?] = args.map { UnsafePointer(strdup($0)) }
        cArgs.append(nil)
        defer {
            for p in cArgs where p != nil {
                free(UnsafeMutablePointer(mutating: p!))
            }
        }
        return mpv_command(mpv, &cArgs)
    }

    // MARK: 输入转发（libmpv 没有窗口收输入，OSC 与快捷键全靠这里）

    private func mpvKeyName(for event: NSEvent) -> String? {
        switch event.keyCode {
        case 49: return "SPACE"
        case 123: return "LEFT"
        case 124: return "RIGHT"
        case 125: return "DOWN"
        case 126: return "UP"
        case 53: return "ESC"
        default: break
        }
        switch event.charactersIgnoringModifiers {
        case " ": return "SPACE"
        case "f": return "f"
        case "m": return "m"
        case "j": return "j"          // 切字幕
        case "#": return "SHARP"      // 切音轨
        case "[": return "BRACKET_LEFT"
        case "]": return "BRACKET_RIGHT"
        default: return nil
        }
    }

    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 {  // ESC 的结束语义（全屏先退全屏、再结束播放）归外壳管
            let escape = onEscape
            DispatchQueue.main.async { escape?() }
            return
        }
        if event.charactersIgnoringModifiers == "f" {
            // 嵌在 App 里时 mpv 自己的 fullscreen 不起作用，全屏要由窗口来切
            toggleFullScreenAllowingFloat()
            return
        }
        guard let name = mpvKeyName(for: event) else { super.keyDown(with: event); return }
        mpvCommand(["keypress", name])
    }

    override func mouseMoved(with event: NSEvent) {
        sendMouseLocation(event)
        scheduleCursorHide()
    }

    // MARK: 鼠标静止 2 秒后隐藏光标（画面上方）；一动就由系统自动显示回来

    private var cursorHideTimer: Timer?

    func scheduleCursorHide() {
        cursorHideTimer?.invalidate()
        cursorHideTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: false) { [weak self] _ in
            guard let self = self, let window = self.window, window.isKeyWindow else { return }
            // 光标不在画面上（比如移到了菜单栏或别的窗口）就不藏
            let loc = self.convert(window.mouseLocationOutsideOfEventStream, from: nil)
            guard self.bounds.contains(loc) else { return }
            NSCursor.setHiddenUntilMouseMoves(true)
        }
    }

    override func viewDidMoveToSuperview() {
        super.viewDidMoveToSuperview()
        if superview == nil {
            cursorHideTimer?.invalidate()
            NSCursor.setHiddenUntilMouseMoves(false)  // 回到海报墙时光标必须可见
        } else {
            scheduleCursorHide()  // 开播后鼠标不动也会在 2 秒后隐藏
        }
    }

    override func mouseDragged(with event: NSEvent) {
        sendMouseLocation(event)  // OSC 拖进度条要连续 move
        scheduleCursorHide()
    }

    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            // 双击切全屏（mpv 自己的双击全屏嵌在 App 里不起作用，由窗口来切）
            toggleFullScreenAllowingFloat()
            return
        }
        mpvCommand(["keydown", "MBTN_LEFT"])
    }

    /// 置顶（.floating）的窗口 macOS 不让进全屏，先降回普通层级；退出全屏后由外壳恢复置顶
    private func toggleFullScreenAllowingFloat() {
        guard let window = window else { return }
        if !window.styleMask.contains(.fullScreen) { window.level = .normal }
        window.toggleFullScreen(nil)
    }

    override func mouseUp(with event: NSEvent) {
        if event.clickCount == 2 { return }
        mpvCommand(["keyup", "MBTN_LEFT"])
    }

    override func scrollWheel(with event: NSEvent) {
        if event.deltaY > 0 {
            mpvCommand(["keypress", "WHEEL_UP"])
        } else if event.deltaY < 0 {
            mpvCommand(["keypress", "WHEEL_DOWN"])
        }
    }

    private func sendMouseLocation(_ event: NSEvent) {
        let loc = convert(event.locationInWindow, from: nil)      // 视图坐标，原点左下
        let backing = convertToBacking(bounds)
        let pixel = convertToBacking(loc)                          // 像素坐标，原点仍左下
        // mpv 的 mouse 命令要原点在左上的像素坐标
        mpvCommand(["mouse", String(Int(pixel.x)), String(Int(backing.height - pixel.y))])
    }

    // MARK: 绘制

    override func draw(_ dirtyRect: NSRect) {
        guard let gl = renderCtx else { super.draw(dirtyRect); return }
        openGLContext?.makeCurrentContext()
        let backing = convertToBacking(bounds)
        let fboPtr = UnsafeMutablePointer<mpv_opengl_fbo>.allocate(capacity: 1)
        defer { fboPtr.deallocate() }
        // 尺寸必须是像素尺寸（convertToBacking 后，5K 屏是 2 倍），FBO 0 = 默认帧缓冲
        fboPtr.pointee = mpv_opengl_fbo(fbo: 0,
                                        w: CInt(backing.width),
                                        h: CInt(backing.height),
                                        internal_format: 0)
        let flipPtr = UnsafeMutablePointer<CInt>.allocate(capacity: 1)
        defer { flipPtr.deallocate() }
        flipPtr.pointee = 1
        var params: [mpv_render_param] = [
            mpv_render_param(type: MPV_RENDER_PARAM_OPENGL_FBO,
                             data: UnsafeMutableRawPointer(fboPtr)),
            mpv_render_param(type: MPV_RENDER_PARAM_FLIP_Y,
                             data: UnsafeMutableRawPointer(flipPtr)),
            mpv_render_param(),
        ]
        mpv_render_context_render(gl, &params)
        openGLContext?.flushBuffer()
    }

    override func viewDidEndLiveResize() {
        super.viewDidEndLiveResize()
        openGLContext?.update()
        needsDisplay = true
    }

    override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        openGLContext?.update()
        needsDisplay = true
    }
}
