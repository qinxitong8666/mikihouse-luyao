import AppKit
import Foundation

private let progressPrefix = "MIKIHOUSE_PROGRESS "
private let productionConfirmation = "CONFIRM_MIKIHOUSE_WECHAT_FAVORITE_PRODUCTION_SAVE"
private let appBundleIdentifier = "cn.luyao.mikihouse.quoteassistant"
private let createDailyOperation = "CREATE_DAILY_TWO_FAVORITES"
private let rebuildMissingDailyOperation = "REBUILD_MISSING_DAILY_TWO_FAVORITES"
private let recoverFrozenPDFOperation = "RECOVER_FROZEN_PDF_AND_CREATE_PENDING_TEXT"

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
    private var rebuildButton: NSButton!
    private var recoveryButton: NSButton!
    private var openPDFButton: NSButton!
    private var openFolderButton: NSButton!
    private var relocateButton: NSButton!
    private var running = false
    private var trackedProductionEnabled = false
    private var environmentReady = false
    private var wechatInstalled = false
    private var runtimeErrors: [String] = []
    private var latestPDF: URL?
    private var activeProcess: Process?
    private var lastProcessOutput = ""
    private let outputQueue = DispatchQueue(label: "cn.luyao.mikihouse.quoteassistant.output")
    private var outputBuffer = Data()

    private var repositoryRoot: URL!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        repositoryRoot = locateRepositoryRoot() ?? Bundle.main.bundleURL.deletingLastPathComponent()
        buildWindow()
        performEnvironmentCheck()
        refreshDashboard()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        appendLog("应用已启动；尚未执行任何官网抓取或微信写入。")
        appendLog("仓库位置：\(repositoryRoot.path)")
        appendEnvironmentStatus()

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

    private func repositoryLooksValid(_ url: URL) -> Bool {
        let required = [
            "pyproject.toml",
            "special_skus_2026aw.csv",
            "config/daily_quote.json",
            "scripts/generate_daily_quote.py",
            "scripts/run_mikihouse_daily_production.py",
        ]
        return required.allSatisfy {
            FileManager.default.fileExists(atPath: url.appendingPathComponent($0).path)
        }
    }

    private func ancestors(startingAt url: URL, limit: Int = 8) -> [URL] {
        var values: [URL] = []
        var current = url.standardizedFileURL
        for _ in 0..<limit {
            values.append(current)
            let parent = current.deletingLastPathComponent()
            if parent.path == current.path { break }
            current = parent
        }
        return values
    }

    private func locateRepositoryRoot() -> URL? {
        var candidates: [URL] = []
        if let configured = ProcessInfo.processInfo.environment["MIKIHOUSE_REPOSITORY_ROOT"],
           !configured.isEmpty {
            candidates.append(URL(fileURLWithPath: configured, isDirectory: true))
        }
        if let saved = savedRepositoryURL() { candidates.append(saved) }
        candidates += ancestors(startingAt: Bundle.main.bundleURL.deletingLastPathComponent())
        candidates += ancestors(startingAt: URL(fileURLWithPath: FileManager.default.currentDirectoryPath))
        var seen = Set<String>()
        for candidate in candidates {
            let normalized = candidate.resolvingSymlinksInPath().standardizedFileURL
            guard seen.insert(normalized.path).inserted else { continue }
            if repositoryLooksValid(normalized) { return normalized }
        }
        return nil
    }

    private func repositoryPreferenceURL() -> URL? {
        guard let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        return support
            .appendingPathComponent(appBundleIdentifier, isDirectory: true)
            .appendingPathComponent("repository_path.txt")
    }

    private func savedRepositoryURL() -> URL? {
        guard let preference = repositoryPreferenceURL(),
              let value = try? String(contentsOf: preference, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { return nil }
        return URL(fileURLWithPath: value, isDirectory: true)
    }

    private func persistRepositoryURL(_ url: URL) throws {
        guard let preference = repositoryPreferenceURL() else {
            throw NSError(domain: appBundleIdentifier, code: 1, userInfo: [NSLocalizedDescriptionKey: "无法读取用户配置目录。"])
        }
        try FileManager.default.createDirectory(
            at: preference.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try (url.path + "\n").write(to: preference, atomically: true, encoding: .utf8)
    }

    private func runJSONCommand(executable: URL, arguments: [String], environment: [String: String]) -> (Int32, [String: Any]?, String) {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = repositoryRoot
        process.environment = environment
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return (127, nil, error.localizedDescription)
        }
        let outputData = output.fileHandleForReading.readDataToEndOfFile()
        let errorData = errors.fileHandleForReading.readDataToEndOfFile()
        let outputText = String(data: outputData, encoding: .utf8) ?? ""
        let errorText = String(data: errorData, encoding: .utf8) ?? ""
        let object = try? JSONSerialization.jsonObject(with: outputData) as? [String: Any]
        return (process.terminationStatus, object, errorText.isEmpty ? outputText : errorText)
    }

    private func pythonEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        // A Python framework executable launched by a native .app can inherit
        // the parent's LaunchServices/XPC identity.  On some macOS versions
        // that makes Python initialise as another GUI app and hang before it
        // can import even the standard-library codecs module.  These values
        // describe the parent process, not the quote task, and must not cross
        // the subprocess boundary.
        for key in ["__CFBundleIdentifier", "XPC_SERVICE_NAME", "XPC_FLAGS", "__PYVENV_LAUNCHER__"] {
            environment.removeValue(forKey: key)
        }
        environment["PWD"] = repositoryRoot.path
        environment["PYTHONPATH"] = repositoryRoot.appendingPathComponent("src").path
        environment["PYTHONPYCACHEPREFIX"] = FileManager.default.temporaryDirectory
            .appendingPathComponent("mikihouse-quote-assistant-pyc", isDirectory: true).path
        return environment
    }

    private func performEnvironmentCheck() {
        runtimeErrors = []
        environmentReady = false
        wechatInstalled = false
        guard repositoryLooksValid(repositoryRoot) else {
            runtimeErrors.append("无法定位完整的 mikihouse-luyao 仓库。请把 App 放回仓库根目录。")
            return
        }
        let python = repositoryRoot.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            runtimeErrors.append("找不到项目运行环境 .venv/bin/python，请按 README 完成一次安装。")
            return
        }
        let environment = pythonEnvironment()
        let script = repositoryRoot.appendingPathComponent("scripts/check_quote_assistant_runtime.py")
        let result = runJSONCommand(
            executable: python,
            arguments: [script.path, "--repository-root", repositoryRoot.path],
            environment: environment
        )
        if result.0 != 0 || result.1?["status"] as? String != "READY" {
            if let errors = result.1?["errors"] as? [String], !errors.isEmpty {
                runtimeErrors.append(contentsOf: errors)
            } else {
                runtimeErrors.append("运行环境检查失败：\(result.2.trimmingCharacters(in: .whitespacesAndNewlines))")
            }
            return
        }
        let configURL = repositoryRoot.appendingPathComponent("config/wechat_favorite_runtime.json")
        if let data = try? Data(contentsOf: configURL),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let targetPath = config["target_app_path"] as? String,
           let expectedBundle = config["target_bundle_id"] as? String,
           let targetBundle = Bundle(url: URL(fileURLWithPath: targetPath)),
           targetBundle.bundleIdentifier == expectedBundle {
            wechatInstalled = true
        } else {
            runtimeErrors.append("未找到已验收的“微信2”应用，双收藏保存暂不可用。")
        }
        environmentReady = true
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
        rebuildButton = button("安全重建已删除收藏", action: #selector(rebuildMissingFavorites), emphasized: false)
        openPDFButton = button("打开 PDF", action: #selector(openPDF), emphasized: false)
        openFolderButton = button("打开输出目录", action: #selector(openOutputFolder), emphasized: false)
        actions.addArrangedSubview(generateButton)
        actions.addArrangedSubview(productionButton)
        actions.addArrangedSubview(rebuildButton)
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
        recoveryButton = button("恢复冻结PDF附件", action: #selector(recoverFrozenPDFAttachment), emphasized: false)
        previewActions.addArrangedSubview(recoveryButton)
        previewActions.addArrangedSubview(button("刷新状态", action: #selector(refreshAction), emphasized: false))
        relocateButton = button("选择仓库…", action: #selector(chooseRepository), emphasized: false)
        previewActions.addArrangedSubview(relocateButton)
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

    @objc private func refreshAction() {
        performEnvironmentCheck()
        refreshDashboard()
        appendEnvironmentStatus()
    }

    @objc private func chooseRepository() {
        guard !running else { return }
        let panel = NSOpenPanel()
        panel.title = "选择 mikihouse-luyao 仓库文件夹"
        panel.prompt = "使用此仓库"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = repositoryRoot
        guard panel.runModal() == .OK, let selected = panel.url else { return }
        let normalized = selected.resolvingSymlinksInPath().standardizedFileURL
        guard repositoryLooksValid(normalized) else {
            showAlert(
                title: "选择的文件夹不是完整仓库",
                message: "请选择包含 pyproject.toml、special_skus_2026aw.csv、config 和 scripts 的 mikihouse-luyao 根目录。"
            )
            return
        }
        do {
            try persistRepositoryURL(normalized)
            repositoryRoot = normalized
            performEnvironmentCheck()
            refreshDashboard()
            appendLog("仓库位置已更新：\(repositoryRoot.path)")
            appendEnvironmentStatus()
        } catch {
            showAlert(title: "无法保存仓库位置", message: error.localizedDescription)
        }
    }

    @objc private func generateToday() {
        guard environmentReady else {
            showEnvironmentError()
            return
        }
        startCore(production: false, rebuildMissing: false, recoverFrozenPDF: false, confirmation: nil, authorizationFile: nil)
    }

    @objc private func generateAndSave() {
        performEnvironmentCheck()
        refreshDashboard()
        guard environmentReady && wechatInstalled else {
            showEnvironmentError()
            return
        }
        let alert = NSAlert()
        alert.messageText = "单次授权：生成并保存两个微信收藏"
        alert.informativeText = "本次将重新完整抓取官网、生成今日报价，然后依次创建 PDF版和文字版两条微信收藏。不会发送聊天；任何写入结果不确定都会立即停止且不会自动重试。\n\n授权仅对本次、当前仓库版本有效，15分钟后自动过期。"
        alert.addButton(withTitle: "单次授权并执行")
        alert.addButton(withTitle: "取消")
        let confirmationCheck = NSButton(checkboxWithTitle: "我确认本次创建恰好两条微信收藏", target: nil, action: nil)
        confirmationCheck.frame = NSRect(x: 0, y: 0, width: 460, height: 28)
        alert.accessoryView = confirmationCheck
        guard alert.runModal() == .alertFirstButtonReturn,
              confirmationCheck.state == .on else {
            statusLabel.stringValue = "未完成单次授权；零微信写入"
            appendLog("用户未确认本次双收藏创建；流程未启动。")
            return
        }
        statusLabel.stringValue = "正在签发一次性本地授权……"
        guard let authorizationFile = issueOneTimeAuthorization(operation: createDailyOperation) else { return }
        startCore(
            production: true,
            rebuildMissing: false,
            recoverFrozenPDF: false,
            confirmation: productionConfirmation,
            authorizationFile: authorizationFile
        )
    }

    @objc private func rebuildMissingFavorites() {
        performEnvironmentCheck()
        refreshDashboard()
        guard environmentReady && wechatInstalled else {
            showEnvironmentError()
            return
        }
        statusLabel.stringValue = "正在只读检查当天两条收藏标题……"
        appendLog("开始只读检查 PDF版和文字版标题；本步不修改 checkpoint，不创建收藏。")
        let python = repositoryRoot.appendingPathComponent(".venv/bin/python")
        let script = repositoryRoot.appendingPathComponent("scripts/audit_wechat_daily_favorite_titles.py")
        let environment = pythonEnvironment()
        let audit = runJSONCommand(
            executable: python,
            arguments: [script.path],
            environment: environment
        )
        guard audit.0 == 0,
              audit.1?["status"] as? String == "READY_FOR_EXPLICIT_REBUILD_AUTHORIZATION",
              audit.1?["all_titles_absent"] as? Bool == true else {
            let status = audit.1?["status"] as? String ?? "FAILED_CLOSED"
            let error = audit.1?["error"] as? String ?? "PDF版或文字版收藏仍然存在，或只读检查无法得出唯一结论。"
            statusLabel.stringValue = "安全重建已阻止；零微信写入"
            appendLog("只读标题检查结果：\(status)；checkpoint 未重置。")
            appendLog("阻止原因：\(error)")
            showAlert(title: "不能安全重建", message: error + "\n\n只有两个精确标题都不存在时才允许重建。")
            return
        }
        appendLog("只读检查通过：当天 PDF版和文字版精确标题均为 0 个候选。")
        let alert = NSAlert()
        alert.messageText = "单次授权：使用当前报价安全重建当天收藏"
        alert.informativeText = "只读检查已确认当天 PDF版和文字版标题均不存在。本次不会再次生成或覆盖报价文件；将使用当前最新 bundle，完整归档旧 PASS/冻结 checkpoint 及对应证据，建立新 checkpoint，然后依次创建恰好两条收藏。\n\n写入前会再次检查两个标题、当前 bundle 完整性及归档；任一收藏存在即停止，不重置。"
        alert.addButton(withTitle: "单次授权并安全重建")
        alert.addButton(withTitle: "取消")
        let confirmationCheck = NSButton(checkboxWithTitle: "我确认当天两条收藏均已人工删除，本次重建恰好两条", target: nil, action: nil)
        confirmationCheck.frame = NSRect(x: 0, y: 0, width: 560, height: 28)
        alert.accessoryView = confirmationCheck
        guard alert.runModal() == .alertFirstButtonReturn,
              confirmationCheck.state == .on else {
            statusLabel.stringValue = "未完成重建授权；checkpoint 未修改"
            appendLog("用户未确认安全重建；流程未启动。")
            return
        }
        guard let authorizationFile = issueOneTimeAuthorization(operation: rebuildMissingDailyOperation) else { return }
        startCore(
            production: true,
            rebuildMissing: true,
            recoverFrozenPDF: false,
            confirmation: productionConfirmation,
            authorizationFile: authorizationFile
        )
    }

    @objc private func recoverFrozenPDFAttachment() {
        performEnvironmentCheck()
        refreshDashboard()
        guard environmentReady && wechatInstalled else {
            showEnvironmentError()
            return
        }
        statusLabel.stringValue = "正在只读核对冻结草稿与收藏标题……"
        appendLog("开始只读核对：PDF版必须恰好1个、文字版必须为0个，checkpoint必须是首次附件不可见冻结状态。")
        let python = repositoryRoot.appendingPathComponent(".venv/bin/python")
        let script = repositoryRoot.appendingPathComponent("scripts/audit_wechat_frozen_pdf_recovery.py")
        let audit = runJSONCommand(
            executable: python,
            arguments: [script.path],
            environment: pythonEnvironment()
        )
        guard audit.0 == 0,
              audit.1?["status"] as? String == "READY_FOR_EXPLICIT_PDF_RECOVERY_AUTHORIZATION" else {
            let error = audit.1?["error"] as? String ?? "未能证明当前只有一条本任务PDF收藏且文字收藏不存在。"
            statusLabel.stringValue = "PDF附件恢复已阻止；零新增写入"
            appendLog("恢复前只读核对失败：\(error)")
            showAlert(title: "不能安全恢复", message: error)
            return
        }
        appendLog("只读核对通过：将仅修复现有PDF收藏的附件，不创建第二条PDF收藏。")
        let alert = NSAlert()
        alert.messageText = "单次授权：恢复冻结PDF附件并完成文字收藏"
        alert.informativeText = "当前已证明PDF标题恰好1个、文字标题0个。本次只会在该任务已有PDF收藏中，通过工具栏「文件」按钮选择同一PDF一次；强回读通过后再创建待处理的文字收藏。任何不确定都立即冻结，不自动重试。"
        alert.addButton(withTitle: "单次授权并恢复")
        alert.addButton(withTitle: "取消")
        let check = NSButton(checkboxWithTitle: "我确认修复现有1条PDF收藏并创建待处理的1条文字收藏", target: nil, action: nil)
        check.frame = NSRect(x: 0, y: 0, width: 620, height: 28)
        alert.accessoryView = check
        guard alert.runModal() == .alertFirstButtonReturn, check.state == .on else {
            statusLabel.stringValue = "未完成PDF恢复授权；未修改微信"
            appendLog("用户未确认PDF附件恢复；流程未启动。")
            return
        }
        guard let authorizationFile = issueOneTimeAuthorization(operation: recoverFrozenPDFOperation) else { return }
        startCore(
            production: true,
            rebuildMissing: false,
            recoverFrozenPDF: true,
            confirmation: productionConfirmation,
            authorizationFile: authorizationFile
        )
    }

    private func issueOneTimeAuthorization(operation: String) -> String? {
        let python = repositoryRoot.appendingPathComponent(".venv/bin/python")
        let script = repositoryRoot.appendingPathComponent("scripts/create_quote_assistant_one_time_authorization.py")
        var environment = pythonEnvironment()
        environment["MIKIHOUSE_QUOTE_ASSISTANT_APP"] = appBundleIdentifier
        let result = runJSONCommand(
            executable: python,
            arguments: [
                script.path,
                "--repository-root", repositoryRoot.path,
                "--confirm", productionConfirmation,
                "--operation", operation,
            ],
            environment: environment
        )
        guard result.0 == 0,
              result.1?["status"] as? String == "ISSUED",
              let path = result.1?["authorization_file"] as? String else {
            let error = result.1?["error"] as? String ?? result.2
            statusLabel.stringValue = "单次授权失败；零微信写入"
            appendLog("单次授权失败：\(error)")
            showAlert(title: "无法开始本次保存", message: error.isEmpty ? "一次性授权创建失败。" : error)
            return nil
        }
        appendLog("一次性授权已签发并绑定当前仓库版本；正式入口将立即消费，不能复用。")
        return path
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
        trackedProductionEnabled = false
        if let data = try? Data(contentsOf: configURL),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            trackedProductionEnabled = config["production_save_enabled"] as? Bool == true
        }
        if !environmentReady {
            gateLabel.stringValue = "运行环境未就绪；生成和保存均已安全禁用"
        } else if !wechatInstalled {
            gateLabel.stringValue = "报价生成可用；未找到已验收的微信2，双收藏保存已禁用"
        } else if trackedProductionEnabled {
            gateLabel.stringValue = "仓库生产开关已启用；执行前仍要求 App 单次明确确认"
        } else {
            gateLabel.stringValue = "仓库生产开关保持关闭；App 单次授权可用且不会修改配置"
        }
        updateButtonStates()
    }

    private func updateButtonStates() {
        generateButton.isEnabled = environmentReady && !running
        productionButton.isEnabled = environmentReady && wechatInstalled && !running
        rebuildButton.isEnabled = environmentReady && wechatInstalled && !running
        recoveryButton.isEnabled = environmentReady && wechatInstalled && !running
        relocateButton.isEnabled = !running
        openPDFButton.isEnabled = latestPDF != nil && !running
        openFolderButton.isEnabled = FileManager.default.fileExists(
            atPath: repositoryRoot.appendingPathComponent("outputs/daily_quote/最新").path
        ) && !running
    }

    private func appendEnvironmentStatus() {
        if environmentReady {
            appendLog(wechatInstalled ? "运行环境检查通过；今日报价和单次授权双收藏均可用。" : "报价环境检查通过，但未找到微信2。")
        } else {
            runtimeErrors.forEach { appendLog("环境检查：\($0)") }
        }
    }

    private func showEnvironmentError() {
        let message = runtimeErrors.isEmpty ? "运行环境尚未就绪。" : runtimeErrors.joined(separator: "\n")
        showAlert(title: "运行环境检查未通过", message: message)
    }

    private func startCore(production: Bool, rebuildMissing: Bool, recoverFrozenPDF: Bool, confirmation: String?, authorizationFile: String?) {
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
            if rebuildMissing {
                arguments.append("--rebuild-missing-daily-favorites")
            }
            if recoverFrozenPDF {
                arguments.append("--recover-frozen-pdf")
            }
            if let authorizationFile {
                arguments += ["--app-authorization-file", authorizationFile]
            }
        }
        arguments.append("--progress-jsonl")

        let process = Process()
        process.executableURL = python
        process.arguments = arguments
        process.currentDirectoryURL = repositoryRoot
        let environment = pythonEnvironment()
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
        lastProcessOutput = ""
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
            lastProcessOutput += line + "\n"
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
            showAlert(title: "流程已安全停止", message: chineseFailureMessage())
        }
    }

    private func chineseFailureMessage() -> String {
        if let data = lastProcessOutput.data(using: .utf8),
           let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let error = payload["error"] as? String,
           !error.isEmpty {
            return error + "\n\n未自动重试任何微信写入；请根据提示处理后重新从 App 发起。"
        }
        let lower = lastProcessOutput.lowercased()
        if lower.contains("network") || lower.contains("timeout") || lower.contains("connection") {
            return "官网、汇率或图片网络请求失败。上一次成功结果未覆盖，也没有创建微信收藏。请检查网络后重新运行。"
        }
        if lower.contains("permission") || lower.contains("accessibility") {
            return "微信自动化权限不足。请在“系统设置→隐私与安全性→辅助功能”中允许报价助手，然后重新运行。"
        }
        return "流程未完成。上一次成功结果未覆盖，微信副作用操作不会自动重试。请查看窗口中的中文运行记录和 checkpoint。"
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
                "safe_rebuild_missing_favorites": rebuildButton.title,
                "recover_frozen_pdf_attachment": recoveryButton.title,
                "open_pdf": openPDFButton.title,
                "open_output_directory": openFolderButton.title,
                "choose_repository": relocateButton.title,
            ],
            "tracked_production_save_enabled": trackedProductionEnabled,
            "app_one_time_authorization_available": environmentReady && wechatInstalled,
            "production_button_enabled": productionButton.isEnabled,
            "environment_ready": environmentReady,
            "wechat2_installed": wechatInstalled,
            "repository_root": repositoryRoot.path,
            "runtime_errors": runtimeErrors,
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
