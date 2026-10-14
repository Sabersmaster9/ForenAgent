# ForenAgent 1.0 — 法医临床司法鉴定 × 多智能体 LLM × 计算机视觉系统

ForenAgent 1.0 是一个面向**法医临床司法鉴定意见书生成**的端到端研究原型。系统把案卷材料 OCR、事实结构化、检验记录整理、量角器图像读数、ROM 功能丧失率查表、伤残条款匹配、三期评定和报告审计拆分为可替换、可复核的多智能体流水线。

本项目的核心定位不是让大模型直接裁决鉴定结论，而是构建一个：

```text
本地优先 OCR/LLM 辅助事实抽取
        +
人工可确认的视觉测量证据
        +
确定性规则引擎负责关键结论
        +
Boss Agent 输出正式草稿与审计报告
```

的司法鉴定辅助框架。LLM 主要承担材料归纳、结构化补缺、检验记录整理和审查提示；伤残等级、ROM 功能丧失率、条款适用、误工/护理/营养期等核心结论由规则引擎生成，以保证结果可复现、可解释、可追溯。

> 重要说明：ForenAgent 当前仍是研究原型，输出应作为司法鉴定人的复核草稿和审计辅助材料，不应直接替代人工鉴定意见。

---

## 1. 项目面向的问题

法医临床司法鉴定意见书通常需要整合多源材料：

- 法院委托书、起诉状、鉴定申请书、交通事故认定书等案情材料；
- 门急诊病历、住院病历、出院记录、影像报告、诊断证明等医学材料；
- 检验过程中的检查记录、医生记录、辅助检查资料和可用转写文本；
- 关节活动度测量结果、影像学表现、手术史、后遗功能障碍；
- 《人体损伤致残程度分级》《人身损害误工期、护理期、营养期评定规范》等法规标准。

传统人工流程存在材料多、格式杂、复核成本高、测量记录客观化不足等问题。ForenAgent 将上述流程拆解为可测试、可替换、可审计的模块：前端负责材料读取和事实抽取，中间层负责检验记录与视觉证据整理，后端规则引擎负责条款推理和报告组装。

---

## 2. 总体架构

系统可概括为“三层证据处理 + 五 Agent 流水线 + 人工审计闭环”。

```text
┌────────────────────────────────────────────────────────────────────┐
│                              输入与知识层                            │
│  案卷文书/PDF/图片/Word │ 检查记录/文本 │ 活动度照片 │ 法规标准/案例库 │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│                         多智能体处理流水线                           │
│                                                                    │
│  Extractor → Structuring → Visual → Judge → Ruler                   │
│  OCR/清洗      第1-3部分结构化  图像读数候选  第4部分检验过程  第5-6部分规则推理 │
│                                                                    │
│                         Boss Agent                                 │
│        调度编排 · 人工确认/补充 · 专家文书渲染 · 统一审计报告           │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│                         决策与证据输出层                             │
│  01_extractor.md │ 02_structuring.json │ 03_visual.json │ 04_judge.json │
│  05_ruler.json   │ 06_boss_draft.md    │ 06_boss_reviewed.md │ 06_boss_audit.md │
└────────────────────────────────────────────────────────────────────┘
```

### 2.1 Agent 分工

| 模块 | 主要文件 | 输入 | 输出 | 核心能力 |
|---|---|---|---|---|
| Extractor Agent | `agents/extractor_agent/extractor_agent_main.py` | 案件材料文件夹，支持 PDF/DOCX/DOC/图片/文本 | `01_extractor.md` | 本地 PaddleOCR 优先、可复制 PDF 直读、低质量页二次 OCR、可选云端 LlamaParse |
| Structuring Agent | `agents/structuring_agent/structuring_agent_main.py` | OCR 后 Markdown | `02_structuring.json` | 第1-3部分结构化：基本情况、基本案情、资料摘要；支持 `rule` / `llm` / `hybrid` 模式 |
| Visual Agent | `agents/visual_agent/visual_agent_main.py` | 检查照片、活动度/量角器图片 | `03_visual.json` | YOLO-Seg 量角器读数、质量特征、拒识/补偿、人工确认候选，不直接生成伤残等级 |
| Judge Agent | `agents/judge_agent/judge_agent_main.py` | 结构化摘要、检查记录 DOCX/TXT/MD、已确认视觉 ROM | `04_judge.json` | 第4部分检验过程整理、体格检查信息抽取、ROM 标准化、辅助检查整理 |
| Ruler Agent | `agents/ruler_agent/ruler_agent_main.py` | Structuring + Judge JSON | `05_ruler.json` | ROM 查表、伤残条款匹配、三期评定、阴性结论和人工审查清单 |
| Boss Agent | `agents/boss_agent/boss_agent_main.py` + `agents/boss_agent/report_renderer.py` | 案件根目录或兼容双目录输入 | `06_boss_draft.md`、`06_boss_reviewed.md`、`06_boss_audit.md` | 全流程调度、GUI/CLI、人工补充回填、专家文书渲染、最终审计报告 |

