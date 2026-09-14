# 四家期货公司每日观点 Pipeline

本项目按四家公司分别采集/检索，再统一分类、校验、评分并生成 Excel：

```text
广发期货官网 -> 综合 -> 广发期货日评 -> PNG超级汇总 -> 表格结构化OCR ┐
中信期货官网 -> 公开 reportsQuery API -> rptSummary -------┤
国泰君安期货官网 -> 当日公开标题（全文在小程序）------------┤
东证期货官网 -> WordPress API -> 网页摘要+PDF各品种展望摘录 ┤
                                                          ↓
                    广发/中信/东证精简 Prompt -> TokenHub API
                       国泰公开标题 -> Python确定性分类
                                                          ↓
                         四份同构 JSON -> Python 校验/评分
                                                          ↓
               日期文件夹 + 总观点库 + 当前快照 + 汇总 Excel
```

## 一、最终结果

每次运行在同一个日期目录中生成：

```text
output/20260825/
  gfqh.json
  citicsf.json
  gtjaqh.json
  dzqh.json
  current_view_20260825.json
  summary_20260825.json
  期货公司观点汇总_20260825.xlsx
```

项目同时维护 `data/view_database.sqlite3`。`view_history` 按记录唯一键去重追加所有已校验的日度观点，
`current_company_views` 保存每家机构各品种的最新方向，`current_view_fields` 按品种分别滚动更新
供给、需求、库存、核心逻辑和中期展望。当天未出现的字段沿用旧值，出现新内容时只替换对应字段。
每个当前字段同时保留来源机构、观点日期、标题和 URL，避免把历史沿用误认为当天新观点。

四家公司 JSON 结构一致。每条观点含 `view` 和 Python 固定生成的 `score`：

```text
强烈看多 =  2
看多     =  1
中性     =  0
看空     = -1
强烈看空 = -2
```

方向性修饰优先于“震荡”一词：`震荡偏多/偏强/回升/上行` 归为看多，
`震荡偏空/偏弱/下行/趋弱` 归为看空；只有不带方向修饰的纯区间或纯震荡归为中性。
“仍有支撑但上方空间（或高度）受限”按震荡偏多处理；对称表述按震荡偏空处理。

JSON 只保存目标日新观点，不把历史观点混入当天记录。历史沿用仅在 Excel 汇总阶段发生，黄色单元格批注会注明原观点日期。

每天的 Excel 始终使用固定文件名 `期货公司观点汇总_YYYYMMDD.xlsx`，不会生成“更新版”副本。
如果重跑时该文件正在 Excel 中打开，总入口会在采集和付费 API 调用之前停止，并提示先关闭文件，避免跑完后才发现无法覆盖。

## 二、首次安装（Conda）

在 Anaconda Prompt 或已经初始化 Conda 的 PowerShell 中：

```powershell
cd "F:\广发固收\期货公司观点\futures_view_pipeline_v0_1"
Set-ExecutionPolicy -Scope Process Bypass
.\first_setup.ps1
```

脚本会创建/更新 `futures-view-system` 环境、安装项目依赖和 Playwright Chromium。

## 三、每日总入口

默认使用 Asia/Shanghai 当日，并通过四家公司官网采集资料。广发、中信和东证使用腾讯 TokenHub API 分类；国泰海通公开标题由 Python 确定性规则本地分类，不消耗模型 Token：

```powershell
.\run_all.ps1
```

首次使用时脚本会安全提示输入 API Key，输入内容不会显示，并保存到已被 Git 忽略的 `secrets/tokenhub_api_key.txt`。后续无需重复输入；环境变量 `TENCENT_TOKENHUB_API_KEY` 仍具有更高优先级。也可以显式写出 API 参数：

```powershell
.\run_all.ps1 -Date 2026-08-26 -LlmBackend api
```

API 首选使用 `hy3`。普通分类的接入地域应按 TokenHub 服务和 API Key 的实际开通地域选择，不能仅凭当前电脑位置自动判断：

