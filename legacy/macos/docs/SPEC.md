# PaperHarbor 首版规格

已确认：个人 macOS 原生论文助手；分类/关键词与时间范围检索、多个订阅、退出主程序后的自动抓取、自动摘要筛选、手动 PDF 精读。两个分析阶段分别选择 OpenAI 兼容或 Anthropic 原生协议、自定义地址/模型/密钥。密钥明文保存在本机，文件权限 0600。不包含同步、团队协作、自动全文分析。

## 实现与验收
- macOS 14+，SwiftUI 三栏：订阅 / 论文 / 详情，原生设置、空状态、错误提示。
- Foundation URLSession 调用 arXiv Atom API，分页、串行限速、UTC 提交日期过滤、版本去重。
- 首次订阅回看 7 天；后续使用成功游标并重叠 7 天应对索引延迟。抓取失败不推进游标，已收集论文保存并去重。
- 短事务 flock + 原子 JSON 写入协调应用/agent；单独工作锁防止同时抓取或重复收费。
- SMAppService 注册内嵌 LaunchAgent；每 5 分钟唤起，按设置的间隔判断是否到期。睡眠时不唤醒机器，恢复后补抓。后台状态来自系统实际状态。
- 自动摘要分析每轮有上限；失败保留并要求手动重试。摘要与精读记录模型、时间、依据。全文通过 PDFKit 提取，长文分块后综合，空/扫描 PDF 给出错误。
- 仅 HTTPS，允许 localhost HTTP；API 请求禁用重定向防止密钥发往其他地址。错误仅显示状态码，不回显服务商响应正文。

## 目录与命令
`Sources/PaperCore`：数据/持久化/API/工作流；`Sources/PaperHarbor`：UI；`Sources/PaperHarborAgent`：后台入口；`Tests/PaperCoreTests`：可直接运行的行为测试；`Resources`：app/agent plist；`scripts`：打包。

`swift run PaperCoreChecks`；`swift build`；`bash scripts/build-app.sh`。

## 风格与验证
四空格，数据 Codable/Sendable，外部边界抛错，例如 `func fetch(_ topic: Topic, from: Date, to: Date) async throws -> [Paper]`。
测试日期查询、Atom 格式/错误、版本去重、保留分析、多进程存储、两类协议和不合法模型响应。真实 arXiv 检索和原生 UI 冒烟测试；没有用户 API Key 时不宣称真实付费 LLM 验证。

始终验证输入和保留错误；新增产品范围先确认；禁止提交密钥、覆盖损坏数据库、自动精读。

## 实施顺序
1. 数据模型、Atom 解析与查询、存储及测试。
2. LLM 协议、PDF 分块和后台工作流及测试。
3. SwiftUI 界面、设置、LaunchAgent 和可运行 app。
4. 构建、真实请求、UI 验证、修复及交付说明。

## 参考
- https://info.arxiv.org/help/api/user-manual.html
- https://developer.apple.com/documentation/servicemanagement/smappservice
- https://platform.claude.com/docs/en/api/messages/create
- https://platform.openai.com/docs/api-reference/chat/create
