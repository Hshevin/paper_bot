import Foundation

private final class NoRedirect: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

public enum HTTP {
    public static func data(_ request: URLRequest, authenticated: Bool = false, maxBytes: Int = 8_000_000) async throws -> Data {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 120
        configuration.timeoutIntervalForResource = 240
        let session = URLSession(configuration: configuration, delegate: authenticated ? NoRedirect() : nil, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        do {
            let (bytes, response) = try await session.bytes(for: request)
            guard let response = response as? HTTPURLResponse, (200...299).contains(response.statusCode) else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                throw AppError("请求失败（HTTP \(code)）。请检查地址、模型、密钥或服务限额后重试。")
            }
            guard response.expectedContentLength <= maxBytes else { throw AppError("响应超过大小上限。") }
            var data = Data()
            for try await byte in bytes {
                data.append(byte)
                if data.count > maxBytes { throw AppError("响应超过大小上限。") }
            }
            return data
        } catch let error as AppError { throw error }
        catch is CancellationError { throw CancellationError() }
        catch {
            if Task.isCancelled || (error as? URLError)?.code == .cancelled { throw CancellationError() }
            throw AppError("网络连接失败或超时，请检查网络后重试。")
        }
    }
}
