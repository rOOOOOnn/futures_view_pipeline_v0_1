# 四家期货公司每日观点 Pipeline

本项目按四家公司分别采集/检索，再统一分类、校验、评分并生成 Excel：

```text
广发期货官网 -> 综合 -> 广发期货日评 -> PNG超级汇总 -> 表格结构化OCR ┐
中信期货官网 -> 公开 reportsQuery API -> rptSummary -------┤
国泰君安期货官网 -> 当日公开标题（全文在小程序）------------┤
东证期货 -> 腾讯元宝联网检索公众号/公开转载 ----------------┤
                                                          ↓
                    四份同构 Prompt -> TokenHub API（默认）/ 网页版元宝（备用）
                                                          ↓
                         四份同构 JSON -> Python 校验/评分
                                                          ↓
                       日期文件夹 + 汇总 Excel + 历史沿用
```

## 一、最终结果

每次运行在同一个日期目录中生成：

```text
output/20260825/
  gfqh.json
  citicsf.json
  gtjaqh.json
  dzqh.json
  summary_20260825.json
  期货公司观点汇总_20260825.xlsx
```

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

## 三、首次登录腾讯元宝

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --login-only
```

在打开的网页中手动扫码登录。进入可输入的聊天页后回到终端按 Enter。登录资料尝试保存在：

```text
browser_profile/llm/
```

请勿把该目录上传、共享或提交到版本库。腾讯可能主动使网页登录态失效。正常运行检测到登录弹窗时，会先自动关闭弹窗并检查输入框；匿名对话可用就直接继续。只有匿名输入不可用时，程序才会在同一个浏览器窗口暂停并提示扫码，扫码后按 Enter 即继续本次任务。可通过 `config.yaml -> llm_web.allow_anonymous` 控制是否允许匿名运行。

匿名模式没有深度思考和文件上传控件，程序会自动使用默认回答并直接输入 Prompt；登录模式才会启用深度思考及长 Prompt 附件上传。当前广发日评经过 PNG OCR 后的 Prompt 已控制在可直接输入的长度内。

## 四、每日总入口

默认使用 Asia/Shanghai 当日，并默认通过腾讯 TokenHub API 完成分类和东证联网检索：

```powershell
.\run_all.ps1
```

首次使用时脚本会安全提示输入 API Key，输入内容不会显示，并保存到已被 Git 忽略的 `secrets/tokenhub_api_key.txt`。后续无需重复输入；环境变量 `TENCENT_TOKENHUB_API_KEY` 仍具有更高优先级。也可以显式写出 API 参数：

```powershell
.\run_all.ps1 -Date 2026-08-26 -LlmBackend api
```

浏览器备用方式：

```powershell
.\run_all.ps1 -Date 2026-08-26 -LlmBackend browser
```

API 默认使用 `hy3`：广发、中信、国泰使用新加坡入口做文本分类；东证使用广州入口开启 TokenHub 标准联网搜索。腾讯的 API Key 按地域隔离，新加坡 Key 不能调用广州入口。首次使用东证 API 前，需要在广州站创建 API Key，并在“工具管理”开通联网搜索资源包，然后运行：

```powershell
.\save_api_key.ps1 -Search
```

该 Key 保存到 Git 已忽略的 `secrets/tokenhub_search_api_key.txt`；总入口发现文件不存在时也会自动提示。模型、两个地址和搜索版本位于 `config.yaml -> llm_api`。普通 Key 可由环境变量 `TENCENT_TOKENHUB_API_KEY` 或本地忽略文件提供，搜索 Key 对应 `TENCENT_TOKENHUB_SEARCH_API_KEY`。禁止把 Key 写入 `config.yaml` 或提交到 Git。

东证采用“两阶段”处理：先联网发现公众号/转载来源，再关闭联网按严格 JSON Schema 分类。程序会保存并检查 API 返回的 `search_results`；联网未实际发生、两轮检索为空或 URL 不是具体文章地址时会明确标记失败，不会把模型口头描述的“已搜索”当成成功。来源顺序为微信公众号“东证期货微咨询”（兼容名称“东证期货微资讯”）→ 明确署名的官方页面 → 可靠完整转载。

所有爬虫公司记录的 URL 均由 Python 按来源标题用标准化文档中的官网 URL 回填，不采用模型生成的链接；`https://`、只有域名或无法匹配具体文章的地址会被拒绝。中信正文还会生成“品种：观点”覆盖清单，首轮遗漏时只对遗漏项进行短复核并合并去重；API 日期与标题中的显式 `YYYYMMDD` 必须同时匹配目标日，防止次日报告提前混入。模型返回还会经过本地必填字段和枚举校验，异常空响应会自动重试。

