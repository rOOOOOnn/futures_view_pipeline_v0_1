# 四家期货公司每日观点 Pipeline

本项目按四家公司分别采集/检索，再统一分类、校验、评分并生成 Excel：

```text
广发期货官网 -> 当日详情 -> 公开 PDF -> PDF 文本 -----------┐
中信期货官网 -> 公开 reportsQuery API -> rptSummary -------┤
国泰君安期货官网 -> 当日公开标题（全文在小程序）------------┤
东证期货 -> 腾讯元宝联网检索公众号/公开转载 ----------------┤
                                                          ↓
                    四份公司 Prompt -> 网页版元宝（无 API）
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

JSON 只保存目标日新观点，不把历史观点混入当天记录。历史沿用仅在 Excel 汇总阶段发生，黄色单元格批注会注明原观点日期。

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

在打开的网页中手动扫码登录。进入可输入的聊天页后回到终端按 Enter。登录状态保存在：

```text
browser_profile/llm/
```

请勿把该目录上传、共享或提交到版本库。若登录失效，重新执行 `--login-only`。

## 四、每日总入口

默认使用 Asia/Shanghai 当日：

```powershell
.\run_all.ps1 -Headed -Debug
```

指定日期：

```powershell
.\run_all.ps1 -Date 2026-08-26
```

官网调试时才需要增加 `-Headed -Debug`。总入口依次运行：

1. 三家公司官网采集；
2. 标准化并生成四份公司 Prompt；
3. 用持久登录的腾讯元宝逐公司处理；
4. 校验四份 JSON，生成评分矩阵和 Excel。

元宝阶段会依次发送 4 次，每家公司恰好一次。程序默认切换到“深度思考”，并在回答停止生成且内容稳定后才进入下一家公司；单次总运行硬性限制最多 4 次，防止异常重复发送。

Prompt 采用自动投递：超过 `config.yaml -> prompt_upload_threshold_chars` 的长 Prompt 会作为 `.txt` 附件上传，输入框只发送简短的附件执行指令；短 Prompt 仍直接输入。这样可以避开元宝输入框的文字上限。上传未确认成功时程序会停止该公司并记录错误，不会退回到粘贴超长文本。

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
.\run_company.ps1 -Company gfqh -Stage crawl -Date 2026-08-25 -Headed -Debug
```

只重新生成某公司的 Prompt：

```powershell
.\run_company.ps1 -Company gfqh -Stage prepare -Date 2026-08-25
```

只让元宝重新处理东证：

```powershell
.\run_company.ps1 -Company dzqh -Stage llm -Date 2026-08-25
```

