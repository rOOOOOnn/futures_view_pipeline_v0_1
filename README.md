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
.\run_all.ps1 -Date 2026-08-25 -Headed -Debug
```

调通后可以去掉 `-Headed -Debug`。总入口依次运行：

1. 三家公司官网采集；
2. 标准化并生成四份公司 Prompt；
3. 用持久登录的腾讯元宝逐公司处理；
4. 校验四份 JSON，生成评分矩阵和 Excel。

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

```text
data/raw/YYYYMMDD/          三家公司原始采集结果、PDF、debug
data/normalized/YYYYMMDD/   统一文档协议
data/llm_input/YYYYMMDD/    四家公司 Prompt
data/llm_output/YYYYMMDD/   元宝返回及自动化状态
output/YYYYMMDD/            最终四 JSON + Excel
```

若网页 LLM 自动化暂时不稳定，可以手动把 `data/llm_input/YYYYMMDD/prompts/*.txt` 逐份发给模型，再将合法 JSON object 保存为：

```text
data/llm_output/YYYYMMDD/gfqh.json
data/llm_output/YYYYMMDD/citicsf.json
data/llm_output/YYYYMMDD/gtjaqh.json
data/llm_output/YYYYMMDD/dzqh.json
```

然后单独运行 finalize。

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
