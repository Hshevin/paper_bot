import SwiftUI
import PaperCore

struct TopicEditor: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) var dismiss
    @ViewState var topic: Topic
    @ViewState private var error: String?
    var existing: Bool { model.library.topics.contains { $0.id == topic.id } }
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text(existing ? "编辑主题" : "添加研究主题").font(.title2.bold())
            Text("选择学科范围，再用关键词聚焦你的研究方向。").foregroundStyle(.secondary)
            Form {
                TextField("主题名称", text: $topic.name, prompt: Text("例如：大语言模型"))
                TextField("arXiv 分类", text: $topic.categories, prompt: Text("cs.AI, cs.CL"))
                TextField("关键词", text: $topic.keywords, prompt: Text("large language model, reasoning"))
                Toggle("自动订阅更新", isOn: $topic.enabled)
            }.textFieldStyle(.roundedBorder)
            Text("分类之间为 OR，关键词短语之间为 OR；两组条件为 AND。多个值用英文逗号分隔，留空其中一项可扩大范围。首次更新回看 7 天。")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Text("常用分类").font(.caption)
                ForEach(["cs.AI", "cs.CL", "cs.LG", "stat.ML"], id: \.self) { category in
                    Button(category) {
                        let values = topic.categories.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                        if !values.contains(category) { topic.categories = (values.filter { !$0.isEmpty } + [category]).joined(separator: ", ") }
                    }.controlSize(.small)
                }
            }
            if let error { Text(error).foregroundStyle(.red).font(.caption) }
            HStack {
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("保存主题") { save() }.keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent).tint(.teal)
            }
        }.padding(28).frame(width: 550)
    }
    func save() {
        do {
            topic.name = topic.name.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !topic.name.isEmpty else { throw AppError("请填写主题名称。") }
            _ = try ArxivQuery.url(topic: topic, from: Date(), to: Date(), offset: 0)
            guard let store = model.store else { throw AppError("存储不可用。") }
            try store.update { value in
                if let i = value.topics.firstIndex(where: { $0.id == topic.id }) {
                    if value.topics[i].criteria != topic.criteria {
                        topic.lastSync = nil; topic.lastError = nil
                        // Old membership and scores no longer describe the edited query.
                        for p in value.papers.indices {
                            value.papers[p].topicIDs.removeAll { $0 == topic.id }
                            value.papers[p].screenings[topic.id.uuidString] = nil
                            value.papers[p].screeningErrors[topic.id.uuidString] = nil
                        }
                    } else {
                        topic.lastSync = value.topics[i].lastSync; topic.lastError = value.topics[i].lastError
                    }
                    value.topics[i] = topic
                } else { value.topics.append(topic) }
            }
            model.reload(); dismiss()
        } catch { self.error = error.localizedDescription }
    }
}

struct RangeSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) var dismiss
    let topic: Topic
    @ViewState private var start = Date().addingTimeInterval(-30 * 86400)
    @ViewState private var end = Date()
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("按日期收集论文").font(.title2.bold())
            Text(topic.name).foregroundStyle(.teal)
            DatePicker("开始日期", selection: $start, displayedComponents: .date)
            DatePicker("结束日期", selection: $end, in: ...Date(), displayedComponents: .date)
            Text("按本机时区的整日范围查询，发送时转换为 UTC。使用首次提交日期，重复论文会自动合并；已启用自动筛选时会分析本轮待筛选摘要。")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("开始抓取") {
                    let from = Calendar.current.startOfDay(for: start)
                    let nextDay = Calendar.current.date(byAdding: .day, value: 1, to: Calendar.current.startOfDay(for: end))!
                    let to = min(Date(), nextDay.addingTimeInterval(-1))
                    model.perform { try await $0.fetchRange(topic: topic, from: from, to: to) }
                    dismiss()
                }.buttonStyle(.borderedProminent).tint(.teal).keyboardShortcut(.defaultAction)
                    .disabled(Calendar.current.startOfDay(for: start) > Calendar.current.startOfDay(for: end) || model.busy)
            }
        }.padding(28).frame(width: 460)
    }
}
