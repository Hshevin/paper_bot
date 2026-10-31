import SwiftUI

/// Render the report's block structure natively; SwiftUI Text alone only renders inline Markdown.
struct ReportText: View {
    let text: String
    private struct Block {
        var kind: Kind
        var text: String
        enum Kind { case paragraph, heading, bullet, code }
    }
    private var blocks: [Block] {
        var result: [Block] = []
        var lines: [String] = []
        var inCode = false
        func flush() {
            if !lines.isEmpty {
                result.append(Block(kind: inCode ? .code : .paragraph, text: lines.joined(separator: "\n")))
                lines = []
            }
        }
        for line in text.components(separatedBy: "\n") {
            let value = line.trimmingCharacters(in: .whitespaces)
            if value.hasPrefix("```") { flush(); inCode.toggle(); continue }
            if inCode { lines.append(line); continue }
            if value.isEmpty { flush(); continue }
            let hashes = value.prefix(while: { $0 == "#" }).count
            if (1...6).contains(hashes), value.dropFirst(hashes).first == " " {
                flush(); result.append(Block(kind: .heading, text: String(value.dropFirst(hashes + 1)))); continue
            }
            if value.hasPrefix("- ") || value.hasPrefix("* ") {
                flush(); result.append(Block(kind: .bullet, text: String(value.dropFirst(2)))); continue
            }
            lines.append(line)
        }
        flush()
        return result
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block.kind {
                case .heading:
                    Text(.init(block.text)).font(.headline).padding(.top, 4)
                case .bullet:
                    HStack(alignment: .top, spacing: 8) {
                        Text("•")
                        Text(.init(block.text)).lineSpacing(4)
                    }
                case .code:
                    Text(block.text).font(.system(.callout, design: .monospaced))
                        .padding(10).frame(maxWidth: .infinity, alignment: .leading).background(.quaternary.opacity(0.4))
                case .paragraph:
                    Text(.init(block.text)).lineSpacing(5)
                }
            }
        }.textSelection(.enabled)
    }
}
