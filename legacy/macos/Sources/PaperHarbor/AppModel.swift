import SwiftUI
import Combine
import ServiceManagement
import PaperCore

@MainActor final class AppModel: ObservableObject {
    @Published var library = Library()
    @Published var error: String?
    @Published var busy = false
    @Published var backgroundStatus = "未启用"
    let store: Store?
    let engine: Engine?
    private var task: Task<Void, Never>?
    private var lastAutomaticCheck = Date.distantPast
    init() {
        do {
            let root = ProcessInfo.processInfo.environment["PAPERHARBOR_DATA_DIR"].map { URL(fileURLWithPath: $0, isDirectory: true) }
            let store = try Store(root: root)
            self.store = store; self.engine = Engine(store: store)
            reload()
        } catch { store = nil; engine = nil; self.error = error.localizedDescription }
        updateBackgroundStatus()
    }
    func reload() {
        guard let store else { return }
        do { library = try store.library() } catch { self.error = error.localizedDescription }
    }
    func mutate(_ body: (inout Library) throws -> Void) {
        guard let store else { return }
        do { try store.update(body); reload() } catch { self.error = error.localizedDescription }
    }
    func perform(quiet: Bool = false, _ action: @escaping (Engine) async throws -> Void) {
        guard !busy, let engine else { return }
        busy = true
        task = Task {
            defer { busy = false; reload(); task = nil }
            do { try await action(engine) }
            catch is WorkInProgressError where quiet { }
            catch is CancellationError { try? store?.activity("任务已取消") }
            catch { self.error = error.localizedDescription }
        }
    }
    func checkForUpdatesIfDue() {
        guard !busy, Date().timeIntervalSince(lastAutomaticCheck) >= 60,
              library.topics.contains(where: \.enabled), let store else { return }
        lastAutomaticCheck = Date()
        do {
            let settings = try store.settings()
            guard library.lastRun == nil || Date().timeIntervalSince(library.lastRun!) >= Double(settings.intervalMinutes * 60) else { return }
            perform(quiet: true) { try await $0.run() }
        } catch { self.error = error.localizedDescription }
    }
    func cancel() { task?.cancel() }
    static var service: SMAppService { SMAppService.agent(plistName: "app.paperharbor.agent.plist") }
    func updateBackgroundStatus() {
        switch Self.service.status {
        case .enabled: backgroundStatus = "已启用 · 主应用退出后继续更新"
        case .requiresApproval: backgroundStatus = "等待在系统设置中允许"
        case .notRegistered: backgroundStatus = "未启用"
        case .notFound:
            let bundled = Bundle.main.bundleURL.appendingPathComponent("Contents/Library/LaunchAgents/app.paperharbor.agent.plist")
            backgroundStatus = FileManager.default.fileExists(atPath: bundled.path) ? "未启用" : "后台组件未找到，请使用打包的应用"
        @unknown default: backgroundStatus = "未知状态"
        }
    }
    func setBackground(_ enabled: Bool) {
        do {
            if enabled { try Self.service.register() }
            else { try Self.service.unregister() }
        } catch { self.error = "后台项目操作失败：\(error.localizedDescription)" }
        updateBackgroundStatus()
    }
}
