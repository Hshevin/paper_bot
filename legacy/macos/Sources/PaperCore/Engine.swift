import Foundation

public actor Engine {
    private let store: Store
    private let arxivData: @Sendable (URLRequest) async throws -> Data
    public init(store: Store, arxivData: @escaping @Sendable (URLRequest) async throws -> Data = { try await HTTP.data($0) }) {
        self.store = store; self.arxivData = arxivData
    }

    private func fetch(topic: Topic, from: Date, to: Date) async throws {
        var offset = 0
        while true {
            try Task.checkCancellation()
            guard try store.library().topics.contains(where: { $0.id == topic.id && $0.criteria == topic.criteria }) else {
                throw AppError("主题已修改或删除，本次任务已停止。")
            }
            try store.activity("抓取「\(topic.name)」· 已检查 \(offset) 篇")
            let lastRequest = try store.library().lastArxivRequest ?? Date.distantPast
            let delay = max(0, 3.1 - Date().timeIntervalSince(lastRequest))
            if delay > 0 { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
            var request = URLRequest(url: try ArxivQuery.url(topic: topic, from: from, to: to, offset: offset))
            request.setValue("PaperHarbor/0.1 (personal research reader)", forHTTPHeaderField: "User-Agent")
            try store.update { $0.lastArxivRequest = Date() }
            let feed = try AtomFeed.parse(await arxivData(request))
            guard !feed.papers.isEmpty || feed.total == 0 || offset >= feed.total else {
                throw AppError("arXiv 分页暂时不完整，已保存收到的论文，下次将补抓。")
            }
            try store.update { library in
                guard library.topics.contains(where: { $0.id == topic.id && $0.criteria == topic.criteria }) else { return }
                library.merge(feed.papers, topicID: topic.id)
            }
            offset += feed.papers.count
            if offset >= feed.total { break }
            guard offset < 30000 else { throw AppError("结果超过 arXiv 单次查询范围，请缩小日期或主题；已收到的论文已保存。") }
        }
    }

    public func run(force: Bool = false) async throws {
        let lock = try store.workLock()
        defer { withExtendedLifetime(lock) {} }
        let settings = try store.settings()
        let library = try store.library()
        guard force || library.lastRun == nil || Date().timeIntervalSince(library.lastRun!) >= Double(settings.intervalMinutes * 60) else { return }
        var failures = 0
        for topic in library.topics where topic.enabled {
            let end = Date()
            let start = (topic.lastSync ?? end).addingTimeInterval(-7 * 86400)
            do {
                try await fetch(topic: topic, from: start, to: end)
                try store.update { value in
                    if let i = value.topics.firstIndex(where: { $0.id == topic.id && $0.criteria == topic.criteria }) {
                        value.topics[i].lastSync = end; value.topics[i].lastError = nil
                    }
                }
            } catch is CancellationError { try store.activity("任务已取消，已抓取论文保留"); throw CancellationError() }
            catch {
                failures += 1
                try store.update { value in
                    if let i = value.topics.firstIndex(where: { $0.id == topic.id }) { value.topics[i].lastError = error.localizedDescription }
                }
            }
        }
        if settings.automaticScreening && settings.screening.isConfigured {
            try await screenPending(settings: settings)
        }
        try store.update { $0.lastRun = Date() }
        try store.activity(failures == 0 ? "更新完成" : "更新完成 · \(failures) 个主题失败，请查看主题详情")
    }

    public func fetchRange(topic: Topic, from: Date, to: Date) async throws {
        let lock = try store.workLock()
        defer { withExtendedLifetime(lock) {} }
        do {
            try await fetch(topic: topic, from: from, to: to)
            let settings = try store.settings()
            if settings.automaticScreening && settings.screening.isConfigured { try await screenPending(settings: settings) }
            try store.activity("日期范围抓取完成")
        } catch { try? store.activity("抓取未完成：\(error.localizedDescription)"); throw error }
    }

    public func screenPendingNow(paperID: String? = nil) async throws {
        let lock = try store.workLock()
        defer { withExtendedLifetime(lock) {} }
        let settings = try store.settings()
        guard settings.screening.isConfigured else { throw AppError("请先配置摘要筛选模型。") }
        try store.update { value in
            value.screeningPause = nil; value.screeningPauseConfig = nil
        }
        try await screenPending(settings: settings, paperID: paperID, retryFailures: true)
        try store.activity(try store.library().screeningPause == nil ? "本轮摘要筛选完成" : "摘要筛选已暂停，请查看错误并重试")
    }

    private func screenPending(settings: Settings, paperID: String? = nil, retryFailures: Bool = false) async throws {
        let snapshot = try store.library()
        guard snapshot.screeningPauseConfig != settings.screening.fingerprint else { return }
        if snapshot.screeningPause != nil {
            try store.update { $0.screeningPause = nil; $0.screeningPauseConfig = nil }
        }
        var attempted = 0
        for paper in snapshot.papers.filter({ paperID == nil || $0.id == paperID }).sorted(by: { $0.published > $1.published }) {
            for topic in snapshot.topics where topic.enabled && paper.topicIDs.contains(topic.id) {
                let key = topic.id.uuidString
                guard paper.screenings[key] == nil, (paper.screeningErrors[key] == nil || retryFailures) else { continue }
                if attempted >= settings.screeningLimit { return }
                try Task.checkCancellation()
                let latest = try store.library()
                guard latest.topics.contains(where: { $0.id == topic.id && $0.enabled && $0.criteria == topic.criteria }) else { continue }
                attempted += 1
                try store.activity("摘要筛选 \(attempted)/\(settings.screeningLimit) · \(paper.title.prefix(55))")
                do {
                    let analysis = try await LLMClient.screen(paper: paper, topic: topic, config: settings.screening)
                    try store.update { value in
                        if let i = value.papers.firstIndex(where: { $0.id == paper.id && $0.version == paper.version }),
                           value.topics.contains(where: { $0.id == topic.id && $0.criteria == topic.criteria }) {
                            value.papers[i].screenings[key] = analysis
                            value.papers[i].screeningErrors[key] = nil
                        }
                    }
                } catch is CancellationError { throw CancellationError() }
                catch {
                    try store.update { value in
                        value.recordScreeningFailure(error.localizedDescription, paper: paper, topic: topic, config: settings.screening)
                    }
                    // Stop after a provider failure, avoiding an entire queue of failed or billable calls.
                    return
                }
            }
        }
    }

    public func readFull(paperID: String) async throws {
        let lock = try store.workLock()
        defer { withExtendedLifetime(lock) {} }
        guard let paper = try store.library().papers.first(where: { $0.id == paperID }) else { throw AppError("未找到论文。") }
        let config = try store.settings().reading
        guard config.isConfigured else { throw AppError("请先配置全文精读模型。") }
        _ = try LLMClient.endpoint(config)
        do {
            try store.activity("下载 PDF · \(paper.title.prefix(55))")
            let url = store.pdfDirectory.appendingPathComponent(paper.fileName)
            if !FileManager.default.fileExists(atPath: url.path) {
                let data = try await HTTP.data(URLRequest(url: paper.pdfURL), maxBytes: 40_000_000)
                guard data.starts(with: Data("%PDF-".utf8)) else { throw AppError("下载内容不是 PDF。") }
                try data.write(to: url, options: .atomic)
            }
            let text = try await Task.detached { try PDFText.extract(url) }.value
            let chunks = PDFText.chunks(text)
            var notes: [String] = []
            let instruction = "分析研究问题、方法、创新、实验依据、局限与适用场景；给出资料中已有的页码依据，不存在的信息写未提供。"
            if chunks.count > 1 {
                for (i, chunk) in chunks.enumerated() {
                    try Task.checkCancellation()
                    try store.activity("全文精读 · 片段 \(i + 1)/\(chunks.count)")
                    let note = try await LLMClient.complete(config: config, system: LLMClient.safety,
                        user: "论文：\(paper.title)\n这是第 \(i + 1)/\(chunks.count) 个片段。\(instruction) 输出不超过 700 字的证据笔记。\n<资料>\n\(chunk)\n</资料>")
                    guard note.count <= 5000 else { throw AppError("模型片段笔记过长，已停止综合；请使用能遵循长度要求的模型。") }
                    notes.append(note)
                }
            }
            try store.activity("生成全文报告 · \(paper.title.prefix(55))")
            let source = chunks.count == 1 ? text : notes.enumerated().map { "片段 \($0.offset + 1) 笔记：\n\($0.element)" }.joined(separator: "\n\n")
            let result = try await LLMClient.complete(config: config, system: LLMClient.safety,
                user: "论文：\(paper.title)\n\(instruction) 用清晰的 Markdown 小节输出中文报告，最后列出值得进一步核对的问题。\(chunks.count > 1 ? "以下是覆盖全文各片段的证据笔记，须保留不确定性。" : "以下是从 PDF 提取的全文。")\n<资料>\n\(source)\n</资料>")
            let analysis = Analysis(text: result, model: config.provider.label + " · " + config.model + " · \(chunks.count) 个全文片段", sourceVersion: paper.version)
            try store.update { value in
                if let i = value.papers.firstIndex(where: { $0.id == paper.id }) {
                    value.papers[i].fullAnalysis = analysis; value.papers[i].fullError = nil
                }
            }
            try store.activity("全文精读完成")
        } catch {
            try store.update { value in
                if let i = value.papers.firstIndex(where: { $0.id == paper.id }) { value.papers[i].fullError = error.localizedDescription }
            }
            try? store.activity("全文精读未完成")
            throw error
        }
    }
}
