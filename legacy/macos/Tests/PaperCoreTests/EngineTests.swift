import Foundation
import PaperCore

private actor PageSource {
    var calls = 0
    let failSecond: Bool
    init(failSecond: Bool) { self.failSecond = failSecond }
    func data(_ request: URLRequest) throws -> Data {
        calls += 1
        if failSecond && calls == 2 { throw AppError("Simulated outage") }
        let value = CoreTests.feed.replacingOccurrences(of: "<opensearch:totalResults>1", with: "<opensearch:totalResults>2")
            .replacingOccurrences(of: "2601.00001", with: calls == 1 ? "2601.00001" : "2601.00002")
        return Data(value.utf8)
    }
}
struct EngineTests {
    func testPartialFailurePreservesPapersWithoutAdvancingCursor() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try Store(root: root)
        try store.update { $0.topics.append(Topic(name: "AI", categories: "cs.AI")) }
        let failing = PageSource(failSecond: true)
        try await Engine(store: store, arxivData: { try await failing.data($0) }).run(force: true)
        expect(try store.library().papers.count == 1)
        expect(try store.library().topics[0].lastSync == nil)
        expect(try store.library().topics[0].lastError != nil)
        let recovered = PageSource(failSecond: false)
        try await Engine(store: store, arxivData: { try await recovered.data($0) }).run(force: true)
        expect(try store.library().papers.count == 2)
        expect(try store.library().topics[0].lastSync != nil)
        expect(try store.library().topics[0].lastError == nil)
    }
    func testScheduleSkipsBeforeInterval() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try Store(root: root)
        try store.update { $0.topics.append(Topic(name: "AI", categories: "cs.AI")); $0.lastRun = Date() }
        let pages = PageSource(failSecond: false)
        try await Engine(store: store, arxivData: { try await pages.data($0) }).run()
        let calls = await pages.calls
        expect(calls == 0)
        expect(try store.library().papers.isEmpty)
    }
}
