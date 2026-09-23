import AppKit
import Foundation

private let progressPrefix = "MIKIHOUSE_PROGRESS "
private let productionConfirmation = "CONFIRM_MIKIHOUSE_WECHAT_FAVORITE_PRODUCTION_SAVE"

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var dateValue = NSTextField(labelWithString: "—")
    private var fxValue = NSTextField(labelWithString: "—")
    private var productsValue = NSTextField(labelWithString: "—")
    private var pdfValue = NSTextField(labelWithString: "—")
    private var gateLabel = NSTextField(labelWithString: "")
    private var statusLabel = NSTextField(labelWithString: "就绪")
    private var progress = NSProgressIndicator()
    private var logView = NSTextView()
    private var generateButton: NSButton!
    private var productionButton: NSButton!
    private var openPDFButton: NSButton!
    private var openFolderButton: NSButton!
    private var running = false
    private var productionEnabled = false
    private var latestPDF: URL?
    private var activeProcess: Process?
    private let outputQueue = DispatchQueue(label: "cn.luyao.mikihouse.quoteassistant.output")
    private var outputBuffer = Data()

    private lazy var repositoryRoot: URL = {
        Bundle.main.bundleURL.deletingLastPathComponent()
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildWindow()
        refreshDashboard()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        appendLog("应用已启动；尚未执行任何官网抓取或微信写入。")

        if let index = CommandLine.arguments.firstIndex(of: "--runtime-smoke-evidence"),
           CommandLine.arguments.indices.contains(index + 1) {
            let path = CommandLine.arguments[index + 1]
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
                self?.writeSmokeEvidence(path: path)
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1080, height: 720),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "MIKI HOUSE 报价助手"
        window.center()
        window.minSize = NSSize(width: 940, height: 620)

        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 14
        root.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = NSView()
        window.contentView?.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 26),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -26),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 22),
            root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -20),
        ])

        let title = NSTextField(labelWithString: "MIKI HOUSE 报价助手")
        title.font = .systemFont(ofSize: 28, weight: .bold)
        root.addArrangedSubview(title)
        let subtitle = NSTextField(labelWithString: "完整抓取官网、冻结当日汇率，生成客户 PDF 与两个微信收藏。")
        subtitle.font = .systemFont(ofSize: 13)
        subtitle.textColor = .secondaryLabelColor
        root.addArrangedSubview(subtitle)

        let metricRow = NSStackView()
        metricRow.orientation = .horizontal
        metricRow.distribution = .fillEqually
        metricRow.spacing = 10
        metricRow.translatesAutoresizingMaskIntoConstraints = false
        metricRow.heightAnchor.constraint(equalToConstant: 92).isActive = true
        metricRow.addArrangedSubview(metricView(caption: "报价日期", value: dateValue))
        metricRow.addArrangedSubview(metricView(caption: "JPY → CNY", value: fxValue))
        metricRow.addArrangedSubview(metricView(caption: "当日有货商品", value: productsValue))
        metricRow.addArrangedSubview(metricView(caption: "全集 PDF", value: pdfValue))
        root.addArrangedSubview(metricRow)
        metricRow.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        let actions = NSStackView()
        actions.orientation = .horizontal
        actions.spacing = 10
        generateButton = button("生成今日报价", action: #selector(generateToday), emphasized: true)
        productionButton = button("生成并保存两个微信收藏", action: #selector(generateAndSave), emphasized: true)
        openPDFButton = button("打开 PDF", action: #selector(openPDF), emphasized: false)
        openFolderButton = button("打开输出目录", action: #selector(openOutputFolder), emphasized: false)
        actions.addArrangedSubview(generateButton)
        actions.addArrangedSubview(productionButton)
        actions.addArrangedSubview(NSView())
        actions.addArrangedSubview(openFolderButton)
        actions.addArrangedSubview(openPDFButton)
        root.addArrangedSubview(actions)
        actions.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        gateLabel.font = .systemFont(ofSize: 12)
        gateLabel.textColor = .secondaryLabelColor
        root.addArrangedSubview(gateLabel)

        statusLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        root.addArrangedSubview(statusLabel)
        progress.isIndeterminate = false
        progress.minValue = 0
        progress.maxValue = 100
        progress.doubleValue = 0
        root.addArrangedSubview(progress)
        progress.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        let previewActions = NSStackView()
        previewActions.orientation = .horizontal
        previewActions.spacing = 8
        previewActions.addArrangedSubview(button("预览 PDF 收藏", action: #selector(previewPDF), emphasized: false))
        previewActions.addArrangedSubview(button("预览文字收藏", action: #selector(previewText), emphasized: false))
        previewActions.addArrangedSubview(button("刷新状态", action: #selector(refreshAction), emphasized: false))
        root.addArrangedSubview(previewActions)

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        logView.isEditable = false
        logView.isSelectable = true
        logView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        logView.textContainerInset = NSSize(width: 8, height: 8)
        scroll.documentView = logView
        root.addArrangedSubview(scroll)
        scroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
    }

    private func metricView(caption: String, value: NSTextField) -> NSView {
        let box = NSBox()
        box.boxType = .custom
        box.borderColor = .separatorColor
        box.borderWidth = 1
        box.cornerRadius = 9
        box.fillColor = .controlBackgroundColor
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 7
        stack.translatesAutoresizingMaskIntoConstraints = false
        let label = NSTextField(labelWithString: caption)
        label.textColor = .secondaryLabelColor
        label.font = .systemFont(ofSize: 11)
        value.font = .systemFont(ofSize: 19, weight: .bold)
        value.lineBreakMode = .byTruncatingTail
        stack.addArrangedSubview(label)
        stack.addArrangedSubview(value)
        box.contentView?.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: box.contentView!.leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: box.contentView!.trailingAnchor, constant: -14),
            stack.centerYAnchor.constraint(equalTo: box.contentView!.centerYAnchor),
        ])
        return box
    }

    private func button(_ title: String, action: Selector, emphasized: Bool) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        button.controlSize = .large
        if emphasized { button.keyEquivalent = "" }
        return button
    }

    @objc private func refreshAction() { refreshDashboard() }
    @objc private func generateToday() { startCore(production: false, confirmation: nil) }

    @objc private func generateAndSave() {
        refreshDashboard()
        guard productionEnabled else {
            showAlert(title: "正式保存已关闭", message: "安全门禁处于关闭状态；应用不会创建微信收藏。需要新的明确授权任务启用。")
            return
        }
        let alert = NSAlert()
        alert.messageText = "确认创建恰好两条微信收藏"
        alert.informativeText = "将重新生成报价，并按 PDF版 → 文字版 保存。不会发送聊天，也不会自动重试 mutation。\n\n请输入完整确认语句："
        alert.addButton(withTitle: "确认")
        alert.addButton(withTitle: "取消")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 500, height: 24))
        alert.accessoryView = input
        guard alert.runModal() == .alertFirstButtonReturn,
              input.stringValue == productionConfirmation else {
            statusLabel.stringValue = "确认语句不匹配；零微信写入"
            appendLog("正式保存确认失败；流程未启动。")
            return
        }
        startCore(production: true, confirmation: input.stringValue)
    }

    @objc private func openPDF() {
        guard let latestPDF, FileManager.default.fileExists(atPath: latestPDF.path) else {
            showAlert(title: "PDF 不存在", message: "请先成功生成今日报价。")
            return
        }
        NSWorkspace.shared.open(latestPDF)
    }

    @objc private func openOutputFolder() {
        let latest = repositoryRoot.appendingPathComponent("outputs/daily_quote/最新", isDirectory: true)
        guard FileManager.default.fileExists(atPath: latest.path) else {
            showAlert(title: "输出目录不存在", message: "请先成功生成今日报价。")
            return
        }
        NSWorkspace.shared.open(latest)
    }

    @objc private func previewPDF() { openPreview(name: "wechat_pdf_favorite_preview.txt") }
    @objc private func previewText() { openPreview(name: "wechat_text_favorite_preview.txt") }

    private func openPreview(name: String) {
        let file = repositoryRoot.appendingPathComponent("outputs/daily_quote/最新/\(name)")
        guard FileManager.default.fileExists(atPath: file.path) else {
            showAlert(title: "尚无预览", message: "请先生成今日报价。")
            return
        }
        NSWorkspace.shared.open(file)
    }

    private func refreshDashboard() {
        let latest = repositoryRoot.appendingPathComponent("outputs/daily_quote/最新", isDirectory: true)
        let statsURL = latest.appendingPathComponent("daily_quote_stats.json")
        latestPDF = nil
        if let data = try? Data(contentsOf: statsURL),
           let stats = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            dateValue.stringValue = stats["quote_date"] as? String ?? "—"
            let fx = stats["fx"] as? [String: Any] ?? [:]
            let rate = fx["jpy_to_cny_rate"] as? String ?? "—"
            fxValue.stringValue = rate == "—" ? rate : String(rate.prefix(10))
            productsValue.stringValue = String(stats["included_in_stock_product_count"] as? Int ?? 0)
            let pdf = stats["pdf"] as? [String: Any] ?? [:]
            if let size = pdf["size_mb"] as? Double {
                pdfValue.stringValue = String(format: "%.2f MB", size)
            } else if let bytes = pdf["file_size_bytes"] as? Int {
                pdfValue.stringValue = String(format: "%.2f MB", Double(bytes) / 1024.0 / 1024.0)
            } else {
                pdfValue.stringValue = "—"
            }
            if let name = pdf["path"] as? String {
                latestPDF = latest.appendingPathComponent(name)
            }
        } else {
            dateValue.stringValue = "尚未生成"
            fxValue.stringValue = "—"
            productsValue.stringValue = "—"
            pdfValue.stringValue = "—"
        }

        let configURL = repositoryRoot.appendingPathComponent("config/wechat_favorite_runtime.json")
        productionEnabled = false
        if let data = try? Data(contentsOf: configURL),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            productionEnabled = config["production_save_enabled"] as? Bool == true
        }
        gateLabel.stringValue = productionEnabled
            ? "微信正式保存门禁：已启用；执行前仍须输入精确确认语句"
            : "微信正式保存门禁：关闭（安全默认；不会创建收藏）"
        updateButtonStates()
    }

    private func updateButtonStates() {
        generateButton.isEnabled = !running
        productionButton.isEnabled = productionEnabled && !running
        openPDFButton.isEnabled = latestPDF != nil && !running
        openFolderButton.isEnabled = FileManager.default.fileExists(
            atPath: repositoryRoot.appendingPathComponent("outputs/daily_quote/最新").path
        ) && !running
    }

    private func startCore(production: Bool, confirmation: String?) {
        guard !running else { return }
        let python = repositoryRoot.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            showAlert(title: "无法启动", message: "找不到仓库 .venv/bin/python，请先按 README 完成安装。")
            return
        }
        let script = repositoryRoot.appendingPathComponent(
            production ? "scripts/run_mikihouse_daily_production.py" : "scripts/generate_daily_quote.py"
        )
        guard FileManager.default.fileExists(atPath: script.path) else {
            showAlert(title: "无法启动", message: "找不到正式业务入口。")
            return
        }

        var arguments = [script.path]
        if production {
            arguments += ["--production-save", "--confirm", confirmation ?? ""]
        }
        arguments.append("--progress-jsonl")

        let process = Process()
        process.executableURL = python
        process.arguments = arguments
        process.currentDirectoryURL = repositoryRoot
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = repositoryRoot.appendingPathComponent("src").path
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        running = true
        activeProcess = process
        progress.doubleValue = 0
        statusLabel.stringValue = "正在启动安全流程……"
        updateButtonStates()
        appendLog(production ? "开始正式双收藏流程。" : "开始生成今日报价。")
        outputQueue.sync { outputBuffer.removeAll(keepingCapacity: true) }

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            self?.consumeOutput(data)
        }
        process.terminationHandler = { [weak self] finished in
            pipe.fileHandleForReading.readabilityHandler = nil
            self?.flushOutput()
            DispatchQueue.main.async {
                self?.finishCore(exitCode: finished.terminationStatus, production: production)
            }
        }
        do {
            try process.run()
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            running = false
            activeProcess = nil
            refreshDashboard()
            statusLabel.stringValue = "启动失败并已安全停止"
            appendLog("无法启动核心流程：\(error.localizedDescription)")
            showAlert(title: "启动失败", message: error.localizedDescription)
        }
    }

    private func consumeOutput(_ data: Data) {
        outputQueue.async { [weak self] in
            guard let self else { return }
            self.outputBuffer.append(data)
            while let newline = self.outputBuffer.firstIndex(of: 0x0A) {
                let lineData = self.outputBuffer.prefix(upTo: newline)
                self.outputBuffer.removeSubrange(...newline)
                if let line = String(data: lineData, encoding: .utf8) {
                    DispatchQueue.main.async { self.handleOutputLine(line) }
                }
            }
        }
    }

    private func flushOutput() {
        outputQueue.sync {
            guard !outputBuffer.isEmpty else { return }
            if let line = String(data: outputBuffer, encoding: .utf8) {
                DispatchQueue.main.async { [weak self] in self?.handleOutputLine(line) }
            }
            outputBuffer.removeAll()
        }
    }

    private func handleOutputLine(_ line: String) {
        if line.hasPrefix(progressPrefix),
           let data = String(line.dropFirst(progressPrefix.count)).data(using: .utf8),
           let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           event["event"] as? String == "progress" {
            if let percent = event["percent"] as? Int { progress.doubleValue = Double(percent) }
            let message = event["message"] as? String ?? "正在运行"
            statusLabel.stringValue = message
            let details = event["details"] as? [String: Any] ?? [:]
            let detail = details.keys.sorted().map { "\($0)=\(details[$0]!)" }.joined(separator: "；")
            appendLog(message + (detail.isEmpty ? "" : "（\(detail)）"))
        } else if !line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            appendLog(line)
        }
    }

    private func finishCore(exitCode: Int32, production: Bool) {
        running = false
        activeProcess = nil
        refreshDashboard()
        if exitCode == 0 {
            progress.doubleValue = 100
            statusLabel.stringValue = production
                ? "成功：两条微信收藏已保存并强回读"
                : "成功：今日报价已生成；未创建微信收藏"
            appendLog("流程成功完成。")
        } else {
            statusLabel.stringValue = "失败并已安全停止；请查看运行记录"
            appendLog("流程退出码：\(exitCode)；未自动重试副作用操作。")
            showAlert(title: "流程已安全停止", message: "请查看运行记录和 checkpoint；不要盲目重跑。")
        }
    }

    private func appendLog(_ text: String) {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = TimeZone(identifier: "Asia/Tokyo")
        formatter.dateFormat = "HH:mm:ss"
        let line = "[\(formatter.string(from: Date()))] \(text.trimmingCharacters(in: .newlines))\n"
        logView.textStorage?.append(NSAttributedString(string: line))
        logView.scrollToEndOfDocument(nil)
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }

    private func writeSmokeEvidence(path: String) {
        let payload: [String: Any] = [
            "schema_version": 1,
            "status": "PASS",
            "application": "MIKI HOUSE 报价助手",
            "bundle_identifier": Bundle.main.bundleIdentifier ?? "",
            "buttons": [
                "generate_today": generateButton.title,
                "generate_and_save_two_favorites": productionButton.title,
                "open_pdf": openPDFButton.title,
                "open_output_directory": openFolderButton.title,
            ],
            "production_save_enabled": productionEnabled,
            "production_button_enabled": productionButton.isEnabled,
            "latest_summary": [
                "quote_date": dateValue.stringValue,
                "fx_rate_display": fxValue.stringValue,
                "product_count": productsValue.stringValue,
                "pdf_size": pdfValue.stringValue,
            ],
            "website_request_count": 0,
            "wechat_mutation_count": 0,
            "shijiu_request_count": 0,
        ]
        do {
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            let url = URL(fileURLWithPath: path)
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: url, options: .atomic)
            NSApp.terminate(nil)
        } catch {
            appendLog("runtime smoke evidence 写入失败：\(error.localizedDescription)")
            NSApp.terminate(nil)
        }
    }
}

let application = NSApplication.shared
private let delegate = AppDelegate()
application.delegate = delegate
application.run()
