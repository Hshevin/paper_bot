import Foundation
import PaperCore

// launchd starts a fresh process periodically. The persisted interval and work lock
// keep scheduling independent from the main application's lifetime.
let arguments = CommandLine.arguments
if arguments.contains("--smoke-range") {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("PaperHarbor-live-" + UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    do {
        let store = try Store(root: root)
        let topic = Topic(name: "Transformer live check", keywords: "attention is all you need")
        try store.update { $0.topics = [topic] }
        let formatter = ISO8601DateFormatter()
        try await Engine(store: store).fetchRange(topic: topic, from: formatter.date(from: "2017-06-01T00:00:00Z")!, to: formatter.date(from: "2017-06-30T23:59:00Z")!)
        let papers = try store.library().papers
        guard papers.contains(where: { $0.id == "1706.03762" }) else { throw AppError("Expected paper missing from date query") }
        print("arXiv date range check: \(papers.count) papers; expected Transformer paper present")
    } catch { print(error.localizedDescription); exit(1) }
} else if arguments.contains("--smoke-fetch") {
    do {
        var request = URLRequest(url: URL(string: "https://export.arxiv.org/api/query?id_list=1706.03762&max_results=1")!)
        request.setValue("PaperHarbor/0.1", forHTTPHeaderField: "User-Agent")
        let feed = try AtomFeed.parse(await HTTP.data(request))
        guard let paper = feed.papers.first else { throw AppError("No paper returned") }
        print("arXiv live check: \(paper.id)v\(paper.version) — \(paper.title)")
    } catch { print(error.localizedDescription); exit(1) }
} else {
    do {
        let root = ProcessInfo.processInfo.environment["PAPERHARBOR_DATA_DIR"].map { URL(fileURLWithPath: $0, isDirectory: true) }
        let store = try Store(root: root)
        try await Engine(store: store).run(force: arguments.contains("--once"))
    } catch {
        // Sanitized application errors only; never print settings or remote response bodies.
        fputs("PaperHarbor: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}