东证公开转载必须实际打开网页核验“东证期货/东证衍生品研究院”署名，不能只相信模型填写的来源名称。国泰标题在 LLM 分类后还会由 Python 对明确方向短语进行保守补充；“关注仓单/利润”等无方向标题仍不输出。

指定日期：

```powershell
.\run_all.ps1 -Date 2026-08-26
```

官网调试时才需要增加 `-Headed -Debug`。总入口依次运行：

1. 三家公司官网采集；
2. 标准化并生成四份公司 Prompt；
3. 默认用 TokenHub API 逐公司处理（浏览器元宝仅作备用）；
4. 校验四份 JSON，生成评分矩阵和 Excel。

模型阶段按 `manifest.json` 依次处理，正常情况下四家公司各执行一项主任务。广发只使用官网“交易品种=综合”下标题严格匹配 `广发期货日评YYYYMMDD` 的详情页，只下载其中 PNG 超级汇总图。Python 会检测表格横线，把“品种、合约、点评、操作建议”按行还原，并通过品种及合约代码固定板块，不再把整图 OCR 文字交叉拼接；PDF、周报汇总和“广发期货期现日报汇总”均不进入主流程。浏览器备用模式会切换到“深度思考”，并在回答停止生成且内容稳定后才进入下一项；单次运行有安全提交上限，防止异常循环发送。

广发日评通常只在周二至周五发布。目标日期没有日评时，官网状态为 `no_reports`，广发当天不调用元宝、不伪造中性观点；最终 Excel 按既有规则沿用最近一次可用观点。

Prompt 采用自动投递：超过 `config.yaml -> prompt_upload_threshold_chars` 的长 Prompt 会作为 `.txt` 附件上传，输入框只发送简短的附件执行指令；短 Prompt 仍直接输入。这样可以避开元宝输入框的文字上限。上传未确认成功时程序会停止该公司并记录错误，不会退回到粘贴超长文本。

附件投递会在三个位置校验：上传稳定后、填写短指令后、发送到消息后。若元宝回复“未收到附件/请重新上传”或“超出字数限制/仅读取部分内容”，对应分块会标记为 `llm_error`，不会把残缺回复误判成正常结果。每次发送都带唯一 `任务ID`，发送后必须在用户消息区重新找到该编号；任务ID、页面地址会写入分块结果和运行日志，便于回看元宝聊天记录。程序还会阻止在“临时对话”模式下运行，因为临时对话不会进入历史记录。同一个 `browser_profile/llm` 不能同时打开两个元宝窗口；重跑前应关闭上一次由项目打开的窗口。

终端在官网阶段显示公司进度，广发还会显示日评、PNG 下载和 OCR 进度；等待元宝时每 5 秒刷新一次进度条。任何任务错误都会在终端、`run.log`、错误 JSON 和截图中留下具体原因。

## 五、逐家公司调试

公司键：

```text
gfqh    广发期货
citicsf 中信期货
gtjaqh  国泰海通期货（官网主体仍显示国泰君安期货）
dzqh    东证期货（只运行元宝联网检索，不爬受限官网）
```

例：只调广发爬虫：

```powershell
.\run_company.ps1 -Company gfqh -Stage crawl -Date 2026-08-25 -Headed -BrowserDebug
```

只重新生成某公司的 Prompt：

```powershell
.\run_company.ps1 -Company gfqh -Stage prepare -Date 2026-08-25
```

只让元宝重新处理东证：

```powershell
.\run_company.ps1 -Company dzqh -Stage llm -Date 2026-08-25
```

只用 API 测试国泰海通（不会调用联网搜索）：

```powershell
.\run_company.ps1 -Company gtjaqh -Stage llm -Date 2026-08-26 -LlmBackend api
```

