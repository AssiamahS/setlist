// Setlist.app — native macOS wrapper for the setlist + amapiano local servers.
// Supervises both Flask servers (spawns them if their ports are quiet), shows
// the web UI in a WKWebView window, and keeps a menu-bar status item alive so
// closing the window doesn't kill downloads in flight.
// Built with swiftc directly (CLT only, no Xcode): see build.sh.

import AppKit
import WebKit

// MARK: - Server supervision

final class ManagedServer {
    let name: String
    let dir: URL
    let script: String
    let healthURL: URL
    var proc: Process?
    var up = false
    var lastSpawn = Date.distantPast

    init(name: String, dir: String, script: String, health: String) {
        self.name = name
        self.dir = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(dir)
        self.script = script
        self.healthURL = URL(string: health)!
    }

    var python: URL { dir.appendingPathComponent(".venv/bin/python") }

    func spawnIfNeeded() {
        guard proc?.isRunning != true,
              Date().timeIntervalSince(lastSpawn) > 15,
              FileManager.default.fileExists(atPath: python.path) else { return }
        lastSpawn = Date()
        let p = Process()
        p.executableURL = python
        p.arguments = [script]
        p.currentDirectoryURL = dir
        let logDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("setlist/macos/logs")
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        let log = logDir.appendingPathComponent("\(name).log")
        if !FileManager.default.fileExists(atPath: log.path) {
            FileManager.default.createFile(atPath: log.path, contents: nil)
        }
        if let fh = try? FileHandle(forWritingTo: log) {
            fh.seekToEndOfFile()
            p.standardOutput = fh
            p.standardError = fh
        }
        do { try p.run(); proc = p } catch {
            NSLog("Setlist: failed to spawn \(name): \(error)")
        }
    }

    func stopIfOwned() {
        if let p = proc, p.isRunning { p.terminate() }
        proc = nil
    }
}

// MARK: - App delegate

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKUIDelegate, WKNavigationDelegate {
    let setlist = ManagedServer(name: "setlist", dir: "setlist", script: "server.py",
                                health: "http://127.0.0.1:8787/api/health")
    let amapiano = ManagedServer(name: "amapiano", dir: "amapiano", script: "server.py",
                                 health: "http://127.0.0.1:8766/api/downloads")
    var window: NSWindow!
    var webView: WKWebView!
    var statusItem: NSStatusItem!
    var setlistMenuItem: NSMenuItem!
    var amapianoMenuItem: NSMenuItem!
    var timer: Timer?
    var uiLoaded = false

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMainMenu()
        buildWindow()
        buildStatusItem()
        NSApp.activate(ignoringOtherApps: true)
        tick()
        timer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    // Closing the window hides it; downloads keep running under the menu-bar item.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        window.orderOut(nil)
        return false
    }

    // MARK: UI construction

    func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 840),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Setlist"
        window.minSize = NSSize(width: 880, height: 560)
        window.center()
        window.setFrameAutosaveName("SetlistMain")
        window.delegate = self

        let cfg = WKWebViewConfiguration()
        webView = WKWebView(frame: window.contentView!.bounds, configuration: cfg)
        webView.autoresizingMask = [.width, .height]
        webView.uiDelegate = self
        webView.navigationDelegate = self
        window.contentView!.addSubview(webView)
        showWaitingPage()
        window.makeKeyAndOrderFront(nil)
    }

    func showWaitingPage() {
        webView.loadHTMLString("""
            <body style="background:#0c0e12;color:#8b93a1;font:15px -apple-system;
                         display:flex;align-items:center;justify-content:center;height:96vh">
              <div style="text-align:center">
                <div style="font-size:26px;letter-spacing:4px;font-weight:800;color:#e8eaee">
                  SET<span style="color:#33d17a">LIST</span></div>
                <div style="margin-top:10px">starting local servers…</div>
              </div></body>
            """, baseURL: nil)
    }

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "♪SL"
        let menu = NSMenu()
        setlistMenuItem = NSMenuItem(title: "setlist: checking…", action: nil, keyEquivalent: "")
        amapianoMenuItem = NSMenuItem(title: "amapiano: checking…", action: nil, keyEquivalent: "")
        menu.addItem(setlistMenuItem)
        menu.addItem(amapianoMenuItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open Setlist", action: #selector(openWindow), keyEquivalent: "o")
        menu.addItem(withTitle: "Open in Browser", action: #selector(openInBrowser), keyEquivalent: "b")
        menu.addItem(withTitle: "Restart Servers", action: #selector(restartServers), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit (servers keep running)", action: #selector(quitKeep), keyEquivalent: "q")
        menu.addItem(withTitle: "Quit & Stop Servers", action: #selector(quitStop), keyEquivalent: "")
        menu.items.forEach { $0.target = self }
        statusItem.menu = menu
    }

    func buildMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Setlist",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Setlist", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Setlist", action: #selector(quitKeep), keyEquivalent: "q")
        appMenu.items.last?.target = self
        appItem.submenu = appMenu

        // Edit menu so ⌘C/⌘V/⌘A work inside the web UI's text fields
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reloadUI), keyEquivalent: "r")
        view.items.last?.target = self
        viewItem.submenu = view

        NSApp.mainMenu = main
    }

    // MARK: actions

    @objc func openWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func openInBrowser() {
        NSWorkspace.shared.open(URL(string: "http://localhost:8787/")!)
    }

    @objc func reloadUI() {
        uiLoaded = false
        showWaitingPage()
        tick()
    }

    @objc func restartServers() {
        [setlist, amapiano].forEach { $0.stopIfOwned(); $0.lastSpawn = .distantPast }
        uiLoaded = false
        showWaitingPage()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.tick() }
    }

    @objc func quitKeep() {
        // leave both servers (and any in-flight downloads) running
        NSApp.terminate(nil)
    }

    @objc func quitStop() {
        [setlist, amapiano].forEach { $0.stopIfOwned() }
        NSApp.terminate(nil)
    }

    // MARK: health loop

    func tick() {
        for server in [setlist, amapiano] {
            var req = URLRequest(url: server.healthURL)
            req.timeoutInterval = 2
            URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
                let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                DispatchQueue.main.async {
                    guard let self else { return }
                    server.up = ok
                    if !ok { server.spawnIfNeeded() }
                    self.refreshStatus()
                    if server === self.setlist, ok, !self.uiLoaded {
                        self.uiLoaded = true
                        self.webView.load(URLRequest(url: URL(string: "http://localhost:8787/")!))
                    }
                }
            }.resume()
        }
    }

    func refreshStatus() {
        setlistMenuItem.title = "setlist: \(setlist.up ? "● running" : "○ starting…")"
        amapianoMenuItem.title = "amapiano: \(amapiano.up ? "● running" : "○ starting…")"
        statusItem.button?.appearsDisabled = !setlist.up
    }

    // MARK: web view: keep localhost inside, push everything else to the browser

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, let host = url.host,
           !["localhost", "127.0.0.1"].contains(host) {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
