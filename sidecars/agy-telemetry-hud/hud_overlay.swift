import Cocoa
import WebKit
import CoreGraphics
import Darwin

class HudBridgeHandler: NSObject, WKScriptMessageHandler {
    weak var controller: OverlayController?
    init(controller: OverlayController) {
        self.controller = controller
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let dict = message.body as? [String: Any],
              let action = dict["action"] as? String else { return }
        DispatchQueue.main.async {
            self.controller?.handleBridgeAction(action: action, payload: dict)
        }
    }
}

class TransparentWebView: WKWebView {
    override var isOpaque: Bool { return false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { return true }
}

struct AntigravityLayoutState {
    var isAuxPaneOpen: Bool = false
    var isOverviewOpen: Bool = true
    var auxPaneWidthPct: CGFloat = 34.58
    var sidebarWidth: CGFloat = 256.0
}

class OverlayController: NSObject, NSApplicationDelegate {
    var panel: NSPanel!
    var webView: TransparentWebView!
    var syncTimer: Timer?
    var dragTimer: Timer?

    var isDragging: Bool = false
    var dragStartMouse: NSPoint = .zero
    var dragStartOrigin: NSPoint = .zero
    var lastAgyFrame: NSRect?
    var lastChatPaneRightX: CGFloat?

    var activeCid: String = ""
    var isViewingConversation: Bool = true
    var wasAgyAppFrontmost: Bool = false
    var agyBecameFrontmostAt: TimeInterval = 0
    var pendingFocusAnnotation: Bool = false
    var lastMaxViewNs: Int64 = -1
    var lastMaxMtimeNs: Int64 = -1

    var cachedStorageMtime: TimeInterval = 0
    var cachedCid: String = ""
    var cachedLayout = AntigravityLayoutState()
    var annotationCache: [String: (mtimeNs: Int64, viewNs: Int64)] = [:]
    let viewTimeRegex = try? NSRegularExpression(pattern: "last_user_view_time:\\s*\\{\\s*seconds:\\s*(\\d+)(?:\\s*nanos:\\s*(\\d+))?")

    // Direct window pixel inspection (via CGWindowListCreateImage on Antigravity wid)
    typealias CGWindowListCreateImageFn = @convention(c) (CGRect, CGWindowListOption, CGWindowID, CGWindowImageOption) -> Unmanaged<CGImage>?
    lazy var cgCreateImage: CGWindowListCreateImageFn? = {
        guard let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "CGWindowListCreateImage") else { return nil }
        return unsafeBitCast(sym, to: CGWindowListCreateImageFn.self)
    }()
    let visionQueue = DispatchQueue(label: "agy.hud.vision", qos: .userInteractive)
    var isVisionRunning: Bool = false
    var hasVisionResult: Bool = false
    var visionOverviewVisible: Bool = false
    var visionOverviewLeftX: CGFloat = 0
    var visionOverviewBottomOffset: CGFloat = 352.0

    // Exact width of the native Overview card (318pt in window coordinates)
    let cardSize = NSSize(width: 318, height: 428)
    // Default anchor relative to the Chat Pane's right edge and top edge (fallback when ScreenCaptureAccess is not granted)
    let defaultRightOffset: CGFloat = 10.0
    let defaultTopOffset: CGFloat = 352.0
    let minWindowHeightForOverview: CGFloat = 620.0

    let defaults = UserDefaults.standard
    var port: Int = 18765

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        if let envPort = ProcessInfo.processInfo.environment["HUD_PORT"], let p = Int(envPort), p > 0 {
            port = p
        }

        let rect = NSRect(x: 1000, y: 400, width: cardSize.width, height: cardSize.height)

