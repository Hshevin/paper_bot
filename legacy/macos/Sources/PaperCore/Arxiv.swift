import Foundation

public enum ArxivQuery {
    public static func url(topic: Topic, from: Date, to: Date, offset: Int) throws -> URL {
        guard from <= to else { throw AppError("开始日期不能晚于结束日期。") }
        let categories = topic.categories.split(whereSeparator: { $0 == "," || $0.isWhitespace }).map(String.init)
        guard categories.allSatisfy({ $0.range(of: "^[A-Za-z][A-Za-z0-9.-]*(\\.\\*)?$", options: .regularExpression) != nil }) else {
            throw AppError("分类格式不正确，例如 cs.AI、cs.CL 或 physics.*。")
        }
        var clauses: [String] = []
        if !categories.isEmpty { clauses.append("(" + categories.map { "cat:\($0)" }.joined(separator: " OR ") + ")") }
        let keywords = topic.keywords.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !keywords.contains("\"") && !keywords.contains("\\") else { throw AppError("关键词无需引号或反斜杠；多个短语请用英文逗号分隔。") }
        let phrases = keywords.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        if !phrases.isEmpty { clauses.append("(" + phrases.map { "all:\"\($0)\"" }.joined(separator: " OR ") + ")") }
        guard !clauses.isEmpty else { throw AppError("请至少填写一个 arXiv 分类或关键词。") }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMddHHmm"
        clauses.append("submittedDate:[\(formatter.string(from: from)) TO \(formatter.string(from: to))]")
        var components = URLComponents(string: "https://export.arxiv.org/api/query")!
        components.queryItems = [
            URLQueryItem(name: "search_query", value: clauses.joined(separator: " AND ")),
            URLQueryItem(name: "start", value: String(offset)),
            URLQueryItem(name: "max_results", value: "100"),
            URLQueryItem(name: "sortBy", value: "submittedDate"),
            URLQueryItem(name: "sortOrder", value: "ascending")
        ]
        return components.url!
    }
}

public struct AtomFeed: Sendable {
    public var papers: [Paper]
    public var total: Int
    public static func parse(_ data: Data) throws -> AtomFeed {
        let delegate = AtomParser()
        let parser = XMLParser(data: data)
        parser.shouldResolveExternalEntities = false
        parser.delegate = delegate
        guard parser.parse(), delegate.seenFeed, let total = delegate.total, delegate.failure == nil else {
            throw AppError(delegate.failure ?? "arXiv 返回的内容不是有效论文列表。")
        }
        return AtomFeed(papers: delegate.papers, total: total)
    }
}

private final class AtomParser: NSObject, XMLParserDelegate {
    var seenFeed = false
    var total: Int?
    var failure: String?
    var papers: [Paper] = []
    var fields: [String: String] = [:]
    var authors: [String] = []
    var categories: [String] = []
    var entry = false
    var text = ""
    func parser(_ parser: XMLParser, didStartElement element: String, namespaceURI: String?, qualifiedName: String?, attributes: [String: String]) {
        text = ""
        if element == "feed" { seenFeed = true }
        if element == "entry" { entry = true; fields = [:]; authors = []; categories = [] }
        if entry && element == "category", let term = attributes["term"] { categories.append(term) }
    }
    func parser(_ parser: XMLParser, foundCharacters string: String) { text += string }
    func parser(_ parser: XMLParser, didEndElement element: String, namespaceURI: String?, qualifiedName: String?) {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if element.hasSuffix("totalResults") { total = Int(value) }
        if entry {
            if ["id", "title", "summary", "published", "updated"].contains(element) { fields[element] = value }
            if element == "name" { authors.append(value) }
            if element == "entry" { finish(); entry = false }
        }
        text = ""
    }
    func finish() {
        let rawID = fields["id", default: ""]
        guard let range = rawID.range(of: "/abs/") else { failure = "arXiv 查询被拒绝或返回无效论文。"; return }
        let fullID = String(rawID[range.upperBound...])
        let pattern = "^((?:[0-9]{4}\\.[0-9]{4,5})|(?:[A-Za-z.-]+/[0-9]{7}))v([0-9]+)$"
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: fullID, range: NSRange(fullID.startIndex..., in: fullID)),
              let idRange = Range(match.range(at: 1), in: fullID),
              let versionRange = Range(match.range(at: 2), in: fullID),
              let version = Int(fullID[versionRange]),
              let published = ISO8601DateFormatter().date(from: fields["published", default: ""]),
              let updated = ISO8601DateFormatter().date(from: fields["updated", default: ""]),
              let title = fields["title"], let abstract = fields["summary"] else {
            failure = "arXiv 论文缺少必要字段。"; return
        }
        papers.append(Paper(id: String(fullID[idRange]), version: version,
                            title: title.split(whereSeparator: \.isWhitespace).joined(separator: " "),
                            abstract: abstract, authors: authors, categories: categories,
                            published: published, updated: updated))
    }
}
