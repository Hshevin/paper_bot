import Foundation
import PaperCore

struct CoreTests {
    func testQueryUsesUTCAndEscapesLiteralKeywords() throws {
        let topic = Topic(name: "LLM", categories: "cs.AI,cs.CL", keywords: "large language model")
        let start = ISO8601DateFormatter().date(from: "2026-01-01T00:00:00Z")!
        let url = try ArxivQuery.url(topic: topic, from: start, to: start.addingTimeInterval(86400), offset: 100)
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)!.queryItems!
        let query = items.first { $0.name == "search_query" }!.value!
        expect(query.contains("(cat:cs.AI OR cat:cs.CL)"))
        expect(query.contains("all:\"large language model\""))
        expect(query.contains("submittedDate:[202601010000 TO 202601020000]"))
        expect(items.first { $0.name == "start" }?.value == "100")
    }

    func testAtomParsesVersionsAuthorsAndCategories() throws {
        let feed = try AtomFeed.parse(Data(Self.feed.utf8))
        expect(feed.total == 1)
        expect(feed.papers.first?.id == "2601.00001")
        expect(feed.papers.first?.version == 2)
        expect(feed.papers.first?.authors == ["Alice Example"])
        expect(feed.papers.first?.title == "A paper title")
        expect(feed.papers.first?.categories == ["cs.AI"])
        expect(feed.papers.first?.pdfURL.absoluteString == "https://arxiv.org/pdf/2601.00001v2")
    }

    func testRejectsMalformedAndErrorFeeds() {
        expectThrows { try AtomFeed.parse(Data("<feed><entry>".utf8)) }
        expectThrows { try AtomFeed.parse(Data("<html>Bad gateway</html>".utf8)) }
        expectThrows { try AtomFeed.parse(Data(Self.feed.replacingOccurrences(of: "http://arxiv.org/abs/2601.00001v2", with: "http://arxiv.org/api/errors#bad").utf8)) }
    }

    static let feed = """
    <?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
    <opensearch:totalResults>1</opensearch:totalResults><entry>
    <id>http://arxiv.org/abs/2601.00001v2</id><title>A paper\n title</title><summary>An abstract.</summary>
    <published>2026-01-01T12:00:00Z</published><updated>2026-01-02T12:00:00Z</updated>
    <author><name>Alice Example</name></author><category term="cs.AI"/>
    </entry></feed>
    """
}

extension CoreTests {
    func testMergeDeduplicatesAndInvalidatesChangedVersion() throws {
        var paper = try require(AtomFeed.parse(Data(Self.feed.utf8)).papers.first)
        let topicA = UUID(), topicB = UUID()
        var library = Library()
        library.merge([paper], topicID: topicA)
        library.papers[0].starred = true
        library.papers[0].screenings[topicA.uuidString] = Analysis(text: "Summary", score: 88, model: "test", sourceVersion: 2)
        library.merge([paper], topicID: topicB)
        expect(library.papers.count == 1)
        expect(library.papers[0].topicIDs.count == 2)
        expect(library.papers[0].screenings.count == 1)
        paper.version = 3
        library.merge([paper], topicID: topicA)
        expect(library.papers[0].starred)
        expect(library.papers[0].screenings.isEmpty)
    }

    func testStorePreservesIndependentChangesAndProtectsSecrets() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let first = try Store(root: root), second = try Store(root: root)
        try first.update { $0.topics.append(Topic(name: "One", categories: "cs.AI")) }
        try second.update { $0.topics.append(Topic(name: "Two", categories: "cs.CL")) }
        expect(try first.library().topics.count == 2)
        try first.saveSettings(Settings())
        let attributes = try FileManager.default.attributesOfItem(atPath: root.appendingPathComponent("settings.json").path)
        expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        let held = try first.workLock()
        expectThrows { try second.workLock() }
        withExtendedLifetime(held) {}
        try Data("corrupted".utf8).write(to: root.appendingPathComponent("library.json"))
        expectThrows { try second.update { $0.topics = [] } }
        expect(try String(contentsOf: root.appendingPathComponent("library.json"), encoding: .utf8) == "corrupted")
    }
}

extension CoreTests {
    func testStaleFailureCannotPoisonEditedTopicOrNewVersion() throws {
        let paper = try require(AtomFeed.parse(Data(Self.feed.utf8)).papers.first)
        let topic = Topic(name: "AI", categories: "cs.AI")
        var library = Library(); library.topics = [topic]
        library.merge([paper], topicID: topic.id)
        library.topics[0].keywords = "changed"
        expect(!library.recordScreeningFailure("Old failure", paper: paper, topic: topic, config: ModelConfig()))
        expect(library.screeningPause == nil)
        library.topics[0] = topic; library.papers[0].version = 3
        expect(!library.recordScreeningFailure("Old failure", paper: paper, topic: topic, config: ModelConfig()))
        library.papers[0].version = paper.version
        expect(library.recordScreeningFailure("Current failure", paper: paper, topic: topic, config: ModelConfig()))
        expect(library.screeningPause != nil)
    }
}
