import Foundation
import PDFKit

public enum PDFText {
    public static func extract(_ url: URL) throws -> String {
        guard let document = PDFDocument(url: url), !document.isLocked else { throw AppError("PDF 无效或已加密。") }
        guard document.pageCount <= 300 else { throw AppError("PDF 超过 300 页，请先在外部阅读。") }
        let text = (0..<document.pageCount).compactMap { index -> String? in
            guard let value = document.page(at: index)?.string else { return nil }
            return "\n[第 \(index + 1) 页]\n" + value
        }.joined()
        guard text.filter({ !$0.isWhitespace }).count > 150 else { throw AppError("PDF 没有足够的可提取文字，可能是扫描件；首版不支持 OCR。") }
        guard text.count <= 240_000 else { throw AppError("全文超过 24 万字符，本次未调用模型。请在外部阅读。") }
        return text
    }
    public static func chunks(_ text: String, size: Int = 18000) -> [String] {
        precondition(size > 0)
        var result: [String] = []
        var start = text.startIndex
        while start < text.endIndex {
            let end = text.index(start, offsetBy: size, limitedBy: text.endIndex) ?? text.endIndex
            result.append(String(text[start..<end])); start = end
        }
        return result
    }
}