---

## 3. 端到端数据流

```text
案件鉴定材料
  └─ PDF / Word / 图片 / 扫描件
      ↓ Extractor Agent
01_extractor.md
      ↓ Structuring Agent
02_structuring.json
      ↓
检查照片/活动度照片
      ↓ Visual Agent
03_visual.json（候选读数、质量证据、人工确认状态）
      ↓
检查记录/医生记录/可用转写文本
      ↓ Judge Agent
04_judge.json（第4部分检验过程 + structured_rom）
      ↓ Ruler Agent
05_ruler.json（条款、等级、三期、人工审查清单）
      ↓ Boss Agent
06_boss_draft.md（审计前草稿）
06_boss_reviewed.md（审计后正式草稿）
06_boss_audit.md（统一审计报告）
```

关键中间产物说明：

| 文件 | 含义 | 复核价值 |
|---|---|---|
| `01_extractor.md` | 原始案卷材料 OCR 与清洗后的 Markdown | 记录材料读取覆盖范围，是后续事实抽取的证据来源 |
| `02_structuring.json` | 前三部分结构化事实 | 可用于分析规则/LLM 混合抽取的准确率、遗漏和冲突 |
| `03_visual.json` | Visual Agent 输出 | 保存每张图的读数候选、质量分数、拒识原因、人工确认状态和 ROM 候选 |
| `04_judge.json` | 检验过程结构化数据 | 包括主诉、体格检查、ROM、辅助检查、视觉候选提示、存疑点 |
| `05_ruler.json` | 法理推理结构化结果 | 包括条款、等级、功能丧失率、三期结果、阴性结论和匹配路径 |
| `06_boss_draft.md` | 审计前鉴定意见书草稿 | 用于人工审计前预览 |
| `06_boss_reviewed.md` | 审计后正式草稿 | 主要人工复核正文，尽量保持司法鉴定意见书风格 |
| `06_boss_audit.md` | Boss 统一审计报告 | 汇总人工审查项、上游存疑点、待补充字段、运行耗时和 AI 一致性审查 |

---

## 4. 法医 × 计算机视觉：量角器读数子系统

### 4.1 任务定位

在法医临床鉴定中，关节活动度测量是判断功能障碍和伤残等级的重要依据。传统记录通常依赖人工读数和文字转录，存在以下问题：

1. 量角器拍摄角度、遮挡、透视畸变会影响读数；
2. 单纯输出角度缺少“该读数是否可靠”的质量解释；
3. 活动度数值进入伤残等级推理后，需要可追溯的视觉证据链；
4. 法医场景下不能为了追求全自动而输出不可靠结论，必要时应拒识或提示人工复核。

因此，本项目的视觉模块目标是：从量角器照片中生成**可解释、可拒识、可补偿、可审计**的角度读数，而不是无条件给出一个预测角度。

### 4.2 当前视觉方案

当前 runtime 方案位于 `agents/visual_agent/visual_agent_main.py`；几何与质量特征算法在 `agents/visual_agent/goniometer/`，模型权重路径为：

```text
models/goniometer_seg_best.pt
```

核心流程如下：

```text
输入量角器图片
  ↓
YOLO-Seg 分割 fixed_arm / moving_arm / pivot
  ↓
固定臂、移动臂、轴点几何拟合
  ↓
提取图像证据质量特征
  ├─ 分割置信度与 mask 面积
  ├─ fixed/moving arm 边缘平行性与宽度变化
  ├─ pivot 圆度、轴比、半径变异
  ├─ 多候选角度分歧 candidate_delta_spread
  └─ 透视风险 perspective_risk_score 与补偿可行性
  ↓
置信度门控
  ├─ 图像证据不足：拒识，提示人工复核
  └─ 图像证据可用：进入预测路由
        ├─ baseline 几何读数
        ├─ graded arc 轻/中/强透视补偿
        └─ radial fixed-arm correction
  ↓
输出最终角度候选 + 路由类型 + 质量证据 + 是否拒识
```