仅重新汇总已有四份 LLM JSON：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.run --stage finalize --date 2026-08-25
```

## 六、中间文件

日期参数使用 `YYYY-MM-DD`，日期文件夹使用 `YYYYMMDD`。例如 `2026-08-26` 对应目录 `20260826`。

| 目录/文件 | 作用 | 建议检查内容 |
|---|---|---|
| `data/raw/YYYYMMDD/gfqh.json` | 广发官网原始结果 | 日期、标题、详情URL、PDF正文、采集状态 |
| `data/raw/YYYYMMDD/citicsf.json` | 中信官网原始结果 | 日期、标题、摘要、作者、详情URL |
| `data/raw/YYYYMMDD/gtjaqh.json` | 国泰官网标题结果 | 日期、标题、详情URL；正文按设计只有标题 |
| `data/raw/YYYYMMDD/all_raw.json` | 三家公司原始数据合并 | `crawl_status` 和全部 `items` |
| `data/raw/YYYYMMDD/pdfs/` | 广发下载的原始PDF | 可人工打开并与提取正文抽查 |
| `data/raw/YYYYMMDD/debug/` | 官网采集调试材料 | 网页结构变化或采集失败时检查 |
| `data/normalized/YYYYMMDD/documents.json` | 统一文档协议 | 只保留状态正常、可交给LLM的文档 |
| `data/llm_input/YYYYMMDD/prompts/` | 四家公司最终Prompt | 长Prompt会作为 `.txt` 上传元宝 |
| `data/llm_input/YYYYMMDD/prompts/manifest.json` | Prompt清单 | 公司、来源数量、Prompt路径 |
| `data/llm_output/YYYYMMDD/*.json` | 元宝原始分类结果 | 必须确认 `processing_status=ok` |
| `data/llm_output/YYYYMMDD/run_summary.json` | 元宝运行摘要 | 快速查看失败公司和错误原因 |
| `data/llm_output/YYYYMMDD/error_*.png` | 元宝失败截图 | 仅失败时生成 |
| `output/YYYYMMDD/*.json` | Python校验、去重、评分后的JSON | 最终 `records` 和 `score` |
| `output/YYYYMMDD/期货公司观点汇总_YYYYMMDD.xlsx` | 最终Excel | 汇总矩阵、当日明细、数据质量 |

项目根目录和程序模块：

| 文件 | 作用 |
|---|---|
| `config.yaml` | 官网地址、浏览器、元宝控件、深度思考和上传阈值 |
| `first_setup.ps1` | 创建/更新Conda环境并安装浏览器 |
| `run_all.ps1` | 默认当日或指定日期的完整流程入口 |
| `run_company.ps1` | 单公司、单阶段调试入口 |
| `prompts/classifier_prompt.txt` | 广发、中信、国泰分类规则 |
| `prompts/dzqh_search_prompt.txt` | 东证公开信息检索规则 |
| `src/futures_view/pipeline.py` | 三家官网采集调度 |
| `src/futures_view/normalize.py` | 原始数据标准化 |
| `src/futures_view/build_prompts.py` | 生成四家公司Prompt |
| `src/futures_view/llm/browser_llm.py` | 元宝登录、上传、发送和JSON提取 |
| `src/futures_view/finalize.py` | 校验、评分、历史沿用和Excel生成 |
| `src/futures_view/inspect_run.py` | 指定日期运行结果检查 |

### 如何判断一次运行是否完整成功

`[DONE] gfqh: 1` 只表示广发官网采集得到1条，不代表元宝和Excel均已成功。运行结束后执行：

```powershell
conda run --no-capture-output -n futures-view-system python -m futures_view.inspect_run --date 2026-08-26
```

检查顺序：

1. 官网采集 `status` 应为 `ok`，日期异常和URL异常应为0。
2. 广发正文不应为空；国泰正文只有标题是正常设计。
3. 四家公司元宝状态都应为 `processing_status=ok`。
4. 最终应显示Excel存在，完整性结论为“四家公司均正常”。
5. 打开Excel的 `运行与数据质量` 工作表，再确认状态和有效观点数。

人工抽查爬虫时，打开三份 `data/raw/YYYYMMDD/*.json`，建议每家公司随机核对2—3条官网链接、日期、标题和正文。

若网页 LLM 自动化暂时不稳定，可以手动把 `data/llm_input/YYYYMMDD/prompts/*.txt` 逐份发给模型，再将合法 JSON object 保存为：

```text
data/llm_output/YYYYMMDD/gfqh.json
data/llm_output/YYYYMMDD/citicsf.json
data/llm_output/YYYYMMDD/gtjaqh.json
data/llm_output/YYYYMMDD/dzqh.json
```

然后单独运行 finalize。

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
- 元宝首次登录必须手动扫码；验证码/CAPTCHA 也必须人工处理。
- 广发 PDF 若是纯扫描件，当前 `pypdf` 可能提取不到文字；状态会标为 `pdf_found_but_text_empty`，后续可加 OCR。
- 国泰只按官网标题分类，标题不够明确就不输出，置信度固定为低。
- 官网结构变化或网络失败不会被伪装成“当天无报告”；状态会进入 JSON 和 Excel 数据质量页。
