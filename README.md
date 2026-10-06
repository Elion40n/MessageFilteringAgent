# MessageFilteringAgent

面向 Windows 本机运行的可配置消息筛选 Agent。

## 项目声明与文档

- 本项目参考了 GitHub 仓库 [simfeng/agents-from-scratch](https://github.com/simfeng/agents-from-scratch/tree/main)，并全程由 GitHub Copilot 完成。
- 本项目为非商用源码项目，采用 [PolyForm Noncommercial 1.0.0](LICENSE) 许可。
- B 站演示视频：[【AI coding Agent项目介绍】消息筛选助手_哔哩哔哩_bilibili](https://www.bilibili.com/video/BV1phpF6aETy/?vd_source=2a8db4ca73f063c55025e87a06cf38a4#reply316227257617)。
- 联系邮箱：[rento27182818@qq.com](mailto:rento27182818@qq.com)。

项目文档：

- [PLAN.md](PLAN.md)：项目目标、实现范围、技术决策、验证要求及暂缓事项。
- [PROGRESS.md](PROGRESS.md)：实现进度、已执行的验证、已知限制和后续工作。
- [ACTUAL_TEST_GUIDE.md](ACTUAL_TEST_GUIDE.md)：本地手动测试步骤、测试数据隔离要求和安全边界。

开始修改代码前，建议先让 AI 阅读 `README.md`、`PLAN.md`、`PROGRESS.md` 和 `ACTUAL_TEST_GUIDE.md`，了解项目目标、当前状态、验证边界与操作约束，再结合相关代码和测试开展修改。

## 当前实现

- 本地浏览器界面，仅监听 `127.0.0.1`。
- “手动输入”可添加多段聊天记录（每次最多 20 段），每段都可包含多条目标信息和杂讯；模型分别拆分成有序片段（拆分阶段保留所有内容），再核验片段是否覆盖原文，最后逐片段进入正式工作流。若核验发现漏字或改写，该段停止处理。满足项按当前输出设置发送邮件，不满足项（包括无关杂讯）进入“过滤信息”，不确定项进入“待确认”；逐条展示所属记录、片段、状态和理由。
- 聊天记录拆分会额外调用一次模型，随后每个片段单独分类；模型费用随片段数增加。
- 正式工作流中判为“不满足”的消息按 profile 归档 30 天（可在设置中调整），可在“过滤信息”页查看原文、日期、来源、处理 profile 和理由，并手动转入“待确认”重新处理；转入时立即从过滤列表删除，后续无论处理结果如何都不恢复该归档项，也不会自动投递。
- 网页打开期间每 3 秒检查当前 profile 的活动摘要，自动更新待确认和过滤信息徽标；若正在查看相应列表，列表变化时也会自动刷新。
- 待确认事项由用户选择满足/不满足后直接结案，不进行二次分类；填写的理由只保存为 profile 记忆，供后续分类参考。处理保存成功后，待确认卡片立即从界面移除。
- 分类按整条消息理解筛选条件；“或”条件任一命中即可，多岗位/多地点列表不要求逐项配对，除非筛选条件明确要求岗位与地点对应。缺少明确要求的对应关系时应判“不确定”，而非“不满足”。
- 当前只处理文本消息，不能直接识别图片或截图；图片识别/OCR 支持列为低优先级待办。
- 每个 JSON 配置文件启动一个独立 Agent；待确认问题、来源游标、去重状态和记忆按 profile 隔离。
- 新建默认配置首次保存时绑定“筛选配置名称”，并将 `settings.json` 重命名为 `<名称>.json`；保持默认名称会生成 `default.json`。保存后名称锁定，身份或配置文件路径变化需要重启。已有 JSON 缺少绑定标记时按已初始化处理，不提供旧配置改名兼容。
- 规则与连接、记忆管理位于各自独立的标签页；记忆可新增、编辑、启停和删除，人工处理理由会保存为记忆，供后续分类提供补充上下文。
- 按来源消息 ID 去重，并跨来源按规范化正文精确去重；默认保留 30 天，可调整。
- QQ 邮箱 IMAP UID/UIDVALIDITY 读取、SMTP 通知代码。
- QQ 邮件正文优先读取纯文本；纯文本缺失时解析 HTML 可见文字和链接，不处理图片/二进制附件 OCR。
- 当前版本暂不采用微信自动输入；SIWX JSON 解析、本机 API 客户端、同步/导入与清理代码保留为未来接入占位，不属于当前支持功能。
- Agent 不连接微信桌面客户端，也不负责解密微信数据库。旧配置中的 SIWX 输入模式会回退到手动模式；手动模式不会运行 SIWX 导入或清理。
- Windows 服务运行期间请求阻止系统睡眠，允许屏幕关闭。

用户确认已完成真实 QQ 邮箱端到端验证。自动化测试仍使用模拟响应和 fixture；SIWX 测试只覆盖保留的占位代码，不代表当前支持真实微信输入。Windows 系统凭据库实际读写及防睡眠请求状态未做环境验证，不应据此推断这两项在目标机器上已验证。QQ Mail UIDVALIDITY 改变时最多重扫最近 2,000 封；这不保证超出窗口的历史邮件均被补回。

## 安装与启动

### 普通用户：使用预构建便携版

启动器连续 30 分钟无操作会自动关闭；点击、输入等操作会重置计时，已启动的 Agent 不受影响。Agent 网页上的“关闭 Agent”操作成功后会关闭网页；若浏览器不允许关闭标签页，则会切换到空白页。

普通用户无需安装 Python、创建虚拟环境或自行构建启动器。项目仓库会一并提供预构建便携版 ZIP，文件位于 `dist/MessageFilteringAgent-portable.zip`；也可从 GitHub 发布页下载（发布页链接待补充）。下载并解压后运行 `MessageFilteringAgent\MessageFilteringAgent.exe`。启动器用于选择并启动已有 profile，或创建新的 profile；启动后按页面显示的本地地址访问 Agent。设置和 SQLite 数据默认位于 `%LOCALAPPDATA%\MessageFilteringAgent`，不保存在程序目录内。

**安全提示：**此便携版 EXE 未进行 Authenticode 代码签名。2026-10-06 的 VirusTotal 检查中，ZIP 有 4/64 个引擎、EXE 有 7/71 个引擎标记告警，包括 Microsoft `Trojan:Win32/Wacatac.B!ml`；[ZIP 报告](https://www.virustotal.com/gui/file/571bce465c6c8c63fdd2fe1da6ca2a119fb5139677108689e3377728d71015b1)和 [EXE 报告](https://www.virustotal.com/gui/file/df962ec79a0b770fdf367f4c7f346104dd38779d37ecb25ddec6723789777a12)。本机 Windows Defender 未检出威胁；这些结果既不能证明文件恶意，也不能证明是误报。若安全软件拦截，请勿关闭防护或添加排除项；可按下方“开发者：从源码运行或构建”自行构建。自行构建也不保证消除安全软件告警。

### 开发者：从源码运行或构建

以下步骤仅供开发、调试或自行构建使用；普通用户不需要执行。若便携版被安全软件拦截，可按这些步骤从源码构建，但自建版本仍可能被检测。

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e .
```

从源码启动 profile 管理器：

```powershell
.\.venv\Scripts\message-filtering-launcher.exe
.\.venv\Scripts\python -m message_filtering_agent.launcher
```

自行构建便携版：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[packaging]"
.\.venv\Scripts\python.exe .\scripts\build_portable.py
```

构建产物为 `dist/MessageFilteringAgent-portable.zip`；发布给普通用户的预构建便携版即为此格式。

直接启动 Agent（开发与诊断）：

也可跳过启动器，直接运行单个 Agent：

```powershell
.venv\Scripts\python -m message_filtering_agent.app
```

浏览器打开 `http://127.0.0.1:8765`，按 `Ctrl+C` 关闭服务。可覆盖端口和本地数据目录：

```powershell
.venv\Scripts\python -m message_filtering_agent.app --port 8766 --data-dir D:\MFAData
```

也可使用不同配置文件启动不同 Agent。默认 `settings.json` 首次保存时会按“筛选配置名称”命名 JSON 文件；若保留 `default`，则生成 `default.json`。已有 JSON 缺少绑定标记时视为已初始化，不会再获得首次改名机会。其他首次使用且尚不存在的 `--config` 路径会按路径文件名确定 profile。保存后若文件重命名，Agent 会关闭；多配置目录请使用对应的 `--config` 路径。同目录配置默认共享 SQLite 文件，但记忆、待确认事项和来源游标按 profile 隔离。若首选端口已占用，程序会在其后寻找可用端口。

```powershell
.venv\Scripts\python -m message_filtering_agent.app --config "$env:LOCALAPPDATA\MessageFilteringAgent\profiles\recruitment.json"
.venv\Scripts\python -m message_filtering_agent.app --config "$env:LOCALAPPDATA\MessageFilteringAgent\profiles\finance.json"
```

模型 API key 按规范化后的模型 URL 隔离，同一 URL 的 profile 共用；QQ 邮箱授权码按邮箱账号隔离。旧版全局模型 key 只迁移给默认 URL；若旧 profile 使用自定义 URL，首次启动时需为该 URL 单独输入 API key。Windows 系统凭据库的实际读写和系统防睡眠状态未做环境验证。首次使用前，在界面设置筛选条件、模型、邮箱账号及通知地址，并保存对应密钥。启用 IMAP 收件后才会连接邮箱；SMTP 仅在分类结果为“满足”时发送。

## 输入来源

- **手动输入**：每个输入框粘贴一段完整聊天记录，可添加多个记录框（最多 20 段）；每段内部仍可含多条目标信息。模型先拆分消息边界且不丢弃闲聊，再对所有片段正式分类。无关杂讯会在分类阶段判为不满足并归档；满足项可能立即发送邮件，请先配置测试收件地址。
- **QQ 邮箱**：填写 IMAP 参数、启用收件并保存授权码。每条邮件在完成分类、过滤或将待确认状态落库后才推进 UID 检查点。
- **微信输入（未来预留）**：当前版本暂不采用 SIWX 或其他微信自动输入方式。相关 SIWX 解析器和客户端代码仅作未来接入占位，设置页不可启用，也不应按真实微信来源进行测试。

## 测试

```powershell
python -m unittest discover -s tests -v
```

自动测试不使用真实账号。需要验证真实服务时，应使用用户指定的测试账号并先获得明确同意。

真实手动测试步骤、测试数据隔离和各输入渠道的注意事项见 [ACTUAL_TEST_GUIDE.md](ACTUAL_TEST_GUIDE.md)。

## 贡献与安全

- 贡献请通过 fork 和 PR 提交；提交前运行上方测试命令。请确保你有权提交这些内容，并同意贡献按 [LICENSE](LICENSE) 提供。
- 安全问题请通过[联系邮箱](mailto:rento27182818@qq.com)私下报告；请勿在公开 issue、PR 或讨论中披露漏洞细节、凭据或可利用样例。

## 中断后续接

继续实施前阅读 `PLAN.md` 和 `PROGRESS.md`，再检查当前文件与测试状态。每个阶段及暂停前更新 `PROGRESS.md`，记录具体改动、执行过的命令、结果、限制和下一步。不要将凭据写入交接文件。