### 4.3 与主系统的衔接边界

Visual Agent 已接入 Boss 主流程并固定产出 `03_visual.json`。但视觉读数遵循保守策略：

- 视觉模块**只读角度，不裁决伤残等级**；
- 视觉模块从文件夹/文件名中保守推断关节、侧别、动作，但这些医学语义必须由人工确认；
- `03_visual.json` 中的 `structured_rom_candidates` 是候选值；
- 未经人工确认时，`structured_rom` 默认保持为空，Judge/Ruler 不会把图片读数直接当成正式 ROM；
- GUI 模式下可在视觉预审窗口逐图确认，确认后 Boss 会生成事实回填项并重跑 Judge/Ruler；
- 无 GUI 模式下，视觉候选和复核要求会进入 `06_boss_audit.md`，不阻断报告生成。

```text
活动度照片/量角器图像
  ↓ Visual Agent
角度候选 + 质量证据 + 拒识/补偿标记
  ↓ 人工确认关节/侧别/动作/角度
确认后的 structured_rom
  ↓ Judge Agent
04_judge.json
  ↓ Ruler Agent
按《人体损伤致残程度分级》附录 C 查表计算功能丧失率
  ↓ Boss Agent
06_boss_reviewed.md / 06_boss_audit.md
```

### 4.4 阶段实验结果

阶段实验（训练与实验数据已随论文归档，不随仓库分发）在 303 张有 GT 的样本上达到：

| 指标 | Baseline | 分级补偿方案 | 改善情况 |
|---|---:|---:|---:|
| 拒识数 | 0 | 9 | 拒识率 2.97%，满足 ≤3% 目标 |
| 非拒识 MAE | 2.256° | 1.766° | 降低 0.490° |
| P90 | 4.545° | 3.870° | 降低 0.675° |
| 最大误差 | 11.661° | 6.722° | 降低 4.939° |
| >5° 样本数 | 19 | 5 | 减少 14 张 |

阶段结论：视觉子系统已形成“低拒识率置信度门控 + 多通道路由 + 自适应分级透视补偿”的读数框架。其论文价值在于将角度预测转化为带质量解释的证据生成过程：系统不仅回答“角度是多少”，还回答“为什么可以读”“为什么需要补偿”“什么时候不应自动读”。

---

## 5. 规则推理与可审计设计

ForenAgent 的关键决策层尽量避免依赖大模型自由生成。

### 5.1 ROM 查表引擎

Ruler Agent 内置《人体损伤致残程度分级》附录 C 中与六大关节相关的 ROM 表：

- 肩关节；
- 肘关节；
- 腕关节；
- 髋关节；
- 膝关节；
- 踝关节。

系统根据 Judge Agent 输出的结构化 ROM 数据计算单方向功能丧失值，并结合健侧数据进行扣减。膝关节采用屈曲丧失与伸展丧失求和方式；其他关节按方向平均或表格规则计算。默认肌力等级为 M5，CLI 中预留 `--muscle-grade` 参数。

### 5.2 伤残条款匹配

Ruler Agent 采用“标准条款覆盖矩阵 + 确定性规则引擎”的方式工作：

1. 优先读取 Structuring Agent 输出的 `forensic_facts.rule_features`、`fracture_features`、`period_rule_features`；
2. 对可自动判断的条款进行确定性匹配，例如脊柱椎体骨折、肋骨数量/缺失/畸形愈合阈值、ROM 功能丧失率、肢体长度差、足弓破坏、骨盆/跟骨畸形愈合、体表/面部瘢痕阈值、眼眶骨折眼球内陷、关节置换、创伤性关节炎、手足功能丧失分值、单眼视力/视野数值阈值、青少年长骨骨骺受累和慢性骨髓炎长期不愈等；
3. 对明确低于条款阈值的情形，输出可审计的 `negative_auto` 阴性结论，并保留未达阈值的拒绝理由；
4. 对缺少结构化事实或需临床综合判断的条款，不由 LLM 直接裁决，而是在覆盖矩阵和输出审计块中标为 `manual_review`、`clinical_context_required` 或 `blocked_missing_fact_fields`；
5. 每个匹配结果保留条款号、分支、条款文本、等级、匹配路径、事实依据、拒绝理由和警告信息。