        panel = NSPanel(
            contentRect: rect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isMovableByWindowBackground = false
        panel.hidesOnDeactivate = false

        let contentController = WKUserContentController()
        let bridge = HudBridgeHandler(controller: self)
        contentController.add(bridge, name: "hudBridge")

        let cfg = WKWebViewConfiguration()
        cfg.userContentController = contentController

        webView = TransparentWebView(frame: NSRect(origin: .zero, size: cardSize), configuration: cfg)
        webView.wantsLayer = true
        webView.layer?.backgroundColor = NSColor.clear.cgColor
        webView.setValue(false, forKey: "drawsBackground")
        webView.autoresizingMask = [.width, .height]

        panel.contentView = webView

        loadHudUrl()

        syncTimer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            self?.syncWithAntigravityWindow()
        }
        syncWithAntigravityWindow()
    }

    func loadHudUrl() {
        let urlStr = "http://127.0.0.1:\(port)/?view=overlay"
        if let url = URL(string: urlStr) {
            webView.load(URLRequest(url: url))
        }
    }

    func resolveDisplayedConversationId(now: TimeInterval) -> String {
        let annDir = (NSHomeDirectory() as NSString).appendingPathComponent(".gemini/antigravity/annotations")
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: annDir) else {
            return activeCid
        }
        var bestViewNs: Int64 = -1
        var bestMtimeNs: Int64 = -1
        var bestCid: String = activeCid
        var allViewTimes: [Int64] = []

        for name in entries {
            guard name.hasSuffix(".pbtxt") else { continue }
            let cid = String(name.dropLast(6))
            let fullPath = (annDir as NSString).appendingPathComponent(name)
            var st = stat()
            guard stat(fullPath, &st) == 0 else { continue }
            let mtimeNs = Int64(st.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(st.st_mtimespec.tv_nsec)

            var viewNs: Int64 = 0
            if let cached = annotationCache[cid], cached.mtimeNs == mtimeNs {
                viewNs = cached.viewNs
            } else {
                if let txt = try? String(contentsOfFile: fullPath, encoding: .utf8),
                   let rx = viewTimeRegex,
                   let match = rx.firstMatch(in: txt, options: [], range: NSRange(txt.startIndex..., in: txt)) {
                    let secRange = match.range(at: 1)
                    if secRange.location != NSNotFound, let r1 = Range(secRange, in: txt), let sec = Int64(txt[r1]) {
                        var nanos: Int64 = 0
                        let nanoRange = match.range(at: 2)
                        if nanoRange.location != NSNotFound, let r2 = Range(nanoRange, in: txt), let n = Int64(txt[r2]) {
                            nanos = n
                        }
                        viewNs = sec * 1_000_000_000 + nanos
                    }
                }
                annotationCache[cid] = (mtimeNs: mtimeNs, viewNs: viewNs)
            }

            if viewNs > 0 {
                allViewTimes.append(viewNs)
            }
            if viewNs > bestViewNs || (viewNs == bestViewNs && mtimeNs > bestMtimeNs) {
                bestViewNs = viewNs
                bestMtimeNs = mtimeNs
                bestCid = cid
            }
        }

        guard bestViewNs > 0 else { return activeCid }

        var countAtBestViewNs = 0
        for v in allViewTimes {
            if abs(v - bestViewNs) < 5_000_000 {
                countAtBestViewNs += 1
            }
        }

        if lastMaxViewNs == -1 {
            lastMaxViewNs = bestViewNs
            lastMaxMtimeNs = bestMtimeNs
            activeCid = bestCid
            let savedViewNs = (defaults.object(forKey: "agy_hud_v3_lastMaxViewNs") as? NSNumber)?.int64Value ?? -1
            if savedViewNs == bestViewNs, let savedViewing = defaults.object(forKey: "agy_hud_v3_isViewingConv") as? Bool {
                isViewingConversation = savedViewing
            } else {
                isViewingConversation = true
            }
            return activeCid
        }

        if bestViewNs > lastMaxViewNs || (bestViewNs == lastMaxViewNs && bestMtimeNs > lastMaxMtimeNs) {
            let nowNs = Int64(now * 1_000_000_000.0)
            // When switching A -> B, Antigravity writes A.pbtxt then B.pbtxt (~10-30ms apart).
            // Wait 1 tick (60ms) if only activeCid was updated so B.pbtxt finishes writing.
            if countAtBestViewNs == 1 && bestCid == activeCid && isViewingConversation && (nowNs - bestMtimeNs) < 60_000_000 {
                return activeCid
            }

            if countAtBestViewNs >= 2 {
                // Switched between two conversations: d([oldCid, newCid])
                isViewingConversation = true
                activeCid = bestCid
            } else {
                // Only 1 file updated at bestViewNs
                if !isViewingConversation {
                    // Switched from "+ New Conversation" / Automations / Customizations back to a conversation: d([undefined, bestCid])
                    isViewingConversation = true
                    activeCid = bestCid
                } else if bestCid != activeCid {
                    // Started a brand new conversation from "+ New Conversation" or switched to bestCid
                    isViewingConversation = true
                    activeCid = bestCid
                } else {
                    // Only activeCid.pbtxt was updated while already viewing activeCid:
                    // Either windowFocusChanged (iJa) within ~1.2s of app focus, OR viewChanged to "+ New Conversation" (d([activeCid, undefined]))!
                    if pendingFocusAnnotation && (now - agyBecameFrontmostAt < 1.2) {
                        pendingFocusAnnotation = false
                        isViewingConversation = true
                    } else {
                        isViewingConversation = false
                    }
                }
            }

            lastMaxViewNs = bestViewNs
            lastMaxMtimeNs = bestMtimeNs
            defaults.set(NSNumber(value: bestViewNs), forKey: "agy_hud_v3_lastMaxViewNs")
            defaults.set(isViewingConversation, forKey: "agy_hud_v3_isViewingConv")
        }

        return activeCid
    }

    func readAntigravityLayoutState(now: TimeInterval) -> AntigravityLayoutState {
        let prevCid = activeCid
        let displayedCid = resolveDisplayedConversationId(now: now)
        if !displayedCid.isEmpty && displayedCid != prevCid {
            webView.evaluateJavaScript("if (window.onDisplayedCidChanged) window.onDisplayedCidChanged();", completionHandler: nil)
        }

        let storagePath = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/Antigravity/app_storage.json")
        var st = stat()
        guard stat(storagePath, &st) == 0 else {
            return cachedLayout
        }
        let mtime = Double(st.st_mtimespec.tv_sec) + Double(st.st_mtimespec.tv_nsec) / 1_000_000_000.0
        if mtime == cachedStorageMtime && activeCid == cachedCid && cachedStorageMtime > 0 {
            return cachedLayout
        }

        guard let data = try? Data(contentsOf: URL(fileURLWithPath: storagePath)),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return cachedLayout
        }

        var state = AntigravityLayoutState()

        if let wStr = root["auxPaneWidth"] as? String, let wVal = Double(wStr), wVal > 5.0, wVal <= 100.0 {
            state.auxPaneWidthPct = CGFloat(wVal)
        } else if let wNum = root["auxPaneWidth"] as? NSNumber {
            state.auxPaneWidthPct = CGFloat(wNum.doubleValue)
        }

        if let sStr = root["sidebarWidth"] as? String, let sVal = Double(sStr), sVal >= 180.0, sVal <= 500.0 {
            state.sidebarWidth = CGFloat(sVal)
        } else if let sNum = root["sidebarWidth"] as? NSNumber {
            state.sidebarWidth = CGFloat(sNum.doubleValue)
        }

        let targetCid = activeCid
        if let auxSessionStr = root["aux-pane-session"] as? String,
           let auxData = auxSessionStr.data(using: .utf8),
           let auxObj = try? JSONSerialization.jsonObject(with: auxData) as? [String: Any],
           let convPanes = auxObj["conversationPanes"] as? [String: Any] {
            if !targetCid.isEmpty, let paneInfo = convPanes[targetCid] as? [String: Any] {
                if let isOpen = paneInfo["isPaneOpen"] as? Bool {
                    state.isAuxPaneOpen = isOpen
                }
                if let sidebarStates = paneInfo["sidebarOpenStates"] as? [String: Any],
                   let ovOpen = sidebarStates["overview"] as? Bool {
                    state.isOverviewOpen = ovOpen
                }
            }
        }

        // Also check aux-pane-v2-session for overviewPanelOpen
        if !targetCid.isEmpty,
           let auxV2Str = root["aux-pane-v2-session"] as? String,
           let auxV2Data = auxV2Str.data(using: .utf8),
           let auxV2Obj = try? JSONSerialization.jsonObject(with: auxV2Data) as? [String: Any],
           let ovMap = auxV2Obj["overviewPanelOpen"] as? [String: Any],
           let ovOpen = ovMap[targetCid] as? Bool {
            state.isOverviewOpen = ovOpen
        }

        cachedStorageMtime = mtime
        cachedCid = activeCid
        cachedLayout = state
        return state
    }

    // Anchored relative to chatPaneRightX and window maxY (top edge)
    func savedTopRightOffsets() -> (CGFloat, CGFloat) {
        let kr = "agy_hud_v3_rightOffset"
        let kt = "agy_hud_v3_topOffset"
        if defaults.object(forKey: kr) != nil && defaults.object(forKey: kt) != nil {
            return (CGFloat(defaults.double(forKey: kr)), CGFloat(defaults.double(forKey: kt)))
        }
        return (defaultRightOffset, defaultTopOffset)
    }

    func saveTopRightOffsets(panelFrame: NSRect, agyFrame: NSRect, chatPaneRightX: CGFloat) {
        let rightOff = max(4.0, min(agyFrame.width - panelFrame.width - 4.0, chatPaneRightX - panelFrame.maxX))
        let topOff = max(40.0, min(agyFrame.height - panelFrame.height - 4.0, agyFrame.maxY - panelFrame.maxY))
        defaults.set(Double(rightOff), forKey: "agy_hud_v3_rightOffset")
        defaults.set(Double(topOff), forKey: "agy_hud_v3_topOffset")
    }

    func startNativeDragTracking() {
        isDragging = true
        dragStartMouse = NSEvent.mouseLocation
        dragStartOrigin = panel.frame.origin
        dragTimer?.invalidate()
        dragTimer = Timer.scheduledTimer(withTimeInterval: 0.016, repeats: true) { [weak self] timer in
            guard let self = self else { timer.invalidate(); return }
            let buttons = NSEvent.pressedMouseButtons
            if (buttons & 1) == 0 {
                timer.invalidate()
                self.dragTimer = nil
                self.isDragging = false
                if let agy = self.lastAgyFrame {
                    let rightEdge = self.lastChatPaneRightX ?? agy.maxX
                    self.saveTopRightOffsets(panelFrame: self.panel.frame, agyFrame: agy, chatPaneRightX: rightEdge)
                }
                return
            }
            let curMouse = NSEvent.mouseLocation
            let dx = curMouse.x - self.dragStartMouse.x
            let dy = curMouse.y - self.dragStartMouse.y
            var newOrigin = NSPoint(x: self.dragStartOrigin.x + dx, y: self.dragStartOrigin.y + dy)
            if let agy = self.lastAgyFrame {
                newOrigin.x = self.clamp(newOrigin.x, min: agy.origin.x + 4, max: agy.maxX - self.panel.frame.width - 4)
                newOrigin.y = self.clamp(newOrigin.y, min: agy.origin.y + 4, max: agy.maxY - self.panel.frame.height - 40)
            }
            self.panel.setFrameOrigin(newOrigin)
        }
    }

    func handleBridgeAction(action: String, payload: [String: Any]) {
        switch action {
        case "startDrag":
            startNativeDragTracking()

        case "resetPosition":
            defaults.removeObject(forKey: "agy_hud_v3_rightOffset")
            defaults.removeObject(forKey: "agy_hud_v3_topOffset")
            syncWithAntigravityWindow()

        default:
            break
        }
    }

    func clamp(_ val: CGFloat, min minVal: CGFloat, max maxVal: CGFloat) -> CGFloat {
        return Swift.max(minVal, Swift.min(maxVal, val))
    }

    func findAntigravityWindowInfo() -> (wid: CGWindowID, cgBounds: CGRect, cocoaFrame: NSRect)? {
        guard let primaryScreen = NSScreen.screens.first else { return nil }
        let primaryH = primaryScreen.frame.height

        let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let infoList = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
            return nil
        }

        var bestInfo: (wid: CGWindowID, cgBounds: CGRect, cocoaFrame: NSRect)?
        var bestArea: CGFloat = 0

        for entry in infoList {
            let layer = entry[kCGWindowLayer as String] as? Int ?? -1
            if layer != 0 { continue }
            let owner = (entry[kCGWindowOwnerName as String] as? String ?? "").lowercased()
            if !owner.contains("antigravity") { continue }
            guard let wid = entry[kCGWindowNumber as String] as? CGWindowID,
                  let b = entry[kCGWindowBounds as String] as? [String: CGFloat],
                  let x = b["X"], let y = b["Y"], let w = b["Width"], let h = b["Height"],
                  w >= 320, h >= 240 else { continue }

            let area = w * h
            if area > bestArea {
                bestArea = area
                let cocoaY = primaryH - (y + h)
                let cgRect = CGRect(x: x, y: y, width: w, height: h)
                let cocoaRect = NSRect(x: x, y: cocoaY, width: w, height: h)
                bestInfo = (wid: wid, cgBounds: cgRect, cocoaFrame: cocoaRect)
            }
        }
        return bestInfo
    }

    func scheduleVisionOverviewCheck(wid: CGWindowID, cgBounds: CGRect) {
        guard !isVisionRunning, let createImg = cgCreateImage, CGPreflightScreenCaptureAccess() else {
            return
        }
        isVisionRunning = true
        visionQueue.async { [weak self] in
            defer {
                DispatchQueue.main.async {
                    self?.isVisionRunning = false
                }
            }
            guard let self = self else { return }
            let wx = cgBounds.origin.x
            let wy = cgBounds.origin.y
            let ww = cgBounds.size.width
            let wh = cgBounds.size.height
            guard ww > 400, wh > 200 else { return }

            // Capture a 12px-tall strip across the window width at wy + 44 (covers Overview top border at wy+48 and interior at wy+52)
            guard let topImg = createImg(CGRect(x: wx, y: wy + 44, width: ww, height: 12), .optionIncludingWindow, wid, [.boundsIgnoreFraming, .nominalResolution])?.takeRetainedValue(),
                  let topData = topImg.dataProvider?.data,
                  let topPtr = CFDataGetBytePtr(topData) else {
                return
            }

            let bpr = topImg.bytesPerRow
            let bpp = topImg.bitsPerPixel / 8
            let imgW = topImg.width
            let imgH = topImg.height
            guard imgW > 360, imgH >= 10, bpp >= 3 else { return }

            var foundStart = -1
            var foundEnd = -1
            var foundLight = true

            for dy in 2...6 {
                var curStart = -1
                var curLight = true
                for x in 200..<imgW {
                    let p0 = dy * bpr + x * bpp
                    let p4 = (dy + 4) * bpr + x * bpp
                    let r0 = Int(topPtr[p0 + 2]), g0 = Int(topPtr[p0 + 1]), b0 = Int(topPtr[p0])
                    let r4 = Int(topPtr[p4 + 2]), g4 = Int(topPtr[p4 + 1]), b4 = Int(topPtr[p4])
                    let isLight = (r0 >= 225 && r0 <= 253 && abs(r0 - g0) <= 4 && abs(r0 - b0) <= 6) && (r4 >= 250 && g4 >= 250 && b4 >= 250) && (r4 - r0 >= 3)
                    let isDark  = (r0 >= 28 && r0 <= 60 && abs(r0 - g0) <= 4 && abs(r0 - b0) <= 6) && (r4 >= 18 && r4 <= 38 && abs(r4 - g4) <= 4) && (r0 - r4 >= 3)
                    if isLight || isDark {
                        if curStart == -1 {
                            curStart = x
                            curLight = isLight
                        }
                    } else if curStart != -1 {
                        let runW = x - curStart
                        if runW >= 285 && runW <= 316 {
                            foundStart = curStart
                            foundEnd = x - 1
                            foundLight = curLight
                        }
                        curStart = -1
                    }
                }
                if curStart != -1 {
                    let runW = imgW - curStart
                    if runW >= 285 && runW <= 316 {
                        foundStart = curStart
                        foundEnd = imgW - 1
                        foundLight = curLight
                    }
                }
                if foundStart != -1 { break }
            }

            guard foundStart != -1, foundEnd > foundStart else {
                // Overview card is either hidden (+ New Conversation, narrow window, collapsed) OR covered by a modal (e.g. image preview lightbox)!
                DispatchQueue.main.async {
                    self.hasVisionResult = true
                    self.visionOverviewVisible = false
                }
                return
            }

            // Scan the 12px-wide vertical band around the left border (foundStart - 10) starting at wy + 60 to find exactLeftX and bottomY
            let baseX = CGFloat(max(0, foundStart - 10))
            let scanH = min(500.0, max(80.0, wh - 65.0))
            var exactLeftX = wx + CGFloat(foundStart - 8)
            var bottomOffset: CGFloat = 352.0

            if let vertImg = createImg(CGRect(x: wx + baseX, y: wy + 60, width: 12, height: scanH), .optionIncludingWindow, wid, [.boundsIgnoreFraming, .nominalResolution])?.takeRetainedValue(),
               let vertData = vertImg.dataProvider?.data,
               let vertPtr = CFDataGetBytePtr(vertData) {
                let vbpr = vertImg.bytesPerRow
                let vbpp = vertImg.bitsPerPixel / 8
                let vw = vertImg.width
                let vh = vertImg.height
                if vw >= 6 && vh >= 40 && vbpp >= 3 {
                    var bestDx = 2
                    var targetR = Int(vertPtr[2 * vbpp + 2])
                    for dx in 0..<vw {
                        let r = Int(vertPtr[dx * vbpp + 2])
                        if foundLight {
                            if r < targetR { targetR = r; bestDx = dx }
                        } else {
                            if r > targetR { targetR = r; bestDx = dx }
                        }
                    }
                    var lastDy = 0
                    for dy in 0..<vh {
                        let r = Int(vertPtr[dy * vbpr + bestDx * vbpp + 2])
                        if abs(r - targetR) <= 4 {
                            lastDy = dy
                        } else if dy - lastDy > 3 {
                            break
                        }
                    }
                    exactLeftX = wx + baseX + CGFloat(bestDx)
                    if lastDy >= 16 {
                        bottomOffset = CGFloat(60 + lastDy + 7) + 8.0
                    }
                }
            }

            DispatchQueue.main.async {
                self.hasVisionResult = true
                self.visionOverviewVisible = true
                self.visionOverviewLeftX = exactLeftX
                self.visionOverviewBottomOffset = bottomOffset
            }
        }
    }

    func syncWithAntigravityWindow() {
        let now = Date().timeIntervalSince1970
        let frontApp = NSWorkspace.shared.frontmostApplication
        let frontName = (frontApp?.localizedName ?? "").lowercased()
        let isAgyAppFrontmost = frontName.contains("antigravity")
        if isAgyAppFrontmost && !wasAgyAppFrontmost {
            agyBecameFrontmostAt = now
            pendingFocusAnnotation = true
        }
        wasAgyAppFrontmost = isAgyAppFrontmost
        if pendingFocusAnnotation && (now - agyBecameFrontmostAt >= 1.2) {
            pendingFocusAnnotation = false
        }

        let isAgyActive = isAgyAppFrontmost || frontApp?.processIdentifier == ProcessInfo.processInfo.processIdentifier

        guard isAgyActive, let winInfo = findAntigravityWindowInfo() else {
            if panel.isVisible {
                panel.orderOut(nil)
            }
            return
        }

        let agyFrame = winInfo.cocoaFrame
        scheduleVisionOverviewCheck(wid: winInfo.wid, cgBounds: winInfo.cgBounds)

        let layout = readAntigravityLayoutState(now: now)
        // Match Antigravity's exact layout calculation in main.js (yJc, zJc, xIc, BIc):
        let effectiveAuxOpen = layout.isAuxPaneOpen && agyFrame.width >= 640.0
        let effectiveSidebarOpen = agyFrame.width >= 768.0 && !(effectiveAuxOpen && agyFrame.width < 1280.0)
        let leftSidebarW = effectiveSidebarOpen ? layout.sidebarWidth : 0.0
        let workspaceW = max(0.0, agyFrame.width - leftSidebarW)
        let auxPaneW = effectiveAuxOpen ? (workspaceW * layout.auxPaneWidthPct / 100.0) : 0.0
        var chatPaneRightX = agyFrame.maxX - auxPaneW
        let chatPaneWidth = chatPaneRightX - (agyFrame.origin.x + leftSidebarW)

        // Exact Overview breakpoint threshold from xIc in main.js:
        // conversationMaxW = max(0.3 * window.innerWidth, 768.0), medium threshold = conversationMaxW + 32 + 336 (1136pt at <=2560w)
        let convTextW = max(0.3 * agyFrame.width, 768.0)
        let minChatPaneWidthForOverview = convTextW + 368.0

        // If direct window pixel inspection is active, it authoritatively detects whether Overview is visible & uncovered
        // (including hiding when the image preview lightbox modal covers Overview, or when on + New Conversation / narrow window).
        if hasVisionResult {
            if !visionOverviewVisible || agyFrame.height < minWindowHeightForOverview {
                if panel.isVisible {
                    panel.orderOut(nil)
                }
                return
            }
            chatPaneRightX = visionOverviewLeftX + cardSize.width + defaultRightOffset
        } else {
            if !isViewingConversation || !layout.isOverviewOpen || chatPaneWidth < minChatPaneWidthForOverview || agyFrame.height < minWindowHeightForOverview {
                if panel.isVisible {
                    panel.orderOut(nil)
                }
                return
            }
        }

        lastAgyFrame = agyFrame
        lastChatPaneRightX = chatPaneRightX

        if !isDragging {
            let hasCustomOffset = defaults.object(forKey: "agy_hud_v3_rightOffset") != nil && defaults.object(forKey: "agy_hud_v3_topOffset") != nil
            let (rightOff, savedTopOff) = savedTopRightOffsets()
            let topOff = (!hasCustomOffset && hasVisionResult) ? visionOverviewBottomOffset : savedTopOff
            let rawX = (!hasCustomOffset && hasVisionResult) ? visionOverviewLeftX : (chatPaneRightX - cardSize.width - rightOff)
            let targetX = clamp(rawX, min: agyFrame.origin.x + leftSidebarW + 12, max: agyFrame.maxX - cardSize.width - 4)
            let targetY = clamp(agyFrame.maxY - cardSize.height - topOff, min: agyFrame.origin.y + 6, max: agyFrame.maxY - cardSize.height - 40)
            let newFrame = NSRect(x: targetX, y: targetY, width: cardSize.width, height: cardSize.height)
            if abs(panel.frame.origin.x - newFrame.origin.x) > 0.5 ||
               abs(panel.frame.origin.y - newFrame.origin.y) > 0.5 ||
               abs(panel.frame.size.width - newFrame.size.width) > 0.5 ||
               abs(panel.frame.size.height - newFrame.size.height) > 0.5 {
                panel.setFrame(newFrame, display: true)
            }
        }

        if !panel.isVisible {
            panel.orderFrontRegardless()
        }
    }
}

let lockFd = open("/tmp/agy_telemetry_hud_overlay.lock", O_CREAT | O_RDWR, 0o600)
if lockFd < 0 || flock(lockFd, LOCK_EX | LOCK_NB) != 0 {
    exit(0)
}

let app = NSApplication.shared
let delegate = OverlayController()
app.delegate = delegate
app.run()
