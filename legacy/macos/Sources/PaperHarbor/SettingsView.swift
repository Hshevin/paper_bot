import SwiftUI
import Combine
import ServiceManagement
import PaperCore

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @ViewState private var settings = Settings()
    @ViewState private var message = ""
    @ViewState private var loaded = false
    private let timer = Timer.publish(every: 3, on: .main, in: .common).autoconnect()
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 6) {
                    Text("设置与模型").font(.title2.bold())
                    Text("让轻量模型筛选，让擅长推理的模型精读。").foregroundStyle(.secondary)
                }
                Spacer()
            }.padding(24)
            TabView {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        ModelEditor(title: "摘要筛选模型", config: $settings.screening, profiles: $settings.screeningProfiles)
                        Divider()
                        ModelEditor(title: "全文精读模型", config: $settings.reading, profiles: $settings.readingProfiles)
                        Label("密钥仅保存在本机配置文件中，不使用系统钥匙串。", systemImage: "externaldrive")
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(24)
                }.tabItem { Label("模型 API", systemImage: "sparkles") }
                Form {
                    Section("自动更新") {
                        Stepper("检查间隔：\(settings.intervalMinutes) 分钟", value: $settings.intervalMinutes, in: 15...1440, step: 15)
                        Toggle("抓取后自动筛选摘要", isOn: $settings.automaticScreening)
                        Stepper("每轮最多分析 \(settings.screeningLimit) 份摘要", value: $settings.screeningLimit, in: 1...200)
                        Text("同一论文命中多个主题时，会分别评分并计入上限。失败的分析需要手动重试，全文精读始终由你触发。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Section("退出应用后继续更新") {
                        LabeledContent("系统后台项目", value: model.backgroundStatus)
                        HStack {
                            Button("启用后台更新") { model.setBackground(true) }
                            Button("停用") { model.setBackground(false) }
                            Button("打开系统登录项设置") { SMAppService.openSystemSettingsLoginItems() }
                        }
                        Text("请先将应用放到固定位置（建议“应用程序”文件夹），再启用。系统每 5 分钟检查是否到期；Mac 睡眠时不会被唤醒，恢复后补抓。未登录用户时不运行。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Section("本机数据") {
                        Button("在 Finder 中查看数据目录") {
                            if let root = model.store?.root { NSWorkspace.shared.open(root) }
                        }
                        Text("论文、PDF 和分析保存在本机。settings.json 含明文密钥，请勿分享。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }.formStyle(.grouped).tabItem { Label("更新与存储", systemImage: "clock") }
            }
            Divider()
            HStack {
                Text(message).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                Spacer()
                Button("保存设置") { save() }.buttonStyle(.borderedProminent).tint(.teal).keyboardShortcut("s").disabled(!loaded)
            }.padding(18)
        }.frame(width: 680, height: 760)
        .onAppear {
            do {
                guard let store = model.store else { throw AppError("本地存储不可用。") }
                settings = try store.settings(); loaded = true
            } catch { message = error.localizedDescription }
            model.updateBackgroundStatus()
        }
        .onReceive(timer) { _ in model.updateBackgroundStatus() }
        .alert("设置操作失败", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("好") { model.error = nil }
        } message: { Text(model.error ?? "") }
    }
    func save() {
        do {
            for config in [settings.screening, settings.reading] { _ = try LLMClient.endpoint(config) }
            settings.screeningProfiles[settings.screening.provider.rawValue] = settings.screening
            settings.readingProfiles[settings.reading.provider.rawValue] = settings.reading
            try model.store?.saveSettings(settings)
            message = "已保存，新任务将使用此配置。"
        } catch { message = error.localizedDescription }
    }
}

private struct ModelEditor: View {
    let title: String
    @Binding var config: ModelConfig
    @Binding var profiles: [String: ModelConfig]
    @ViewState private var showKey = false
    @ViewState private var testing = false
    @ViewState private var result = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(title).font(.headline)
                Spacer()
                Button(testing ? "测试中…" : "测试连接") { test() }.disabled(testing || !config.isConfigured)
            }
            Form {
                Picker("提供商协议", selection: Binding(get: { config.provider }, set: switchProvider)) {
                    ForEach(Provider.allCases) { Text($0.label).tag($0) }
                }
                TextField("API 地址", text: $config.baseURL, prompt: Text("https://your-service.example/v1"))
                TextField("模型名称", text: $config.model, prompt: Text("填写服务商提供的模型 ID"))
                HStack {
                    if showKey { TextField("API 密钥", text: $config.apiKey) }
                    else { SecureField("API 密钥", text: $config.apiKey) }
                    Button { showKey.toggle() } label: { Image(systemName: showKey ? "eye.slash" : "eye") }
                        .buttonStyle(.plain).help(showKey ? "隐藏密钥" : "显示密钥").accessibilityLabel("切换密钥可见性")
                }
            }.textFieldStyle(.roundedBorder)
            Text("支持带 /v1 的基础地址或完整端点；自定义代理路径原样保留。测试连接会发送一条简短请求，可能产生少量 API 费用。")
                .font(.caption).foregroundStyle(.secondary)
            if !result.isEmpty { Text(result).font(.caption).foregroundStyle(.secondary) }
        }
    }
    func switchProvider(_ provider: Provider) {
        profiles[config.provider.rawValue] = config
        if let saved = profiles[provider.rawValue] { config = saved }
        else {
            var fresh = ModelConfig(); fresh.provider = provider
            fresh.baseURL = provider == .anthropic ? "https://api.anthropic.com/v1" : "https://api.openai.com/v1"
            config = fresh
        }
        result = ""
    }
    func test() {
        testing = true; result = ""
        let snapshot = config
        Task {
            defer { testing = false }
            do {
                _ = try await LLMClient.complete(config: snapshot, system: "You are a connectivity test. Reply with OK only.", user: "Test")
                result = "连接成功，模型返回了有效响应。"
            } catch { result = error.localizedDescription }
        }
    }
}
