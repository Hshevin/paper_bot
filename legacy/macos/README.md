# 论文港 · PaperHarbor

个人 macOS 原生 arXiv 论文助手。SwiftUI 三栏界面：订阅主题、论文列表、摘要与全文报告。支持 macOS 14+；此处打包产物面向本机 Apple Silicon。

## 开始使用

1. 当前已安装到 `~/Applications/PaperHarbor.app`，直接打开即可。构建产物也在 `dist/PaperHarbor.app`；后续安装建议使用 `bash scripts/install-local.sh`。不要从云同步/文件提供商目录长期运行后台任务。
2. 在「设置与模型」分别配置**摘要筛选**和**全文精读**：选择 OpenAI 兼容或 Anthropic 原生协议，填写 API 地址、模型 ID 和密钥。切换提供商会保留各自的配置；点击「保存设置」。
3. 添加主题。分类例：`cs.AI, cs.CL`；关键词例：`large language model, reasoning`。分类之间 OR、关键词短语之间 OR，两组之间 AND。至少填一项。主题名称用于相关性评分。
4. 「更新订阅」首次回看 7 天；选择左侧具体主题可「按日期抓取」历史论文。日期按本机时区整日输入，转换 UTC，匹配首次提交日期。
5. 有效配置下，新摘要会自动生成中文概述、相关性 0–100 分和理由。选择论文并点击「开始精读」才会下载 PDF、发送正文并生成报告。
6. 「设置与模型 → 更新与存储 → 启用后台更新」。如果系统要求批准，在系统登录项中允许。主应用退出后 agent 仍运行；移动或替换 app 前先停用，之后重新启用。

支持收藏、已读标记、本地搜索、相关性排序、原文 PDF 预览和 Markdown 导出。API 测试连接会发送一条简短模型请求，可能产生少量费用。

## API 地址示例

| 协议 | 基础地址示例 | 自动使用端点 |
|---|---|---|
| OpenAI 兼容 | `https://api.openai.com/v1` | `/chat/completions` |
| Anthropic 原生 | `https://api.anthropic.com/v1` | `/messages` |
| 本地兼容服务 | `http://localhost:11434/v1` | `/chat/completions` |

可填写完整端点，也可保留服务商代理前缀；空根路径会补 `/v1`，非空自定义路径原样保留。远程地址必须 HTTPS；本机 HTTP 可用。模型 ID 由服务商提供。OpenAI 兼容协议使用 Chat Completions 文本请求与 `max_tokens`，服务商需支持这些字段。不含工具调用。

## 自动更新与费用

- 系统每 5 分钟唤起 agent，再判断用户设定的更新间隔（15–1440 分钟）。打开主应用时也会检查到期任务。Mac 睡眠时不会被唤醒，唤醒、联网并登录后补抓。
- 自动抓取从上次成功时间往前重叠 7 天，减少索引延迟漏抓；失败不推进游标。长时间失败后重试保留已抓取论文并去重。
- 使用提交日期查询，因此不会持续追踪所有历史论文的新版本；被检索到的新版本会更新并重新筛选。
- 请求串行，跨进程共享至少 3.1 秒间隔。单次日期查询最多覆盖 arXiv 允许的前 30,000 条结果，超过会明确报错并保留已收集论文，需缩小范围。
- 每轮自动摘要默认最多 20 份；一篇命中多个主题分别计数。模型调用失败会暂停该配置的自动筛选，修改模型配置或点击筛选/重试后恢复，避免错误配置连续收费。
- 全文精读仅手动触发。PDF 上限 40 MB、300 页、24 万字符，分为至多 14 个片段并综合，最多约 15 次模型调用。扫描件不做 OCR，图表图像不输入模型；超过限制会报错，不会静默截断。
- 论文、分段笔记均视为资料；模型不执行工具。模型结论仍需与原文核对。取消时，已发出的请求仍可能被服务商计费。

## 本地数据

`~/Library/Application Support/PaperHarbor/`：

- `settings.json`：密钥及模型设置，**明文、权限 0600**，不使用系统钥匙串。
- `library.json`：论文、订阅、分析、成功游标与状态。
- `PDFs/`：手动精读时下载的 PDF。

目录权限 0700。设置与论文分开，`flock` 短事务和原子替换协调多进程保存，单独工作锁避免并行分析/抓取。损坏 JSON 会报错并保留文件，不会静默清空。Markdown 导出不含配置或密钥。可退出应用、停用后台后备份整个目录；包含密钥，请妥善保管。

## 开发

需要 Swift 6 工具链（当前仅 Command Line Tools 即可），无第三方 Swift 依赖。

```bash
swift build
swift run PaperCoreChecks       # 12 项核心行为检查
bash scripts/test.sh            # 核心 + 本机模拟 API/PDF 集成测试，需要 Python 3
bash scripts/build-app.sh       # Release .app，本地 ad-hoc 签名
bash scripts/install-local.sh   # 安装到 ~/Applications，先退出应用并停用后台
swift run PaperHarborAgent --smoke-fetch
swift run PaperHarborAgent --smoke-range
```

`swift test` 不是本项目的测试入口。此机器的 CLT 缺少完整 XCTest 运行环境；测试以可执行目标提供，断言或异常失败时返回非零退出码。模拟 API 仅监听 127.0.0.1，不访问付费服务。无需 Python 虚拟环境；若未来需要创建，使用 uv。

`PAPERHARBOR_DATA_DIR` 可为开发进程指定独立测试目录。`PaperHarborAgent --once` 立即执行已保存的订阅，可能调用所配置的模型；不要用真实密钥运行费用不受控的测试。

## 结构与验证

- `Sources/PaperCore`：模型、存储、arXiv、LLM、PDF、工作流。
- `Sources/PaperHarbor`：SwiftUI 界面、设置、后台注册。
- `Sources/PaperHarborAgent`：独立后台入口。
- `Tests/PaperCoreTests`：无需测试框架的可执行测试。
- `Resources` / `scripts`：打包与本机模拟服务。
- `docs/SPEC.md` / `docs/VALIDATION.md`：规格与验证记录。

本地产物已签名用于本机使用，尚未使用 Developer ID 签名或公证，不是可面向其他 Mac 直接分发的正式发行包。源码可在 Intel Mac 上编译，但尚未在该架构实测。没有提供真实 API 密钥，因此不宣称已验证某个付费模型的实际分析质量。
