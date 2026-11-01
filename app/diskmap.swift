// diskmap.app (A′, K14): the page of `diskmap ui` in a native window.
//
// The window knows nothing about disks. It starts `python -m cli ui` from the bundle, as
// the user who opened the app, reads the one-time link the server prints, and shows that
// page — the same server, token, cookie and checks as in a browser, with no new rights:
// writing stays `sudo diskmap apply` in Terminal, which the server opens (K14).
//
// What a browser did and a web view does not, the window does itself:
// - links that lead off the server (Support, mailto: Feedback) go to the default browser
//   and mail client (D52), never into the window;
// - the app menu's "About diskmap" opens the page's own About box (showAbout(), D52);
// - the server lives exactly as long as the app: the app holds the server's stdin, and
//   the server stops when it closes (`--exit-on-eof`), however the app ends.

import AppKit
import WebKit

// Set by app/build.sh, one of the two (D53, D54):
// - the interpreter the bundle was built and checked with; diskmap's code is in the bundle;
// - the `diskmap` command the bundle runs, code and all — Homebrew's, through its opt/ path,
//   so a copy of the app outside the formula runs whatever version is installed.
let pythonKey = "DiskmapPython"
let commandKey = "DiskmapCommand"
let linkPrefix = "diskmap ui: "
let startTimeout: TimeInterval = 15

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var lifeline: Pipe?  // the server's stdin; closing it stops the server
    var origin: URL?  // http://127.0.0.1:PORT/ once the server said its link
    var errors = Data()  // the server's stderr, for the message if it fails
    var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = mainMenu()
        makeWindow()
        startServer()
        NSApp.activate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        try? lifeline?.fileHandleForWriting.close()
        guard let s = server, s.isRunning else { return }
        let deadline = Date().addingTimeInterval(3)
        while s.isRunning && Date() < deadline { usleep(50_000) }
        if s.isRunning { s.terminate() }
    }

    // MARK: the server

    func startServer() {
        let ui = ["ui", "--no-open", "--exit-on-eof"]
        let p = Process()
        if let command = Bundle.main.object(forInfoDictionaryKey: commandKey) as? String {
            guard FileManager.default.isExecutableFile(atPath: command) else {
                return fail("diskmap is not installed.", "\(command) is missing. Install it: brew install zapotylok/diskmap/diskmap")
            }
            p.executableURL = URL(fileURLWithPath: command)
            p.arguments = ui
            p.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        } else {
            guard let python = Bundle.main.object(forInfoDictionaryKey: pythonKey) as? String,
                FileManager.default.isExecutableFile(atPath: python)
            else {
                return fail("No Python to run diskmap.", "Rebuild the app with app/build.sh on this Mac.")
            }
            guard let resources = Bundle.main.resourcePath else {
                return fail("The app bundle has no Resources folder.", "Rebuild the app with app/build.sh.")
            }
            p.executableURL = URL(fileURLWithPath: python)
            // -B: write no bytecode into the bundle (build.sh compiled it); -E -s: no PYTHON*
            // variables, no user site-packages — only the stdlib and the bundle's own code.
            p.arguments = ["-B", "-E", "-s", "-m", "cli"] + ui
            p.currentDirectoryURL = URL(fileURLWithPath: resources).appendingPathComponent("diskmap")
        }
        let shown = ([p.executableURL!.path] + (p.arguments ?? [])).joined(separator: " ")
        let input = Pipe(), output = Pipe(), stderr = Pipe()
        p.standardInput = input
        p.standardOutput = output
        p.standardError = stderr
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.serverEnded(proc) }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            DispatchQueue.main.async {
                guard let self else { return }
                self.errors.append(d)
                if self.errors.count > 64 * 1024 { self.errors = self.errors.suffix(32 * 1024) }
            }
        }
        do {
            try p.run()
        } catch {
            return fail("Could not start diskmap ui.", "\(shown): \(error.localizedDescription)")
        }
        server = p
        lifeline = input
        readLink(output.fileHandleForReading)
        DispatchQueue.main.asyncAfter(deadline: .now() + startTimeout) { [weak self] in
            guard let self, self.origin == nil, !self.quitting else { return }
            self.fail("diskmap ui did not start in \(Int(startTimeout)) s.", self.errorText())
        }
    }

    /// The first line of the server's stdout is its link; the rest is read to the end so
    /// the pipe never fills.
    func readLink(_ h: FileHandle) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var pending = Data()
            var said = false
            while true {
                let d = h.availableData
                if d.isEmpty { return }
                if said { continue }
                pending.append(d)
                guard let nl = pending.firstIndex(of: 0x0A) else { continue }
                said = true
                let line = String(decoding: pending[..<nl], as: UTF8.self)
                DispatchQueue.main.async { self?.serverSaid(line) }
            }
        }
    }

    func serverSaid(_ line: String) {
        guard line.hasPrefix(linkPrefix), let link = URL(string: String(line.dropFirst(linkPrefix.count))),
            link.scheme == "http", link.host == "127.0.0.1", let port = link.port
        else {
            return fail("diskmap ui said something unexpected.", line)
        }
        origin = URL(string: "http://127.0.0.1:\(port)/")
        web.load(URLRequest(url: link))
    }

    func serverEnded(_ p: Process) {
        if quitting { return }
        fail("diskmap ui stopped (exit status \(p.terminationStatus)).", errorText())
    }

    func errorText() -> String {
        let text = String(decoding: errors, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? "It said nothing on stderr." : String(text.suffix(2000))
    }

    func fail(_ message: String, _ detail: String) {
        if quitting { return }
        quitting = true
        let a = NSAlert()
        a.alertStyle = .critical
        a.messageText = message
        a.informativeText = detail
        a.runModal()
        NSApp.terminate(nil)
    }

    // MARK: the web view

    func makeWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()  // the session cookie dies with the app
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.isInspectable = true  // Safari ▸ Develop, as a browser's inspector was
        web.setValue(false, forKey: "drawsBackground")  // no white flash before the page in dark mode
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1200, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "diskmap"
        window.minSize = NSSize(width: 720, height: 480)
        window.tabbingMode = .disallowed
        window.contentView = web
        if !window.setFrameUsingName("main") { window.center() }
        window.setFrameAutosaveName("main")
        window.makeKeyAndOrderFront(nil)
    }

    func isServer(_ url: URL) -> Bool {
        guard let origin else { return false }
        return url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port
    }

    /// A link off the server goes to the app that handles it — the default browser, the mail
    /// client — never into this window. Anything else that is not the server is dropped.
    func openOutside(_ url: URL) {
        if ["https", "http", "mailto"].contains(url.scheme?.lowercased() ?? "") {
            NSWorkspace.shared.open(url)
        }
    }

    func webView(
        _ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
        decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if isServer(url) { return decisionHandler(.allow) }
        if action.navigationType == .linkActivated { openOutside(url) }
        decisionHandler(.cancel)
    }

    // target="_blank": no second window — the link goes outside.
    func webView(
        _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
        for action: WKNavigationAction, windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = action.request.url, !isServer(url) { openOutside(url) }
        return nil
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    // MARK: the menu

    @objc func showAbout(_ sender: Any?) {
        web.evaluateJavaScript("showAbout()")
    }

    @objc func reloadPage(_ sender: Any?) { web.reload() }
    @objc func zoomIn(_ sender: Any?) { web.pageZoom = min(web.pageZoom + 0.1, 3) }
    @objc func zoomOut(_ sender: Any?) { web.pageZoom = max(web.pageZoom - 0.1, 0.5) }
    @objc func actualSize(_ sender: Any?) { web.pageZoom = 1 }

    func mainMenu() -> NSMenu {
        let bar = NSMenu()
        func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            let top = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            top.submenu = m
            bar.addItem(top)
            return m
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ mods: NSEvent.ModifierFlags = .command, to target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        _ = submenu("diskmap", [
            item("About diskmap", #selector(showAbout(_:)), to: self),
            .separator(),
            item("Hide diskmap", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item("Quit diskmap", #selector(NSApplication.terminate(_:)), "q"),
        ])
        // Without these the page's text fields get no ⌘C/⌘V: a web view takes them from the menu.
        _ = submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = submenu("View", [
            item("Reload", #selector(reloadPage(_:)), "r", to: self),
            .separator(),
            item("Actual Size", #selector(actualSize(_:)), "0", to: self),
            item("Zoom In", #selector(zoomIn(_:)), "+", to: self),
            item("Zoom Out", #selector(zoomOut(_:)), "-", to: self),
        ])
        let windows = submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:))),
            .separator(),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.windowsMenu = windows
        return bar
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