| 服务开通地域 | `config.yaml -> llm_api.base_url` | 资源范围 |
|---|---|---|
| 广州 | `https://tokenhub.tencentmaas.com/v1` | 中国大陆 |
| 新加坡 | `https://tokenhub-intl.tencentmaas.com/v1` | 全球 |

一般在中国大陆使用可优先选择广州，面向全球可选择新加坡；最终必须以 API Key 所属站点为准，广州和新加坡 Key 不互通。项目当前配置使用广州入口做普通分类，与广州“启用管理”页面显示的免费额度保持一致。腾讯官方说明见 [TokenHub API 使用说明](https://cloud.tencent.com/document/product/1823/130078)。

TokenHub 模型调用顺序由 `llm_api.model` 和 `llm_api.model_fallbacks` 配置，当前为 `hy3` → `deepseek-v4-flash` → `deepseek-v4-pro`，同时适用于普通分类和东证联网搜索兜底。只有接口返回 HTTP 402（免费额度耗尽、且未开通后付费等额度类错误）时才自动切换到下一个模型；429 限流、网络故障或返回格式异常仍按原错误处理，避免误判。某个备用模型成功后，本次运行的后续任务会优先继续使用它，并在结果文件中记录实际模型和此前失败的尝试。额度规则见 [在线推理服务说明](https://cloud.tencent.com/document/product/1823/130087)。

东证现在和另外三家公司一样，主流程直接从官网 WordPress API 定位当天晨报。程序保留网页简短摘要，并在本地从官方 PDF 逐品种提取“总结与展望”；新闻全文、行情明细和免责声明不会送入模型，避免浪费 Token。只有官网 API、PDF 下载或摘要提取失败时，才启用 TokenHub 联网搜索作为兜底。若需要配置这项可选兜底，可在广州站创建 Key、开通联网搜索资源包，然后运行：

```powershell
.\save_api_key.ps1 -Search
```

该 Key 保存到 Git 已忽略的 `secrets/tokenhub_search_api_key.txt`，但正常官网采集成功时不会读取或要求它。模型、两个地址和搜索版本位于 `config.yaml -> llm_api`。普通 Key 可由环境变量 `TENCENT_TOKENHUB_API_KEY` 或本地忽略文件提供，搜索兜底 Key 对应 `TENCENT_TOKENHUB_SEARCH_API_KEY`。禁止把 Key 写入 `config.yaml` 或提交到 Git。联网搜索的广州地域限制见 [官方联网搜索文档](https://cloud.tencent.com/document/product/1823/132358)。

东证正常流程与其他公司相同：本地标准化官网摘要和 PDF 各品种“总结与展望”，再关闭联网按严格 JSON Schema 分类；最终记录的标题、URL 和来源类型均回填自官网文档。仅当官网链路失败时，才退回原“两阶段”联网发现与分类，并保存、检查 API 返回的 `search_results`。

所有爬虫公司记录的 URL 均由 Python 按来源标题用标准化文档中的官网 URL 回填，不采用模型生成的链接；`https://`、只有域名或无法匹配具体文章的地址会被拒绝。中信正文还会生成“品种：观点”覆盖清单，首轮遗漏时只对遗漏项进行短复核并合并去重；API 日期与标题中的显式 `YYYYMMDD` 必须同时匹配目标日，防止次日报告提前混入。模型返回还会经过本地必填字段和枚举校验，异常空响应会自动重试。

东证官方 PDF 在采集阶段会校验文件格式、正文长度、报告日期和东证署名；联网兜底的公开转载仍必须实际打开网页核验署名。国泰标题完全由 Python 对明确方向短语进行保守分类，不调用模型；“关注仓单/利润”等无方向标题仍不输出。

指定日期：

```powershell
.\run_all.ps1 -Date 2026-08-26
```

官网调试时才需要增加 `-Headed -Debug`。总入口依次运行：

1. 四家公司官网采集；
2. 标准化并生成三份精简模型 Prompt 及一份国泰本地任务；
3. 用 TokenHub API 处理广发、中信和东证，同时用 Python 处理国泰；
4. 校验四份 JSON，生成评分矩阵和 Excel。

处理阶段按 `manifest.json` 依次执行。广发只使用官网“交易品种=综合”下标题严格匹配 `广发期货日评YYYYMMDD` 的详情页，只下载其中 PNG 超级汇总图。Python 会检测表格横线，把“品种、合约、点评、操作建议”按行还原，并通过品种及合约代码固定板块；送给模型时只保留计分行的板块、品种、点评和建议，不再重复发送行号、OCR 原词、合约和内部状态。PDF、周报汇总和“广发期货期现日报汇总”均不进入主流程。

中信 Prompt 只保留来源日期、标题、URL 和正文，来源类型与名称作为公共默认值保存一次；覆盖清单不再重复整套来源元数据。若同一篇报告由较长英文版开头、完整中文版结尾组成，程序会在满足中英文长度和完整性门槛后只发送中文版。

广发日评通常只在周二至周五发布。目标日期没有日评时，官网状态为 `no_reports`，广发当天不调用模型 API、不伪造中性观点；最终 Excel 按既有规则沿用最近一次可用观点。

终端会显示官网采集、PNG 下载、OCR、API/本地分类、联网搜索、最终汇总和质量门禁进度。模型公司完成时会显示输入、输出、缓存和联网搜索调用次数；国泰会显示 `Python本地分类` 和零 Token。任何阶段失败都会返回非零退出码并给出具体原因。

## 四、逐家公司调试

公司键：

```text
gfqh    广发期货
citicsf 中信期货
gtjaqh  国泰海通期货（官网主体仍显示国泰君安期货）
dzqh    东证期货（官网 WordPress API + 官方 PDF，联网搜索仅兜底）
```

例：只调广发爬虫：

```powershell
.\run_company.ps1 -Company gfqh -Stage crawl -Date 2026-08-25 -Headed -BrowserDebug
```

只重新生成某公司的 Prompt：

```powershell
.\run_company.ps1 -Company gfqh -Stage prepare -Date 2026-08-25
```

只让 API 重新处理东证：

```powershell
.\run_company.ps1 -Company dzqh -Stage llm -Date 2026-08-25
```

只处理国泰海通（纯 Python，不需要 API Key）：

```powershell
.\run_company.ps1 -Company gtjaqh -Stage llm -Date 2026-08-26 -LlmBackend api
```

仅重新汇总已有四份 LLM JSON：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --stage finalize --date 2026-08-25
```

## 五、中间文件

日期参数使用 `YYYY-MM-DD`，日期文件夹使用 `YYYYMMDD`。例如 `2026-08-26` 对应目录 `20260826`。

| 目录/文件 | 作用 | 建议检查内容 |
|---|---|---|
| `data/raw/YYYYMMDD/gfqh.json` | 广发官网原始结果 | 日评日期、详情URL及逐行结构化的品种/合约/点评/建议 |
| `data/raw/YYYYMMDD/gfqh_ocr_audit.json` | 广发 OCR 质量报告 | 表格总行、计分行、非计分品种、不完整行及每行识别结果 |
| `data/raw/YYYYMMDD/citicsf.json` | 中信官网原始结果 | 日期、标题、摘要、作者、详情URL |
| `data/raw/YYYYMMDD/gtjaqh.json` | 国泰官网标题结果 | 日期、标题、详情URL；正文按设计只有标题 |
| `data/raw/YYYYMMDD/dzqh.json` | 东证官网原始结果 | 网页摘要、官方PDF各品种展望摘录、详情URL和附件URL |
| `data/raw/YYYYMMDD/all_raw.json` | 四家公司原始数据合并 | `crawl_status` 和全部 `items` |
| `data/raw/YYYYMMDD/images/gfqh/` | 广发日评原始PNG | 可人工打开并与OCR正文抽查 |
| `data/raw/YYYYMMDD/debug/` | 官网采集调试材料 | 网页结构变化或采集失败时检查 |
| `data/normalized/YYYYMMDD/documents.json` | 统一文档协议 | 只保留状态正常、可交给LLM的文档 |
| `data/llm_input/YYYYMMDD/prompts/` | 最终Prompt | 广发通常为一份较短的 OCR 文本 Prompt |
| `data/llm_input/YYYYMMDD/prompts/manifest.json` | Prompt清单 | 公司、来源数量和Prompt路径 |
| `data/llm_output/YYYYMMDD/*.json` | API原始分类结果及调用审计 | `processing_status`、`api.calls`、原始回复和拒绝原因 |
| `data/llm_output/YYYYMMDD/api_run_summary.json` | API运行摘要 | 快速查看公司状态、记录数和Token用量 |
| `data/llm_output/YYYYMMDD/error_*_api.json` | API错误详情 | 仅对应公司调用失败时生成 |
| `output/YYYYMMDD/*.json` | Python校验、去重、评分后的JSON | 最终 `records` 和 `score` |
| `output/YYYYMMDD/current_view_YYYYMMDD.json` | 截至目标日的品种级当前观点快照 | 字段级沿用、方向、更新时间、数据年龄和来源 |
| `output/YYYYMMDD/期货公司观点汇总_YYYYMMDD.xlsx` | 最终Excel | 汇总矩阵、当日明细、数据质量 |
| `output/YYYYMMDD/quality_report_YYYYMMDD.json` | 自动质量门禁报告 | 是否通过、重复/非法条数、Excel一致性和明确观点遗漏 |

项目根目录和程序模块：

| 文件 | 作用 |
|---|---|
| `config.yaml` | 官网地址、TokenHub地域/模型/搜索参数及兼容配置 |
| `first_setup.ps1` | 创建/更新Conda环境并安装浏览器 |
| `run_all.ps1` | 默认当日或指定日期的完整流程入口 |
| `run_company.ps1` | 单公司、单阶段调试入口 |
| `save_api_key.ps1` | 安全录入普通分类地域 Key 或广州联网搜索 Key |
| `prompts/classifier_prompt.txt` | 四家公司共用的官网文档分类规则 |
| `prompts/dzqh_search_prompt.txt` | 东证官网失败时的公开信息检索兜底规则 |
| `src/futures_view/pipeline.py` | 四家官网采集调度 |
| `src/futures_view/scrapers/dzqh.py` | 东证 WordPress API、官方PDF下载与正文提取 |
| `src/futures_view/normalize.py` | 原始数据标准化 |
| `src/futures_view/build_prompts.py` | 生成四家公司Prompt |
| `src/futures_view/ocr.py` | 广发日评PNG的表格定位、逐单元格OCR与质量信息 |
| `src/futures_view/commodities.py` | 品种规范名、合约纠错及固定板块映射 |
| `src/futures_view/reprocess_gfqh.py` | 不重新下载，使用已有PNG重做广发表格OCR |
| `src/futures_view/llm/browser_llm.py` | 兼容保留的网页后端与通用JSON提取器；日常运行不需要登录 |
| `src/futures_view/llm/api_llm.py` | TokenHub API、严格JSON Schema、用量记录和东证搜索兜底 |
| `src/futures_view/title_rules.py` | 震荡偏多/偏空、操作建议和标题方向的确定性规则 |
| `src/futures_view/source_validation.py` | URL修复、证据强度和东证真实网页署名核验 |
| `src/futures_view/finalize.py` | 校验、评分、历史沿用和Excel生成 |
| `src/futures_view/view_database.py` | SQLite历史库、字段级更新和当前观点快照 |
| `src/futures_view/inspect_run.py` | 指定日期运行检查；完整流程会自动以严格模式执行 |
| `tests/test_core.py` | 品种映射、方向判断、URL、OCR、去重和质量门禁回归测试 |

### 如何判断一次运行是否完整成功

`[DONE] gfqh: 1` 只表示广发官网采集得到1条，不代表分类和Excel均已成功。完整入口会在生成 Excel 后自动执行严格质量门禁；也可单独执行：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.inspect_run --date 2026-08-26 --strict
```

检查顺序：

1. 官网采集 `status` 应为 `ok`，日期异常和URL异常应为0。
2. 广发正文不应为空；国泰正文只有标题是正常设计。
3. 四家公司状态应以 `ok` 开头，例如 `ok_api`、`ok_api_revalidated` 或 `ok_manual_import`。
4. 最终应显示 Excel 存在，JSON 与 Excel 行数一致，明确规则覆盖遗漏为0，结论为“通过自动质量门禁”。
5. 打开Excel的 `运行与数据质量` 工作表，再确认状态和有效观点数。

只有最后出现 `完整性结论: 通过自动质量门禁`，整次运行才算成功。若终端最后出现
`[阶段失败] futures_view.inspect_run`，前面的 Excel 只是待检查产物，不应直接使用；根据其后列出的未映射品种、重复、URL、日期或覆盖遗漏修复后再重跑。

一次已验证的正常输出示例（2026-09-11）：广发、中信、国泰和东证官网分别采集1、22、58、1条，分类输出分别为40、21、29、18条，最终 JSON 和 Excel 明细均为108条，重复、非法和明确规则遗漏均为0。

自动分类有三层防护：API 严格 Schema 只允许统一品种名；广发结构化表格、中信明确小标题和国泰标题均由 Python 做确定性补漏；证据方向不足或同时存在明确相反信号时留空，不会为提高覆盖率强行打分。

### API 用量与费用

终端的 `总用量` 是模型公司的主调用、自动重试、补充复核及东证联网发现的合计，包含：

- `prompt_tokens`：总输入；
- `completion_tokens`：总输出；
- `cached_tokens`：缓存命中输入；
- `web_search_calls`：实际搜索引擎调用次数。

项目将普通结构化输出限制为10,000 Token，东证分类限制为8,000 Token，避免模型异常重复生成到20,000 Token。2026-09-11 的已验证运行中，广发、中信和东证合计使用39,238个 Token，国泰为本地零 Token；每日用量会随官网报告数量和篇幅变化。价格和免费额度应以腾讯 TokenHub 控制台和官方价格页为准。重校验命令 `--revalidate-existing` 只处理已保存响应，不调用 API。

国泰海通不计入 API 用量：官网标题先由 Python 拆分品种，再用固定短语和交易建议规则分类，输出状态为 `ok_python`。

人工抽查爬虫时，打开四份 `data/raw/YYYYMMDD/*.json`，建议每家公司随机核对2—3条官网链接、日期、标题和正文。

如需手工复核，可以把 `data/llm_input/YYYYMMDD/prompts/*.txt` 发给任意兼容模型，再把回复复制到一个 `.txt` 文件并使用导入命令。导入器会校验日期、公司、字段、观点、板块和网址，并修复少量明确可识别的 JSON 格式问题：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.import_response `
  --date 2026-08-26 --company gfqh --input "C:\path\yuanbao_reply.txt"
```

`--company` 可取 `gfqh`、`citicsf`、`gtjaqh`、`dzqh`。成功后会自动生成同构的公司文件与分块文件：

```text
data/llm_output/YYYYMMDD/gfqh.json
data/llm_output/YYYYMMDD/citicsf.json
data/llm_output/YYYYMMDD/gtjaqh.json
data/llm_output/YYYYMMDD/dzqh.json
```

然后单独运行 finalize。手工导入结果的状态为 `ok_manual_import`，与自动结果一样参与最终校验和 Excel 汇总。

### 分阶段重跑指定日期

完整重跑：

```powershell
.\run_all.ps1 -Date 2026-08-26
```

只重新采集官网：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --stage crawl --date 2026-08-26
```

只重新生成Prompt：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --stage prepare --date 2026-08-26
```

只重新运行 TokenHub API：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --stage llm --date 2026-08-26
```

只根据现有模型JSON重建最终JSON和Excel：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --stage finalize --date 2026-08-26
```

## 六、评分和历史沿用口径

- 同公司 × 同板块：该公司在该板块的不同商品得分等权平均。
- 品种归属由 `src/futures_view/commodities.py` 的 Python 映射强制决定，模型返回的板块不会被直接信任。当前映射已按《商品品种对应》表校准；例如不锈钢、玻璃、纯碱归入黑色系，纸浆归入能化板块。
- 国泰海通一个标题包含多个“品种：观点”时，会先按品种拆段；每个方向明确的品种分别形成一条记录。`多PX空PTA` 这类跨品种指令也会分别记录，方向不明确的品种不强行输出。
- 板块平均：四家公司等权；若某公司当日无该板块观点，先查找该公司该板块最近一次历史得分。
- 历史沿用的单位是“公司 × 板块的历史平均分”，不是复制前一日某个品种，也不是把前一日整份 JSON 混入当天明细。程序会从早于目标日的 `output/YYYYMMDD/公司.json` 中由近到远查找，前一日没有就继续找更早日期。
- 当日与历史都没有：保持空白，不伪装成中性，也不计入平均。
- `当日观点明细` 和当天四家公司 JSON 永远只保存目标日新观点；汇总表中黄色单元格才表示沿用，批注注明实际来源日期。
- 多空分歧：可用公司得分的最大值减最小值。
- 一致性：分歧 `<=1` 为高，`(1,2]` 为中，`>2` 为低；不足两家公司时为无数据。
- 综合判断阈值：一致看多 `1.25`、偏多 `0.5`、偏空 `-0.5`、一致看空 `-1.25`；“一致”还要求分歧 `<=1`。

Excel 包含：

- `期货观点汇总`：公司 × 板块评分矩阵；
- `中期展望`：品种 × 供给/需求/库存/核心逻辑/中期展望，并显示方向、更新时间、数据年龄和来源；字段单元格批注保留各自来源；
- `当日观点明细`：仅目标日新观点及证据；
- `运行与数据质量`：采集/LLM 状态、拒绝记录数和覆盖情况。

`中期展望` 的字段级沿用与板块评分沿用是两套独立口径：前者服务品种研究，后者继续服务原有公司 × 板块评分矩阵。
数据年龄为目标日与该品种最近更新时间的自然日差；4—7 天标黄、8—14 天标橙、超过 14 天标红。
历史日期重跑时，Sheet 会按目标日从 `view_history` 重建快照，不会被数据库中更晚日期的记录污染。

## 七、已知需要实际联调的部分

- TokenHub 的可用模型、价格、免费额度和联网搜索地域可能调整，应以腾讯控制台及官方文档为准。
- 广发 PNG 若无法定位“全品种日评”表头或横线数量异常，会直接报错，避免把错位数据交给模型。可先查看 `gfqh_ocr_audit.json`；修复识别规则后可执行 `conda run --no-capture-output -n futures-view-system python -m futures_view.reprocess_gfqh --date YYYY-MM-DD`，使用已下载PNG重做OCR而无需重新爬取。
- 国泰只按官网标题分类，标题不够明确就不输出，置信度固定为低。
- 官网结构变化或网络失败不会被伪装成“当天无报告”；状态会进入 JSON 和 Excel 数据质量页。

## 八、版本库与本地数据

GitHub 只保存源代码、Prompt、配置模板、脚本、测试和文档。以下内容只保留在本机并由 `.gitignore` 排除：

- `secrets/`：API Key，严禁提交；
- `browser_profile/`：仅兼容网页后端使用的本地资料，日常API运行不会读取；
- `data/`：下载图片、原始数据、Prompt及模型响应；
- `output/`：最终JSON和Excel；
- `tmp/`、缓存、日志和 `*.egg-info/`：可随时重新生成的临时文件。

发布前建议执行：

```powershell
conda run --no-capture-output -n futures-view-system python -m unittest discover -s tests -v
git status --short
```
