import SwiftUI
import PDFKit
import PaperCore
import UniformTypeIdentifiers

struct PaperDetail: View {
    @EnvironmentObject var model: AppModel
    let paper: Paper
    let preferredTopic: UUID?
    @ViewState private var tab = "analysis"
    @ViewState private var confirmReading = false
    var localPDF: URL? {
        guard let url = model.store?.pdfDirectory.appendingPathComponent(paper.fileName), FileManager.default.fileExists(atPath: url.path) else { return nil }
        return url
    }
    var screenings: [(String, Analysis)] {
        paper.screenings.sorted {
            if $0.key == preferredTopic?.uuidString { return true }
            if $1.key == preferredTopic?.uuidString { return false }
            return ($0.value.score ?? 0) > ($1.value.score ?? 0)
        }
    }
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Picker("内容", selection: $tab) {
                    Text("概览与分析").tag("analysis")
                    Text("PDF 原文").tag("pdf")
                }.pickerStyle(.segmented).frame(maxWidth: 260)
                Spacer()
                Button { update { $0.starred.toggle() } } label: {
                    Image(systemName: paper.starred ? "star.fill" : "star")
                }.help(paper.starred ? "取消收藏" : "收藏论文").accessibilityLabel("收藏论文")
                Button { update { $0.read.toggle() } } label: {
                    Image(systemName: paper.read ? "checkmark.circle.fill" : "checkmark.circle")
                }.help(paper.read ? "标为未读" : "标为已读").accessibilityLabel("切换已读状态")
                Menu {
                    Button("打开 arXiv 页面") { NSWorkspace.shared.open(paper.webURL) }
                    Button("在浏览器中打开 PDF") { NSWorkspace.shared.open(paper.pdfURL) }
                    Button("导出 Markdown") { export() }
                } label: { Image(systemName: "ellipsis.circle") }.menuStyle(.borderlessButton).frame(width: 24)
            }.padding(20)
            Divider()
            if tab == "pdf" {
                if let localPDF { PDFReader(url: localPDF) }
                else {
                    ContentUnavailableView {
                        Label("尚未缓存 PDF", systemImage: "doc.richtext")
                    } description: { Text("全文精读时会下载并保存原文；也可以直接在浏览器阅读。") }
                    actions: { Button("打开 PDF") { NSWorkspace.shared.open(paper.pdfURL) } }
                }
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        header
                        Divider()
                        VStack(alignment: .leading, spacing: 12) {
                            Label("原始摘要", systemImage: "text.alignleft").font(.headline)
                            Text(paper.abstract).font(.body).lineSpacing(5).textSelection(.enabled)
                        }
                        if screenings.isEmpty {
                            VStack(alignment: .leading, spacing: 10) {
                                Label("摘要筛选", systemImage: "sparkles").font(.headline)
                                Text("配置摘要模型后，更新订阅会自动筛选；也可以立即处理待筛选论文。").foregroundStyle(.secondary)
                                Button("筛选待分析摘要") { model.perform { try await $0.screenPendingNow() } }.disabled(model.busy)
                            }
                        }
                        ForEach(screenings, id: \.0) { key, analysis in
                            analysisBlock(title: "摘要筛选 · " + (model.library.topics.first { $0.id.uuidString == key }?.name ?? "历史主题"), analysis: analysis)
                        }
                        if !paper.screeningErrors.isEmpty {
                            VStack(alignment: .leading, spacing: 8) {
                                ForEach(paper.screeningErrors.keys.sorted(), id: \.self) { key in
                                    Label(paper.screeningErrors[key] ?? "", systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                                }
                                Button("重试失败的摘要分析") {
                                    model.perform { try await $0.screenPendingNow(paperID: paper.id) }
                                }.disabled(model.busy)
                            }.font(.caption)
                        }
                        Divider()
                        VStack(alignment: .leading, spacing: 12) {
                            HStack {
                                Label("全文精读", systemImage: "book.closed").font(.headline)
                                Spacer()
                                Button(paper.fullAnalysis == nil ? "开始精读" : "重新分析") { confirmReading = true }
                                    .buttonStyle(.borderedProminent).tint(.teal).disabled(model.busy)
                            }
                            if let analysis = paper.fullAnalysis {
                                analysisBlock(title: "方法、证据与局限", analysis: analysis)
                            } else {
                                Text("提取 PDF 全文，分段分析研究方法、创新点、实验依据和局限。仅在你点击后调用精读模型。").foregroundStyle(.secondary).lineSpacing(4)
                            }
                            if let error = paper.fullError { Label(error, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange) }
                        }
                    }.padding(28).frame(maxWidth: 840, alignment: .leading).frame(maxWidth: .infinity)
                }
            }
        }
        .confirmationDialog("开始全文精读？", isPresented: $confirmReading, titleVisibility: .visible) {
            Button("下载 PDF 并调用精读模型") { model.perform { try await $0.readFull(paperID: paper.id) } }
        } message: { Text("正文会发送到你设置的精读 API。长论文会分段处理并产生多次调用费用；最多处理 24 万字符，约 15 次调用。") }
    }
    private var header: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(paper.categories.joined(separator: " · ")).font(.caption.weight(.semibold)).foregroundStyle(.teal)
            Text(paper.title).font(.system(size: 25, weight: .semibold, design: .serif)).textSelection(.enabled)
            Text(paper.authors.joined(separator: ", ")).font(.subheadline).foregroundStyle(.secondary).textSelection(.enabled)
            HStack {
                Text("arXiv:\(paper.id)v\(paper.version)")
                Spacer()
                Text(paper.published, format: .dateTime.year().month().day())
            }.font(.caption).foregroundStyle(.secondary)
        }
    }
    private func analysisBlock(title: String, analysis: Analysis) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(title).font(.headline)
                Spacer()
                if let score = analysis.score { Text("\(score) / 100").font(.headline.monospacedDigit()).foregroundStyle(.teal) }
            }
            ReportText(text: analysis.text)
            Text("\(analysis.model) · 基于 v\(analysis.sourceVersion) · \(analysis.createdAt.formatted(date: .abbreviated, time: .shortened))")
                .font(.caption).foregroundStyle(.secondary)
            if analysis.sourceVersion != paper.version { Text("这是旧版本分析，建议重新分析当前版本。").font(.caption).foregroundStyle(.orange) }
        }.padding(18).background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
    }
    private func update(_ body: (inout Paper) -> Void) {
        model.mutate { library in
            if let i = library.papers.firstIndex(where: { $0.id == paper.id }) { body(&library.papers[i]) }
        }
    }
    private func export() {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.plainText]
        panel.nameFieldStringValue = paper.id.replacingOccurrences(of: "/", with: "_") + ".md"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        var text = "# \(paper.title)\n\n\(paper.webURL.absoluteString)\n\n## 摘要\n\n\(paper.abstract)"
        for (key, analysis) in screenings {
            let name = model.library.topics.first { $0.id.uuidString == key }?.name ?? "历史主题"
            text += "\n\n## 筛选 · \(name) · \(analysis.score ?? 0)/100\n\n\(analysis.text)\n\n模型：\(analysis.model)，版本：v\(analysis.sourceVersion)"
        }
        if let analysis = paper.fullAnalysis { text += "\n\n## 全文精读\n\n\(analysis.text)\n\n模型：\(analysis.model)，版本：v\(analysis.sourceVersion)" }
        do { try text.write(to: url, atomically: true, encoding: .utf8) } catch { model.error = error.localizedDescription }
    }
}

private struct PDFReader: NSViewRepresentable {
    let url: URL
    func makeNSView(context: Context) -> PDFView { let view = PDFView(); view.autoScales = true; return view }
    func updateNSView(_ view: PDFView, context: Context) {
        if view.document?.documentURL != url { view.document = PDFDocument(url: url) }
    }
}