### 5.3 三期评定

系统内置《人身损害误工期、护理期、营养期评定规范》（GA/T 1193-2014）条款矩阵，根据结构化损伤事实和治疗方式匹配误工期、护理期、营养期范围。明确区间默认取上限并保留原始范围；多处损伤遵循附录 A.4，不简单累加，而以较长期限为主并保留所有候选条款。对“根据临床治疗情况确定”的条款，系统只标记需人工评估，不自动编造天数；覆盖检查会同步生成人工审查清单。

---

## 6. 目录结构与数据资源

### 6.1 仓库结构

```text
ForenAgent_1.0/
├── agents/                          # 全部智能体代码（进版本控制）
│   ├── common/                      # LLM/OCR 客户端、运行时配置、输出路径
│   ├── extractor_agent/             # OCR 与材料清洗
│   ├── structuring_agent/           # 第 1-3 部分结构化
│   ├── visual_agent/
│   │   └── goniometer/              # 量角器几何拟合与质量特征算法
│   ├── judge_agent/                 # 第 4 部分检验过程
│   ├── ruler_agent/                 # 第 5-6 部分规则引擎与条款库
│   │   └── rules/                   # 伤残/三期条款矩阵 JSON
│   └── boss_agent/                  # 编排、GUI、报告渲染、一致性审计
├── machine_eval/                    # 评估与自检工具
│   ├── check_human_loop.py          # 人机闭环四窗口字段契约自检
│   ├── compare_with_expert.py       # 系统输出 vs 专家意见书逐项对比
│   └── machine_score_test.py        # 批量评分
├── models/                          # 视觉模型权重（不进版本控制）
│   └── goniometer_seg_best.pt
├── files/知识库/
│   └── 知识库_条例/                 # 法规标准 Markdown（规则引擎查表源，进版本控制）
│                                    # 知识库_案例/ 为真实卷宗，只留本机（见 6.2）
├── database/                        # 案卷材料，按案号分目录（不进版本控制）
├── test_result/                     # 运行输出，按案号分目录（不进版本控制）
├── README.md
└── requirements.txt
```

### 6.2 版本控制边界

仓库**只收代码、规则文本和文档**；以下内容留在本机，不进 git：

| 排除项 | 原因 |
|---|---|
| `database/` | 真实案卷，含身份证、病历等个人信息 |
| `files/知识库/知识库_案例/` | 参考鉴定意见书姓名只做部分脱敏，仍含身份证号、住址、亲属姓名、住院号 |
| `test_result/` | 运行输出，可重跑再生 |
| `models/*.pt` | 模型权重二进制 |
| `files/知识库/知识库_条例/*.pdf` | 法规 PDF；正文 Markdown 才是运行时依赖 |
| `.env` | API 密钥 |

> 参考案例不是运行时依赖，移出后系统功能不受影响。

`files/知识库/知识库_条例/` 下的 Markdown 是 `agents/ruler_agent/coverage.py` 的**运行时输入**，不能删除；对应的 PDF 仅为存档，可另行保存。

---

## 7. 技术栈与运行模式

| 类别 | 技术 |
|---|---|
| 主要语言 | Python |
| 默认 LLM | 本地 OpenAI-compatible 服务（默认 Ollama `qwen3.5:4b`） |
| 可选云端 LLM | SiliconFlow API（默认 `deepseek-ai/DeepSeek-V3.2`，需 `--allow-cloud` 明确允许） |
| 默认 OCR | 本地 PaddleOCR（默认 `PP-OCRv5_mobile_det` + `PP-OCRv5_mobile_rec`） |
| 可选云端 OCR | LlamaParse（需 `LLAMA_CLOUD_API_KEY` 和 `--allow-cloud`） |
| LLM 框架/客户端 | LangChain、LangGraph、OpenAI-compatible client |
| 数据结构与校验 | Pydantic |
| GUI | tkinter |
| 视觉读数方案 | YOLO-Seg、mask 几何拟合、质量特征、置信度门控、多通道路由、透视补偿 |

依赖文件见 `requirements.txt`。当前 Boss Agent 会优先使用 `FORENAGENT_PYTHON` 指定的解释器；未设置时默认尝试 `D:/CondaData/envs/forenagent/python.exe`。**请勿使用仓库内的 `.venv`**：该环境为早期残留、依赖不完整（缺 PaddleOCR / torch / ultralytics），已移出仓库。