仅重新汇总已有四份 LLM JSON：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --stage finalize --date 2026-08-25
```

## 六、中间文件

日期参数使用 `YYYY-MM-DD`，日期文件夹使用 `YYYYMMDD`。例如 `2026-08-26` 对应目录 `20260826`。

| 目录/文件 | 作用 | 建议检查内容 |
|---|---|---|
| `data/raw/YYYYMMDD/gfqh.json` | 广发官网原始结果 | 日评日期、详情URL及逐行结构化的品种/合约/点评/建议 |
| `data/raw/YYYYMMDD/gfqh_ocr_audit.json` | 广发 OCR 质量报告 | 表格总行、计分行、非计分品种、不完整行及每行识别结果 |
| `data/raw/YYYYMMDD/citicsf.json` | 中信官网原始结果 | 日期、标题、摘要、作者、详情URL |
| `data/raw/YYYYMMDD/gtjaqh.json` | 国泰官网标题结果 | 日期、标题、详情URL；正文按设计只有标题 |
| `data/raw/YYYYMMDD/all_raw.json` | 三家公司原始数据合并 | `crawl_status` 和全部 `items` |
| `data/raw/YYYYMMDD/images/gfqh/` | 广发日评原始PNG | 可人工打开并与OCR正文抽查 |
| `data/raw/YYYYMMDD/debug/` | 官网采集调试材料 | 网页结构变化或采集失败时检查 |
| `data/normalized/YYYYMMDD/documents.json` | 统一文档协议 | 只保留状态正常、可交给LLM的文档 |
| `data/llm_input/YYYYMMDD/prompts/` | 最终Prompt | 广发通常为一份较短的 OCR 文本 Prompt |
| `data/llm_input/YYYYMMDD/prompts/manifest.json` | Prompt清单 | 公司、来源数量和Prompt路径 |
| `data/llm_output/YYYYMMDD/*.json` | 元宝原始分类结果 | 必须确认 `processing_status=ok` |
| `data/llm_output/YYYYMMDD/parts/*.json` | 每个分块的独立结果/错误 | 聊天地址、分块状态、原始回复和错误原因 |
| `data/llm_output/YYYYMMDD/run.log` | 元宝运行日志 | 发送时间、等待进度、聊天地址和错误 |
| `data/llm_output/YYYYMMDD/run_summary.json` | 元宝运行摘要 | 快速查看失败公司和错误原因 |
| `data/llm_output/YYYYMMDD/error_*_partNN.png` | 元宝失败截图 | 仅失败时生成 |
| `output/YYYYMMDD/*.json` | Python校验、去重、评分后的JSON | 最终 `records` 和 `score` |
| `output/YYYYMMDD/期货公司观点汇总_YYYYMMDD.xlsx` | 最终Excel | 汇总矩阵、当日明细、数据质量 |
| `output/YYYYMMDD/quality_report_YYYYMMDD.json` | 自动质量门禁报告 | 是否通过、重复/非法条数、Excel一致性和明确观点遗漏 |

项目根目录和程序模块：

| 文件 | 作用 |
|---|---|
| `config.yaml` | 官网地址、浏览器、元宝控件、深度思考和上传阈值 |
| `first_setup.ps1` | 创建/更新Conda环境并安装浏览器 |
| `run_all.ps1` | 默认当日或指定日期的完整流程入口 |
| `run_company.ps1` | 单公司、单阶段调试入口 |
| `save_api_key.ps1` | 安全录入普通分类 Key 或广州联网搜索 Key |
| `prompts/classifier_prompt.txt` | 广发、中信、国泰分类规则 |
| `prompts/dzqh_search_prompt.txt` | 东证公开信息检索规则 |
| `src/futures_view/pipeline.py` | 三家官网采集调度 |
| `src/futures_view/normalize.py` | 原始数据标准化 |
| `src/futures_view/build_prompts.py` | 生成四家公司Prompt |
| `src/futures_view/ocr.py` | 广发日评PNG的表格定位、逐单元格OCR与质量信息 |
| `src/futures_view/commodities.py` | 品种规范名、合约纠错及固定板块映射 |
| `src/futures_view/reprocess_gfqh.py` | 不重新下载，使用已有PNG重做广发表格OCR |
| `src/futures_view/llm/browser_llm.py` | 元宝登录、上传、发送和JSON提取 |
| `src/futures_view/llm/api_llm.py` | TokenHub API、严格JSON Schema、用量记录和东证联网搜索 |
| `src/futures_view/title_rules.py` | 震荡偏多/偏空、操作建议和标题方向的确定性规则 |
| `src/futures_view/source_validation.py` | URL修复、证据强度和东证真实网页署名核验 |
| `src/futures_view/finalize.py` | 校验、评分、历史沿用和Excel生成 |
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

一次已验证的正常输出示例（2026-08-28）：三家官网采集分别为1、15、59条，最终JSON和Excel明细均为104条，重复、非法和明确规则遗漏均为0。

自动分类有三层防护：API 严格 Schema 只允许统一品种名；广发结构化表格、中信明确小标题和国泰标题均由 Python 做确定性补漏；证据方向不足或同时存在明确相反信号时留空，不会为提高覆盖率强行打分。

### API 用量与费用

终端的 `总用量` 是该公司主调用、自动重试、补充复核及东证联网发现的合计，包含：

- `prompt_tokens`：总输入；
- `completion_tokens`：总输出；
- `cached_tokens`：缓存命中输入；
- `web_search_calls`：实际搜索引擎调用次数。

项目将普通结构化输出限制为10,000 Token，东证分类限制为8,000 Token，避免模型异常重复生成到20,000 Token。2026年8月实测完整运行通常约0.15～0.30元/次；价格和免费额度可能变化，应以腾讯 TokenHub 控制台和官方价格页为准。重校验命令 `--revalidate-existing` 只处理已保存响应，不调用API。

人工抽查爬虫时，打开三份 `data/raw/YYYYMMDD/*.json`，建议每家公司随机核对2—3条官网链接、日期、标题和正文。

若网页 LLM 自动化暂时不稳定，可以手动把 `data/llm_input/YYYYMMDD/prompts/*.txt` 逐份发给模型，把回复复制到一个 `.txt` 文件，然后使用导入命令。导入器会校验日期、公司、字段、观点、板块和网址，并可修复元宝长 JSON 中明确可识别的缺失字段引号及重复 `https`：

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

只重新运行元宝：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --stage llm --date 2026-08-26
```

只根据现有元宝JSON重建最终JSON和Excel：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --stage finalize --date 2026-08-26
```

## 七、评分和历史沿用口径

- 同公司 × 同板块：该公司在该板块的不同商品得分等权平均。
- 板块平均：四家公司等权；若某公司当日无观点，先查找该公司该板块最近一次历史得分。
- 当日与历史都没有：保持空白，不伪装成中性，也不计入平均。
- 多空分歧：可用公司得分的最大值减最小值。
- 一致性：分歧 `<=1` 为高，`(1,2]` 为中，`>2` 为低；不足两家公司时为无数据。
- 综合判断阈值：一致看多 `1.25`、偏多 `0.5`、偏空 `-0.5`、一致看空 `-1.25`；“一致”还要求分歧 `<=1`。

Excel 包含：

- `期货观点汇总`：公司 × 板块评分矩阵；
- `当日观点明细`：仅目标日新观点及证据；
- `运行与数据质量`：采集/LLM 状态、拒绝记录数和覆盖情况。

## 八、已知需要实际联调的部分

- 腾讯元宝网页可能改版；届时优先调整 `config.yaml -> llm_web` 的输入、发送、消息和新建对话选择器。
- 元宝浏览器仅作为备用；自动化浏览器可能触发登录或风控。匿名模式可用时程序会关闭登录弹窗继续，否则验证码/CAPTCHA 必须人工处理。日常稳定运行优先使用 TokenHub API。
- 广发 PNG 若无法定位“全品种日评”表头或横线数量异常，会直接报错，避免把错位数据交给模型。可先查看 `gfqh_ocr_audit.json`；修复识别规则后可执行 `conda run --no-capture-output -n futures-view-system python -m futures_view.reprocess_gfqh --date YYYY-MM-DD`，使用已下载PNG重做OCR而无需重新爬取。
- 国泰只按官网标题分类，标题不够明确就不输出，置信度固定为低。
- 官网结构变化或网络失败不会被伪装成“当天无报告”；状态会进入 JSON 和 Excel 数据质量页。

## 九、版本库与本地数据

GitHub 只保存源代码、Prompt、配置模板、脚本、测试和文档。以下内容只保留在本机并由 `.gitignore` 排除：

- `secrets/`：API Key，严禁提交；
- `browser_profile/`：网页登录态；
- `data/`：下载图片、原始数据、Prompt及模型响应；
- `output/`：最终JSON和Excel；
- `tmp/`、缓存、日志和 `*.egg-info/`：可随时重新生成的临时文件。

发布前建议执行：

```powershell
conda run --no-capture-output -n futures-view-system python -m unittest discover -s tests -v
git status --short
```
