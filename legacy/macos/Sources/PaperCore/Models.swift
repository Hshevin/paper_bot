import Foundation
import CryptoKit

public struct AppError: LocalizedError, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

public struct Topic: Codable, Identifiable, Hashable, Sendable {
    public var id = UUID()
    public var name: String
    public var categories: String
    public var keywords: String
    public var enabled = true
    public var lastSync: Date?
    public var lastError: String?
    public init(name: String, categories: String = "", keywords: String = "") {
        self.name = name; self.categories = categories; self.keywords = keywords
    }
    public var criteria: String { "主题：\(name)；分类：\(categories)；关键词：\(keywords)" }
}

public struct Analysis: Codable, Sendable {
    public var text: String
    public var score: Int?
    public var model: String
    public var createdAt = Date()
    public var sourceVersion: Int
    public init(text: String, score: Int? = nil, model: String, sourceVersion: Int) {
        self.text = text; self.score = score; self.model = model; self.sourceVersion = sourceVersion
    }
}

public struct Paper: Codable, Identifiable, Sendable {
    public var id: String
    public var version: Int
    public var title: String
    public var abstract: String
    public var authors: [String]
    public var categories: [String]
    public var published: Date
    public var updated: Date
    public var topicIDs: [UUID] = []
    public var screenings: [String: Analysis] = [:]
    public var screeningErrors: [String: String] = [:]
    public var fullAnalysis: Analysis?
    public var fullError: String?
    public var starred = false
    public var read = false
    public var pdfURL: URL { URL(string: "https://arxiv.org/pdf/\(id)v\(version)")! }
    public var webURL: URL { URL(string: "https://arxiv.org/abs/\(id)")! }
    public var fileName: String { id.replacingOccurrences(of: "/", with: "_") + "v\(version).pdf" }
}

public enum Provider: String, Codable, CaseIterable, Identifiable, Sendable {
    case openAI, anthropic
    public var id: String { rawValue }
    public var label: String { self == .openAI ? "OpenAI 兼容" : "Anthropic 原生" }
}

public struct ModelConfig: Codable, Equatable, Sendable {
    public var provider: Provider = .openAI
    public var baseURL = "https://api.openai.com/v1"
    public var apiKey = ""
    public var model = ""
    public init() {}
    public var fingerprint: String {
        let encoder = JSONEncoder(); encoder.outputFormatting = .sortedKeys
        return SHA256.hash(data: (try? encoder.encode(self)) ?? Data()).map { String(format: "%02x", $0) }.joined()
    }
    public var isConfigured: Bool { !model.trimmingCharacters(in: .whitespaces).isEmpty }
}

public struct Settings: Codable, Sendable {
    public var screening = ModelConfig()
    public var reading = ModelConfig()
    public var screeningProfiles: [String: ModelConfig] = [:]
    public var readingProfiles: [String: ModelConfig] = [:]
    public var intervalMinutes = 60
    public var screeningLimit = 20
    public var automaticScreening = true
    public init() {}
}

public struct Library: Codable, Sendable {
    public var topics: [Topic] = []
    public var papers: [Paper] = []
    public var lastRun: Date?
    public var lastArxivRequest: Date?
    public var screeningPause: String?
    public var screeningPauseConfig: String?
    public var activity = "准备就绪"
    public var activityAt = Date()
    public init() {}
    @discardableResult public mutating func recordScreeningFailure(_ message: String, paper: Paper, topic: Topic, config: ModelConfig) -> Bool {
        guard let i = papers.firstIndex(where: { $0.id == paper.id && $0.version == paper.version && $0.topicIDs.contains(topic.id) }),
              topics.contains(where: { $0.id == topic.id && $0.criteria == topic.criteria }) else { return false }
        papers[i].screeningErrors[topic.id.uuidString] = message
        screeningPause = message; screeningPauseConfig = config.fingerprint
        return true
    }
    public mutating func merge(_ incoming: [Paper], topicID: UUID) {
        for var paper in incoming {
            if let i = papers.firstIndex(where: { $0.id == paper.id }) {
                var old = papers[i]
                if !old.topicIDs.contains(topicID) { old.topicIDs.append(topicID) }
                if paper.version > old.version {
                    paper.topicIDs = old.topicIDs
                    paper.starred = old.starred; paper.read = old.read
                    // A new version must be screened again; retain full analysis with its version label.
                    paper.fullAnalysis = old.fullAnalysis
                    papers[i] = paper
                } else { papers[i] = old }
            } else {
                paper.topicIDs = [topicID]
                papers.append(paper)
            }
        }
    }
}