---

## 8. 快速开始

### 8.1 环境准备

建议使用项目专用 conda 环境，并在该环境中安装依赖：

```bash
conda create -n forenagent python=3.14
conda activate forenagent
pip install -r requirements.txt
```

如使用 GPU 版 PaddleOCR，请按 `requirements.txt` 中的 CUDA 12.6 源安装/更新：

```bash
python -m pip install paddlepaddle-gpu==3.3.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

如需让 Boss 自动切换到指定解释器，可在 `.env` 中配置：

```env
FORENAGENT_PYTHON=D:/CondaData/envs/forenagent/python.exe
```

### 8.2 配置本地优先运行

默认配置为本地优先：

```env
FORENAGENT_LLM_PROVIDER=local
FORENAGENT_OCR_PROVIDER=local
FORENAGENT_LOCAL_LLM_BASE_URL=http://127.0.0.1:11434/v1
FORENAGENT_LOCAL_CHAT_MODEL=qwen3.5:4b
FORENAGENT_LOCAL_LLM_DEVICE=gpu
FORENAGENT_LOCAL_OCR_DEVICE=gpu
```

如果需要使用云端 LLM/OCR，必须显式允许：

```env
SILICONFLOW_API_KEY=你的SiliconFlow_API密钥
LLAMA_CLOUD_API_KEY=你的LlamaParse_API密钥
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_CHAT_MODEL=deepseek-ai/DeepSeek-V3.2
FORENAGENT_ALLOW_CLOUD=1
```

CLI 中也可以用 `--allow-cloud` 明确允许本次使用云端。默认未允许时，系统会阻止司法材料被发送到云端。

### 8.3 GUI 模式（推荐，贴近实际工作方式）

```bash
python agents/boss_agent/boss_agent_main.py
```

输入分两步，一次只处理一个案件：

| 步骤 | 操作 | 必填 |
|---|---|---|
| 第 1 步 | 选择**案件材料文件夹**（委托书、病历、影像报告等证据文档） | 必选 |
| 第 2 步 | 选择**检查照片文件夹**（活动度/量角器照片） | 可选，没有就跳过，系统自动不做视觉读数 |

系统不再要求把材料按固定目录名摆放，也不再扫描"案件父目录"去猜案件——你指向哪个文件夹，就处理哪个文件夹。

运行参数核对无误后点「▶ 运行所选案件」，过程中会依次弹出视觉预审、审计前补充、问答式审计和最终预览四个窗口。

> 「第 4 部分 检验过程」的检查记录单独读取：若材料文件夹内存在 `检查记录` / `损伤情况` / `检查资料` 等子目录，Judge 只读该子目录，不会把整夹案件证据误当检查记录；没有该子目录时走结构化摘要并由鉴定人在审计窗口补充。

**交付目录自动生成在案件材料文件夹内**，运行完成后点「打开输出目录」即可，见第 9 节。

### 8.4 可选：案件根目录一键模式

若材料已按 `1 鉴定材料 / 2 鉴定意见书 / 3 检查照片` 组织好，Boss 也可按关键词自动识别通道：

```text
案件根目录/
├── 1 鉴定材料/       # 自动读取：委托书、病历、影像报告等案卷材料
├── 2 鉴定意见书/     # 专家参考，只供评价，不进入自动生成
├── 3 检查照片/       # 活动度照片、量角器照片；进入 Visual Agent
└── 4 ai生成报告/     # 自动写入 batch_YYYYMMDD_HHMMSS 输出目录
```

兼容关键词：

- 鉴定材料目录：`鉴定材料`、`证据材料`、`移交材料`、`案卷材料`；
- 检查照片目录：`检查照片`、`活动度照片`、`量角器`、`照片`；
- 检查记录目录：`损伤情况`、`检查资料`、`检查材料`、`检查记录`、`检验记录`、`检验材料`；
- 专家参考目录：`鉴定意见书`、`专家报告`、`参考报告`、`样例输出`；
- 输出基目录：`ai生成报告`、`输出`、`result`、`output`。

运行单案：

```bash
python agents/boss_agent/boss_agent_main.py \
  --no-gui \
  --case-folder "案件根目录路径"
