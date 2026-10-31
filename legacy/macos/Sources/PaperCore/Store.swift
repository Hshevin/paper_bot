import Foundation
import Darwin

public struct WorkInProgressError: LocalizedError {
    public var errorDescription: String? { "已有抓取或分析任务正在运行，请稍后重试。" }
    public init() {}
}

public final class FileLock: @unchecked Sendable {
    private var descriptor: Int32
    public init(url: URL, wait: Bool = true) throws {
        descriptor = open(url.path, O_CREAT | O_RDWR | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { throw AppError("无法打开本地数据锁。") }
        guard flock(descriptor, LOCK_EX | (wait ? 0 : LOCK_NB)) == 0 else {
            close(descriptor); descriptor = -1
            throw WorkInProgressError()
        }
    }
    deinit { if descriptor >= 0 { flock(descriptor, LOCK_UN); close(descriptor) } }
}

public final class Store: @unchecked Sendable {
    public let root: URL
    public var pdfDirectory: URL { root.appendingPathComponent("PDFs", isDirectory: true) }
    public init(root: URL? = nil) throws {
        self.root = root ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("PaperHarbor", isDirectory: true)
        try FileManager.default.createDirectory(at: self.root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: self.root.path)
        try FileManager.default.createDirectory(at: pdfDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    }
    private func locked<T>(_ body: () throws -> T) throws -> T {
        let lock = try FileLock(url: root.appendingPathComponent("data.lock"))
        defer { withExtendedLifetime(lock) {} }
        return try body()
    }
    private func load<T: Decodable>(_ file: String, fallback: T) throws -> T {
        let url = root.appendingPathComponent(file)
        guard FileManager.default.fileExists(atPath: url.path) else { return fallback }
        do { return try JSONDecoder().decode(T.self, from: Data(contentsOf: url)) }
        catch { throw AppError("无法读取 \(file)，文件已保留。请检查或恢复备份后重试。") }
    }
    private func save<T: Encodable>(_ value: T, file: String) throws {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(value)
        let target = root.appendingPathComponent(file)
        let temporary = root.appendingPathComponent(".\(UUID().uuidString).tmp")
        guard FileManager.default.createFile(atPath: temporary.path, contents: data, attributes: [.posixPermissions: 0o600]) else { throw AppError("无法写入本地数据。") }
        defer { try? FileManager.default.removeItem(at: temporary) }
        guard rename(temporary.path, target.path) == 0 else { throw AppError("无法保存本地数据。") }
    }
    public func library() throws -> Library { try locked { try load("library.json", fallback: Library()) } }
    public func settings() throws -> Settings { try locked { try load("settings.json", fallback: Settings()) } }
    public func saveSettings(_ settings: Settings) throws {
        guard (15...1440).contains(settings.intervalMinutes), (1...200).contains(settings.screeningLimit) else { throw AppError("更新间隔应为 15–1440 分钟，每轮摘要上限应为 1–200 篇。") }
        try locked {
            let old: Settings = try load("settings.json", fallback: Settings())
            try save(settings, file: "settings.json")
            if old.screening != settings.screening {
                var library: Library = try load("library.json", fallback: Library())
                library.screeningPause = nil; library.screeningPauseConfig = nil
                try save(library, file: "library.json")
            }
        }
    }
    @discardableResult public func update<T>(_ body: (inout Library) throws -> T) throws -> T {
        try locked {
            var library: Library = try load("library.json", fallback: Library())
            let result = try body(&library)
            try save(library, file: "library.json")
            return result
        }
    }
    public func workLock() throws -> FileLock { try FileLock(url: root.appendingPathComponent("work.lock"), wait: false) }
    public func activity(_ message: String) throws {
        try update { $0.activity = message; $0.activityAt = Date() }
    }
}
