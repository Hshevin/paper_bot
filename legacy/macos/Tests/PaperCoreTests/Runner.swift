import Foundation
import PaperCore

private var failures = 0
func expect(_ expression: @autoclosure () throws -> Bool, file: StaticString = #filePath, line: UInt = #line) {
    do {
        if try !expression() { failures += 1; print("FAIL \(file):\(line)") }
    } catch { failures += 1; print("FAIL \(file):\(line): \(error)") }
}
func require<T>(_ value: T?) throws -> T {
    guard let value else { throw AppError("Required value missing") }
    return value
}
func expectThrows<T>(file: StaticString = #filePath, line: UInt = #line, _ body: () throws -> T) {
    do { _ = try body(); failures += 1; print("FAIL expected error \(file):\(line)") } catch {}
}

@main struct TestRunner {
    static func main() async {
        let core = CoreTests(), llm = LLMTests()
        let tests: [(String, () throws -> Void)] = [
            ("Stale failures cannot poison edited records", core.testStaleFailureCannotPoisonEditedTopicOrNewVersion),
            ("UTC date query and keywords", core.testQueryUsesUTCAndEscapesLiteralKeywords),
            ("Atom metadata and version parsing", core.testAtomParsesVersionsAuthorsAndCategories),
            ("Reject malformed/error feeds", core.testRejectsMalformedAndErrorFeeds),
            ("Version deduplication preserves user state", core.testMergeDeduplicatesAndInvalidatesChangedVersion),
            ("Atomic storage, permissions and corruption protection", core.testStorePreservesIndependentChangesAndProtectsSecrets),
            ("Both provider request protocols", llm.testProviderRequestsAndCustomPaths),
            ("Endpoint transport validation", llm.testEndpointRejectsInsecureRemoteAndAllowsLocal),
            ("Provider response and score validation", llm.testResponsesAndSchemaAreValidated),
            ("Full text chunking preserves all content", llm.testChunkingPreservesAllText)
        ]
        for (name, test) in tests {
            let before = failures
            do { try test() } catch { failures += 1; print("FAIL \(name): \(error)") }
            if failures == before { print("PASS \(name)") }
        }
        let engine = EngineTests()
        let asyncTests: [(String, () async throws -> Void)] = [
            ("Partial fetch recovery and cursor correctness", engine.testPartialFailurePreservesPapersWithoutAdvancingCursor),
            ("Background interval skips early runs", engine.testScheduleSkipsBeforeInterval)
        ]
        for (name, test) in asyncTests {
            let before = failures
            do { try await test() } catch { failures += 1; print("FAIL \(name): \(error)") }
            if failures == before { print("PASS \(name)") }
        }
        print("\(tests.count + asyncTests.count) checks, \(failures) failures")
        if let baseURL = ProcessInfo.processInfo.environment["PAPERHARBOR_TEST_API"] {
            let before = failures
            do { try await IntegrationTests(baseURL: baseURL).run() }
            catch { failures += 1; print("FAIL local HTTP integration: \(error)") }
            if failures == before { print("PASS local HTTP: both providers, redirects, cancellation, targeted retry, failure pause, PDF extraction and full reading") }
        }
        if failures != 0 { exit(1) }
    }
}
