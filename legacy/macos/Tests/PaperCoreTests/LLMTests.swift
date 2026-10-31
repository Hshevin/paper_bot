import Foundation
import PaperCore

struct LLMTests {
    func testProviderRequestsAndCustomPaths() throws {
        var config = ModelConfig()
        config.model = "my-model"; config.apiKey = "test-secret"
        config.baseURL = "https://example.com/proxy/v1/"
        let request = try LLMClient.request(config: config, system: "system", user: "paper")
        expect(request.url?.absoluteString == "https://example.com/proxy/v1/chat/completions")
        expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-secret")
        config.provider = .anthropic; config.baseURL = "https://example.com/v1/messages"
        let anthropic = try LLMClient.request(config: config, system: "system", user: "paper")
        expect(anthropic.url?.absoluteString == "https://example.com/v1/messages")
        expect(anthropic.value(forHTTPHeaderField: "x-api-key") == "test-secret")
        let body = try require(JSONSerialization.jsonObject(with: anthropic.httpBody!) as? [String: Any])
        expect(body["system"] as? String == "system")
        expect(anthropic.value(forHTTPHeaderField: "Authorization") == nil)
    }
    func testEndpointRejectsInsecureRemoteAndAllowsLocal() throws {
        var config = ModelConfig(); config.model = "local"
        config.baseURL = "http://example.com/v1"
        expectThrows { try LLMClient.request(config: config, system: "", user: "") }
        config.baseURL = "http://localhost:11434/v1"
        _ = try LLMClient.request(config: config, system: "", user: "")
    }
    func testResponsesAndSchemaAreValidated() throws {
        let openai = Data(#"{"choices":[{"message":{"content":"hello"},"finish_reason":"stop"}]}"#.utf8)
        expect(try LLMClient.response(openai, provider: .openAI) == "hello")
        let anthropic = Data(#"{"content":[{"type":"text","text":"world"}],"stop_reason":"end_turn"}"#.utf8)
        expect(try LLMClient.response(anthropic, provider: .anthropic) == "world")
        expectThrows { try LLMClient.response(Data(#"{"choices":[]}"#.utf8), provider: .openAI) }
        expectThrows { try ScreeningResult.parse(#"{"summary":"x","score":101,"reason":"y"}"#) }
        let result = try ScreeningResult.parse("```json\n{\"summary\":\"摘要\",\"score\":90,\"reason\":\"相关\"}\n```")
        expect(result.score == 90)
    }
    func testChunkingPreservesAllText() {
        let text = String(repeating: "论文内容 abc\n", count: 10000)
        let chunks = PDFText.chunks(text, size: 12000)
        expect(chunks.joined() == text)
        expect(chunks.allSatisfy { $0.count <= 12000 })
    }
}
