import SwiftUI
import Combine
import PaperCore

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @ViewState private var section: String? = "all"
    @ViewState private var selectedPaper: String?
    @ViewState private var search = ""
    @ViewState private var sortByScore = false
    @ViewState private var topicEditor: Topic?
    @ViewState private var rangeTopic: Topic?
    @ViewState private var showNew = false
    @ViewState private var deleteTopic: Topic?
    private let timer = Timer.publish(every: 3, on: .main, in: .common).autoconnect()
    var topic: Topic? { model.library.topics.first { $0.id.uuidString == section } }
    var title: String { topic?.name ?? (section == "starred" ? "收藏论文" : section == "unread" ? "未读论文" : "全部论文") }
    var papers: [Paper] {
        model.library.papers.filter { paper in
            let scope = topic.map { paper.topicIDs.contains($0.id) } ?? (section == "starred" ? paper.starred : section == "unread" ? !paper.read : true)
            return scope && (search.isEmpty || (paper.title + " " + paper.abstract + " " + paper.authors.joined(separator: " ")).localizedCaseInsensitiveContains(search))
        }.sorted {
            if sortByScore, score($0) != score($1) { return score($0) > score($1) }
            return $0.published > $1.published
        }
    }
    func score(_ paper: Paper) -> Int {
        if let topic { return paper.screenings[topic.id.uuidString]?.score ?? -1 }
        return paper.screenings.values.compactMap(\.score).max() ?? -1
    }
    var body: some View {
        NavigationSplitView {
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    Image(systemName: "books.vertical.fill").font(.title2).foregroundStyle(.teal)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("论文港").font(.title3.bold())
                        Text("PAPER HARBOR").font(.system(size: 9, weight: .semibold, design: .rounded)).tracking(2).foregroundStyle(.secondary)
                    }
                    Spacer()
                }.padding(20)
                List(selection: $section) {
                    Section("资料库") {
                        Label("全部论文", systemImage: "tray.full").badge(model.library.papers.count).tag("all")
                        Label("未读论文", systemImage: "circle.dotted").badge(model.library.papers.filter { !$0.read }.count).tag("unread")
                        Label("收藏论文", systemImage: "star").tag("starred")
                    }
                    Section {
                        ForEach(model.library.topics) { topic in
                            HStack {
                                Image(systemName: topic.enabled ? "dot.radiowaves.left.and.right" : "pause.circle")
                                    .foregroundStyle(topic.lastError == nil ? Color.teal : Color.orange)
                                Text(topic.name).lineLimit(1)
                            }.tag(topic.id.uuidString)
                                .contextMenu {
                                    Button("编辑主题") { topicEditor = topic }
                                    Button(topic.enabled ? "暂停订阅" : "恢复订阅") {
                                        model.mutate { value in
                                            if let i = value.topics.firstIndex(where: { $0.id == topic.id }) { value.topics[i].enabled.toggle() }
                                        }
                                    }
                                    Button("删除订阅", role: .destructive) { deleteTopic = topic }
                                }
                        }
                    } header: {
                        HStack {
                            Text("主题订阅")
                            Spacer()
                            Button { showNew = true } label: { Image(systemName: "plus") }.buttonStyle(.plain).help("添加主题")
                        }
                    }
                }.listStyle(.sidebar)
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    Label(model.backgroundStatus, systemImage: "clock.arrow.circlepath")
                        .font(.caption).foregroundStyle(.secondary)
                    SettingsLink { Label("设置与模型", systemImage: "gearshape") }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(16)
            }.navigationSplitViewColumnWidth(min: 210, ideal: 230, max: 280)
        } content: {
            VStack(spacing: 0) {
                listHeader
                Divider()
                if papers.isEmpty {
                    ContentUnavailableView {
                        Label(search.isEmpty ? "让论文主动找到你" : "没有匹配论文", systemImage: "doc.text.magnifyingglass")
                    } description: {
                        Text(search.isEmpty ? "添加研究主题，获取最新论文，\n用你选择的模型筛选值得精读的内容。" : "尝试其他标题、作者或摘要关键词。")
                    } actions: {
                        if search.isEmpty {
                            Button(model.library.topics.isEmpty ? "添加第一个主题" : "更新订阅") {
                                if model.library.topics.isEmpty { showNew = true }
                                else { model.perform { try await $0.run(force: true) } }
                            }.buttonStyle(.borderedProminent).tint(.teal).disabled(model.busy)
                        }
                    }.frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    List(selection: $selectedPaper) {
                        ForEach(papers) { paper in
                            PaperRow(paper: paper, score: score(paper), selected: selectedPaper == paper.id).tag(paper.id)
                        }
                    }.listStyle(.inset)
                }
                Divider()
                HStack(spacing: 8) {
                    if model.busy { ProgressView().controlSize(.small) }
                    Text(model.library.activity).lineLimit(2).font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if model.busy { Button("取消") { model.cancel() }.controlSize(.small) }
                }.padding(12)
            }
            .navigationSplitViewColumnWidth(min: 320, ideal: 400, max: 520)
        } detail: {
            if let paper = model.library.papers.first(where: { $0.id == selectedPaper }) {
                PaperDetail(paper: paper, preferredTopic: topic?.id)
            } else {
                ContentUnavailableView("留出时间，读重要的论文", systemImage: "text.book.closed", description: Text("从左侧选择论文，查看摘要、相关性评分和全文分析。"))
            }
        }
        .tint(.teal)
        .searchable(text: $search, prompt: "搜索本地论文")
        .toolbar {
            ToolbarItemGroup {
                Button { showNew = true } label: { Label("添加主题", systemImage: "plus") }
                Button { model.perform { try await $0.run(force: true) } } label: { Label("更新订阅", systemImage: "arrow.clockwise") }.disabled(model.busy || model.library.topics.isEmpty)
            }
        }
        .sheet(isPresented: $showNew) { TopicEditor(topic: Topic(name: "")).environmentObject(model) }
        .sheet(item: $topicEditor) { TopicEditor(topic: $0).environmentObject(model) }
        .sheet(item: $rangeTopic) { RangeSheet(topic: $0).environmentObject(model) }
        .alert("操作未完成", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("好") { model.error = nil }
        } message: { Text(model.error ?? "") }
        .confirmationDialog("删除这个订阅？论文和已有分析会保留。", isPresented: Binding(get: { deleteTopic != nil }, set: { if !$0 { deleteTopic = nil } })) {
            Button("删除订阅", role: .destructive) {
                if let id = deleteTopic?.id { model.mutate { $0.topics.removeAll { $0.id == id } }; section = "all" }
                deleteTopic = nil
            }
        }
        .onReceive(timer) { _ in model.reload(); model.updateBackgroundStatus(); model.checkForUpdatesIfDue() }
        .onAppear { model.checkForUpdatesIfDue() }
        .onChange(of: section) { _, _ in selectedPaper = nil }
    }
    private var listHeader: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(title).font(.title2.bold())
                Spacer()
                Text("\(papers.count) 篇").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            if let topic {
                Text([topic.categories, topic.keywords].filter { !$0.isEmpty }.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
                if let error = topic.lastError { Label(error, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange) }
                HStack {
                    Button("按日期抓取") { rangeTopic = topic }
                    Button("编辑") { topicEditor = topic }
                    Spacer()
                    if let sync = topic.lastSync { Text(sync, style: .relative).font(.caption).foregroundStyle(.secondary) }
                }.controlSize(.small)
            }
            if let pause = model.library.screeningPause {
                Label("自动筛选已暂停：" + pause, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
            }
            HStack {
                Picker("排序", selection: $sortByScore) {
                    Text("最新发表").tag(false)
                    Text("相关性优先").tag(true)
                }.labelsHidden().pickerStyle(.segmented)
                Button { model.perform { try await $0.screenPendingNow() } } label: { Image(systemName: "sparkles") }
                    .help("筛选待分析摘要").disabled(model.busy).accessibilityLabel("筛选待分析摘要")
            }
        }.padding(20)
    }
}