```

批量运行多案：

```bash
python agents/boss_agent/boss_agent_main.py \
  --no-gui \
  --case-folders "案件1路径" "案件2路径" "案件3路径"
```

如需暂时只跑文本链路，可跳过视觉：

```bash
python agents/boss_agent/boss_agent_main.py \
  --no-gui \
  --case-folder "案件根目录路径" \
  --skip-visual
```

如需指定检查记录目录或检查照片目录：

```bash
python agents/boss_agent/boss_agent_main.py \
  --no-gui \
  --case-folder "案件根目录路径" \
  --exam-doc-folder "检查记录目录" \
  --photo-folder "检查照片目录"
```

### 8.5 人工补充事实回填

人工通道的边界是“补充检查事实，不直接替系统填写伤残等级或三期结论”。例如人工可补充瘢痕长度、视力/视野数值、肋骨数量、是否骨不连、是否累及关节面、ADL 评分或三期临床依据；补充后由 Ruler 规则引擎重新裁决。

可将人工补充 JSON 作为 `--human-overrides` 传入：

```bash
python agents/boss_agent/boss_agent_main.py \
  --no-gui \
  --case-folder "案件根目录路径" \
  --human-overrides "人工补充事实.json"
```

GUI 模式下，系统会在视觉预审、审计前补充和人工审计窗口中生成回填项，并在必要时重跑 Ruler/Boss。

#### 伤残等级确认的覆盖规则

伤残结论由 Ruler 给出初判，鉴定人在审计窗口中确认或调整。三者的优先级必须分清，否则会把“系统判不出”误写成“确实不构成”：

| Ruler 状态 | 人工是否表态 | 正文写法 |
|---|---|---|
| `positive_auto` 等阳性 | 未表态 | 按 Ruler 条款与等级写入 |
| `insufficient_facts` / `manual_review` | **未表态** | “现有材料尚不足以判断……需补充检验资料后由鉴定人评定”，**不得写成否定结论** |
| 任意状态 | **已确认等级（含确认不构成伤残）** | 以人工结论为准，人工填写的结论表述与调整理由一并入文 |

两条配套约定：

- `human_disability_confirmation.clause_text` 只有形如伤残结论的句子才会落到“……规定，<结论>。”的位置；历史遗留的伤情清单或条款标签（如“开颅术后”）会自动回退到固定表述，避免写出没有谓语的病句。
- `human_disability_confirmation.conclusion_type` 与 `grade_number` 是控制字段，二者成对写入；`conclusion_type=no_disability` 会把等级强制归零。

可用 `machine_eval/check_human_loop.py` 复核本表：它按“控制字段看效果、内容字段看原文”两类分别做哨兵探测，并对阳性/阴性两条渲染路径各测一遍。

### 8.6 CLI 兼容双目录模式

旧的双目录模式仍可使用：

```bash
python agents/boss_agent/boss_agent_main.py \
  --no-gui \
  --transfer-folder "案件移交材料文件夹路径" \
  --check-folder "检查照片文件夹路径"
```

如检查记录与照片分开存放，建议显式传入：

```bash
python agents/boss_agent/boss_agent_main.py \
  --no-gui \
  --transfer-folder "案件移交材料文件夹路径" \
  --exam-doc-folder "检查记录文件夹路径" \
  --photo-folder "检查照片文件夹路径"
```

### 8.7 常用运行参数

| 参数 | 说明 |
|---|---|
| `--mode hybrid|llm|rule` | Structuring Agent 模式，默认 `hybrid` |
| `--llm-provider local|cloud|off` | LLM 调用通道，默认 `local` |
| `--ocr-provider local|cloud|text-only` | OCR 调用通道，默认 `local` |
| `--allow-cloud` | 明确允许本次使用云端模型/OCR 处理材料 |
| `--local-chat-model` | 本地普通抽取模型名 |
| `--local-audit-model` | 本地最终审计/高光专用模型名 |
| `--local-llm-base-url` | 本地 OpenAI-compatible base URL |
| `--local-ocr-device auto|gpu|cpu` | 本地 OCR 设备策略 |
| `--local-ocr-render-scale` | PDF OCR 首次渲染倍率 |
| `--local-ocr-second-pass-scale` | 疑难页二次 OCR 渲染/放大倍率 |
| `--disable-local-ocr-second-pass` | 关闭疑难页/图片二次 OCR |
| `--batch-label` | 指定输出批次目录名 |
| `--output-dir` | 指定单案输出目录 |
| `--skip-visual` | 跳过 Visual Agent |
| `--human-overrides` | 传入人工补充事实 JSON |
| `--ignore-saved-overrides` | 忽略输出目录中累计的 `07_human_overrides.json`，从零重跑 |

### 8.8 单独运行各 Agent

```bash
# 文档 OCR 与清洗
python -m agents.extractor_agent.extractor_agent_main

