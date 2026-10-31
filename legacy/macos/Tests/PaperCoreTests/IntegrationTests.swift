import Foundation
import CoreGraphics
import CoreText
import PaperCore

struct IntegrationTests {
    let baseURL: String
    func run() async throws {
        var config = ModelConfig(); config.baseURL = baseURL; config.model = "valid"; config.apiKey = "fixture-key"
        for provider in Provider.allCases {
            config.provider = provider
            let result = try await LLMClient.complete(config: config, system: "Test", user: "Test")
            expect(result.contains("本地测试报告"))
        }
        config.model = "redirect"
        do { _ = try await LLMClient.complete(config: config, system: "", user: ""); expect(false) } catch { expect(error is AppError) }
        config.model = "slow"
        let slowConfig = config
        let task = Task { try await LLMClient.complete(config: slowConfig, system: "", user: "") }
        try await Task.sleep(nanoseconds: 150_000_000)
        task.cancel()
        do { _ = try await task.value; expect(false) } catch { expect(error is CancellationError) }

        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try Store(root: root)
        let topic = Topic(name: "AI", categories: "cs.AI")
        let paper = try require(AtomFeed.parse(Data(CoreTests.feed.utf8)).papers.first)
        var second = paper; second.id = "2601.00002"; second.published = Date()
        try store.update { value in
            value.topics = [topic]; value.merge([paper, second], topicID: topic.id)
            value.papers[0].screeningErrors[topic.id.uuidString] = "Previous failure"
        }
        config.model = "valid"
        var settings = Settings(); settings.screening = config; settings.reading = config; settings.screeningLimit = 1
        try store.saveSettings(settings)
        let otherTopic = Topic(name: "ML", categories: "cs.LG")
        try store.update { value in
            value.topics.append(otherTopic); value.papers[0].topicIDs.append(otherTopic.id)
            value.papers[0].screeningErrors[otherTopic.id.uuidString] = "Unattempted failure"
        }
        let engine = Engine(store: store, arxivData: { _ in Data(CoreTests.feed.utf8) })
        try await engine.screenPendingNow(paperID: paper.id)
        expect(try store.library().papers[0].screenings[topic.id.uuidString]?.score == 87)
        expect(try store.library().papers[1].screenings.isEmpty)
        expect(try store.library().papers[0].screeningErrors[otherTopic.id.uuidString] == "Unattempted failure")

        settings.screening.model = "bad-json"; try store.saveSettings(settings)
        try await engine.screenPendingNow(paperID: second.id)
        expect(try store.library().screeningPause != nil)
        // A subsequent automatic round must not try another model request.
        try await engine.run(force: true)
        expect(try store.library().papers[1].screenings.isEmpty)

        let pdf = store.pdfDirectory.appendingPathComponent(paper.fileName)
        var bounds = CGRect(x: 0, y: 0, width: 612, height: 792)
        let consumer = try require(CGDataConsumer(url: pdf as CFURL))
        let context = try require(CGContext(consumer: consumer, mediaBox: &bounds, nil))
        context.beginPDFPage(nil)
        let font = CTFontCreateWithName("Helvetica" as CFString, 12, nil)
        for line in 0..<15 {
            context.textPosition = CGPoint(x: 40, y: 740 - line * 30)
            let content = NSAttributedString(string: "Research evidence line \(line). Method and experiment details in this paper.", attributes: [NSAttributedString.Key(kCTFontAttributeName as String): font])
            CTLineDraw(CTLineCreateWithAttributedString(content), context)
        }
        context.endPDFPage(); context.closePDF()
        expect(try PDFText.extract(pdf).contains("Research evidence"))
        try await engine.readFull(paperID: paper.id)
        expect(try store.library().papers[0].fullAnalysis?.text.contains("本地测试报告") == true)
        expect(try store.library().papers[0].fullAnalysis?.sourceVersion == paper.version)
    }
}