private struct PaperRow: View {
    let paper: Paper
    let score: Int
    let selected: Bool
    var secondary: Color { selected ? .white.opacity(0.85) : .secondary }
    var accent: Color { selected ? .white : .teal }
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Text(paper.categories.first ?? "arXiv").font(.caption.weight(.medium)).foregroundStyle(accent)
                Spacer()
                if paper.starred { Image(systemName: "star.fill").foregroundStyle(.orange).font(.caption) }
                Text(paper.published, format: .dateTime.month().day()).font(.caption).foregroundStyle(secondary)
            }
            Text(paper.title).font(.system(size: 14, weight: paper.read ? .regular : .semibold)).lineLimit(3)
            Text(paper.authors.prefix(3).joined(separator: ", ")).font(.caption).foregroundStyle(secondary).lineLimit(1)
            HStack {
                if score >= 0 { Text("相关性 \(score)").foregroundStyle(accent) }
                else if !paper.screeningErrors.isEmpty { Label("分析失败", systemImage: "exclamationmark.circle").foregroundStyle(.orange) }
                else { Text("待筛选").foregroundStyle(secondary) }
                Spacer()
                if paper.fullAnalysis != nil { Label("已精读", systemImage: "checkmark.seal").foregroundStyle(secondary) }
            }.font(.caption)
        }.padding(.vertical, 12)
    }
}