# 第1-3部分结构化，支持 hybrid / llm / rule
python -m agents.structuring_agent.structuring_agent_main \
  --input 01_extractor.md \
  --section full \
  --full-mode hybrid

# 量角器图片读数
python -m agents.visual_agent.visual_agent_main \
  --input-dir "检查照片目录" \
  --output-dir "输出目录" \
  --json-name 03_visual.json

# 第4部分检验过程生成
python -m agents.judge_agent.judge_agent_main \
  --input-dir "检查记录目录" \
  --visual-json "输出目录/03_visual.json" \
  --output-dir "输出目录"

# 第5-6部分规则推理
python -m agents.ruler_agent.ruler_agent_main \
  --structuring-json 02_structuring.json \
  --judge-json 04_judge.json \
  --output-dir "输出目录"
```

### 8.9 Ruler Agent 覆盖矩阵与规则测试

```bash
# 生成/检查伤残条款覆盖矩阵和三期条款覆盖矩阵
python -m agents.ruler_agent.validate_rules --check coverage

# 检查规则 JSON、等级、区间和字段引用是否有效
python -m agents.ruler_agent.validate_rules --check schema

# 运行合成事实夹具，检查阳性/阴性规则命中
python -m agents.ruler_agent.validate_rules --check synthetic

# 用真实案件的结构化事实验证：二椎体压缩性骨折 → 九级；脊柱骨折非手术治疗 → 三期
# 预期值取自 agents/ruler_agent/fixtures/，案件路径按本机实际目录填写
python -m agents.ruler_agent.validate_rules \
  --case "test_result/<案号>/02_structuring.json" \
  --judge-json "test_result/<案号>/04_judge.json" \
  --expected-case-id golden_001_two_vertebral_g9
```

覆盖矩阵会同步写入 `agents/ruler_agent/rules/coverage_disability.json`、`agents/ruler_agent/rules/coverage_period.json`，并在 `runs/ruler_rule_engine_coverage/` 下生成 CSV/Markdown 报告。人工审查说明会生成到 `runs/ruler_rule_engine_coverage/manual_review_guide.md` 和 `runs/ruler_rule_engine_coverage/manual_review_items.csv`。

---

## 9. 典型输出目录

输出落在**案件材料文件夹内**的 `输出/` 子目录。鉴定人只需要看三个文件：

```text
<案件材料文件夹>/
├── (委托书、病历、影像报告等证据文档)
└── 输出/
    ├── 鉴定意见书.docx     ← 按司法文书版式排好的正式文书，可直接交付
    ├── 鉴定意见书.md       ← 同一份正文的 Markdown
    ├── AI审计报告.md       ← 人工审查项、上游存疑点、待补充字段与一致性审计
    └── _work/              ← 过程文件，界面不展示
```

| 交付物 | 说明 |
|---|---|
| `鉴定意见书.docx` | 按 `agents/boss_agent/docx_renderer.py` 的版式渲染：标题小标宋二号居中、一级标题黑体、正文仿宋_GB2312 三号、固定行距 28 磅、首行缩进 2 字符、A4 公文页边距。鉴定中心若有自己的模板，改该文件顶部的 STYLE 常量即可。 |
| `鉴定意见书.md` | 审计后的正文，与 docx 同源，便于二次编辑与比对 |
| `AI审计报告.md` | 人工审查项、上游存疑点、待补充字段、人工补充模板、AI 一致性审计与运行摘要 |

`输出/_work/` 保存全部过程文件（OCR 文本、各阶段 JSON、人工回填记录），**不展示也不清理**：

- 重跑同一案件时可直接复用，不必重新 OCR；
- `07_human_overrides.json` 里的累计人工回填会自动重放，鉴定人审计过的内容不会再次丢失。

> 关于 `07_human_overrides.json`：人工审计结果此前只写入 `02/04` 阶段 JSON，而这两个文件在每次重跑时都会重建，导致鉴定人审计过的内容被静默清空。现在所有人工回填（视觉预审、审计前补充、问答式审计、正文补丁）都会并入本文件，**下次运行同一案件时自动重放**。如需从零重跑，删除该文件或加 `--ignore-saved-overrides`。

> 重跑安全：OCR 扫描会跳过 `输出/`、`_work/` 等运行产物目录，避免把上一轮的鉴定意见书当成案件材料重新抽取。

### 9.1 旧模式输出（`test_result/<案号>/`）

用 `--case-folder` 走案件根目录模式时，输出仍写在 `test_result/<案号>/`，保留完整阶段文件（`01_extractor.md` … `07_human_overrides.json`）以便论文评测与逐阶段复核。

---

## 10. 可用于论文讨论的创新点

1. **多智能体分工的司法鉴定文书生成框架**  
   将案卷读取、事实抽取、视觉读数、检验过程整理、法理规则推理和报告审查拆分为独立 Agent，降低单一大模型端到端生成的不可控性。

2. **本地优先的司法材料处理策略**  
   默认使用本地 OCR 和本地 OpenAI-compatible LLM，云端调用必须通过 `--allow-cloud` 或 GUI 明确授权，降低敏感司法材料外传风险。

3. **LLM 与规则引擎的职责隔离**  
   LLM 用于材料理解和辅助归纳，核心法律/医学结论由确定性规则生成，提升可复现性和审计性。

4. **ROM 国标查表自动化**  
   内置六大关节活动度功能丧失率查表逻辑，将检验记录中的活动度数值转化为可解释的百分比结果。

5. **伤残条款多路径匹配**  
   综合 ROM 功能丧失、诊断关键词、手术事实、骨折内固定、特殊损伤条款等路径，输出条款、等级和匹配依据。

6. **法医测量场景的视觉可靠性建模**  
   视觉模块不是黑箱预测角度，而是引入分割质量、几何拟合质量、透视风险、候选分歧等图像证据，形成低拒识率质量门控和自适应补偿机制。

7. **人工确认后再进入规则推理的证据链**  
   视觉读数先作为候选证据，经人工确认后才回填为 ROM 事实，形成“量角器照片 → 角度候选 → 人工确认 → ROM → 条款 → 鉴定意见 / 审计报告”的可追溯链条。

8. **审计前/审计后双报告机制**  
   `06_boss_draft.md` 保留初裁视图，`06_boss_reviewed.md` 展示审计后正文，`06_boss_audit.md` 汇总问题、补充字段和一致性审查，便于论文评测和专家复核。

---

## 11. 当前边界与后续方向

### 当前边界

- 系统仍是研究原型，输出应作为专家复核草稿，而非直接替代司法鉴定人结论；
- 文档 OCR 和 LLM 抽取仍可能产生错读、漏读或混淆，需要保留原文证据和存疑点；
- 当前 Judge 主链路稳定输入为 DOCX/TXT/MD 等检查记录；音视频材料建议先形成转写文本后进入流程；
- 视觉读数最大误差阶段结果仍为 6.722°，尚未完全压到 5°以内；
- 视觉读数的关节、侧别、动作语义必须人工确认，不能只凭文件名推断进入正式结论；
- 当前 303 张 GT 样本可支持阶段评估，但正式论文建议进一步划分开发集、验证集和最终测试集，避免策略搜索过拟合。

### 后续方向

1. 扩展检查音视频的直接 ASR 接入，并将转写证据片段纳入 Judge 审计链；
2. 引入更强的量角器弧线、刻度线和椭圆透视几何建模；
3. 建立“自动读数—低置信拒识—人工复核—规则更新”的闭环；
4. 扩充多关节、多拍摄条件、多设备来源的独立验证集；
5. 对 OCR、LLM 抽取、视觉读数、ROM 查表、条款匹配分别建立可量化评估指标；
6. 将报告审查从单次 LLM 附录升级为结构化一致性校验与专家反馈学习机制。

---

## 12. 一句话摘要

ForenAgent 1.0 是一个面向法医临床司法鉴定的多智能体原型系统：它默认本地优先处理司法材料，用 OCR/LLM 辅助多源案卷和检验记录的结构化抽取，用人工可确认的量角器视觉读数生成 ROM 候选证据，用确定性规则完成 ROM 查表、伤残条款匹配和三期评定，并通过审计前/审计后报告与统一审计报告提供可复核的鉴定意见书生成框架。
