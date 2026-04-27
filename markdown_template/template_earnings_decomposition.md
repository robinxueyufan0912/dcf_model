# EARNINGS PRINT DECOMPOSITION TEMPLATE

> **使用方法**: 将此模版贴给Claude，附上 `TICKER = XXX` 和 `PRINT DATE = YYYY-MM-DD`。Claude会自动搜索数据、填写所有表格、计算delta、输出三段归因分析。

---

## 0. 指令

```
TICKER = [填入]
PRINT DATE = [填入]
```

Claude收到此模版后，请执行以下步骤：

### Step 1: 数据采集
- 搜索该公司该季度的earnings print: actual revenue, EPS (GAAP & non-GAAP), gross margin, operating margin, net income
- 搜索print前的sellside consensus: revenue, EPS, gross margin (来源: FactSet/Bloomberg/Visible Alpha/Zacks)
- 搜索print前的buyside whisper (来源: Earnings Whispers, sellside desk commentary, options implied move)
- 搜索该公司的forward guidance: next quarter revenue/margin guide, full-year guide, any long-term target updates
- 搜索print前后的stock price (收盘 → AH/next open)
- 搜索shares outstanding, 当前WACC或用sector default (semis ~10.5%, software ~9.5%, platform ~9.0%)
- 搜索管理层电话会议的关键commentary (TAM, margin targets, competitive dynamics, capex, cycle commentary)

### Step 2: 分类判定
- 基于该公司历史financials，判定 secular / cyclical / hybrid
- 给出判定依据（收入波动性、margin周期性、GDP sensitivity）

### Step 3: 填写以下所有表格
- 所有NI数据单位统一为 $B (billions)
- Pre-print数据 = print前一天收盘时市场embedded的预期
- Post-print数据 = print后基于actual + guide + commentary的revised预期
- Whisper = buyside bogey（如无法获取，用consensus + 历史beat率估算）

### Step 4: 计算
- 近段PV、远段PV、Terminal Value (pre vs post)
- 三段ΔPV归因到stock move
- Residual归因到positioning
- 对cyclical: 计算NI QoQ序列的二阶导数，判定周期位置

### Step 5: 输出
- 先输出精简判定（一段话）
- 再输出完整归因分析

---

## 1. 基本信息

| 字段 | 值 |
|---|---|
| Ticker | |
| 公司名称 | |
| Print日期 | |
| 财报季度 | |
| 分类 | secular / cyclical / hybrid |
| 分类依据 | |
| Pre-print股价 | $ |
| Post-print股价 | $ |
| Stock Move | % |
| Shares Outstanding | B |
| Pre-print Market Cap | $B |
| Post-print Market Cap | $B |
| ΔMarket Cap | $B |
| WACC (k) | % |
| Options Implied Move | ±% |

---

## 2. 当季Print: Actual vs Consensus vs Whisper

| 指标 | Sellside Consensus | Buyside Whisper | Actual | Δ vs Consensus | Δ vs Whisper |
|---|---|---|---|---|---|
| Revenue | | | | | |
| Non-GAAP EPS | | | | | |
| GAAP EPS | | | | | |
| Gross Margin | | | | | |
| Operating Margin | | | | | |
| Net Income | | | | | |
| 关键KPI 1: [名称] | | | | | |
| 关键KPI 2: [名称] | | | | | |

**Beat Quality判定**: [revenue-driven / margin-driven / tax-share-count-driven / mixed]

---

## 3. Forward Guidance vs Expectations

| 指标 | Street Consensus (Q+1) | Buyside Whisper (Q+1) | Company Guide (Q+1) | Δ vs Consensus | Δ vs Whisper |
|---|---|---|---|---|---|
| Revenue | | | | | |
| Gross Margin | | | | | |
| EPS | | | | | |

| 指标 | 此前Full-Year预期 | 更新后Full-Year Guide | Δ |
|---|---|---|---|
| Revenue Growth | | | |
| Margin Target | | | |
| Capex | | | |

**Guide Quality评分**:
- Guide vs Whisper spread: [beat / inline / miss]
- Range width变化: [收窄 / 不变 / 扩大]
- Back-half loading: [有 / 无]
- Cadence pattern: [raise / maintain / trim]

---

## 4. 近端NI曲线 (逐季度, $B) — Pre vs Post

> Claude: 用non-GAAP net income。Pre = print前market embedded预期；Post = print后revised预期。

| 季度 | Pre-Whisper NI | Post-Revised NI | Δ ($B) | Δ% | QoQ% (Post) |
|---|---|---|---|---|---|
| Q-1 (上季actual) | | | — | — | — |
| **Q0 (本季print)** | | | | | |
| Q+1 | | | | | |
| Q+2 | | | | | |
| Q+3 | | | | | |
| Q+4 | | | | | |
| Q+5 | | | | | |
| Q+6 | | | | | |

**近端Curve Shape变化**: [level up / level down / slope steepen / slope flatten / 无变化]

---

## 5. 远端NI曲线 (逐年, $B) — Pre vs Post

> Claude: 基于sellside FY estimates revision + management commentary推算

| 年度 | Pre-Whisper NI | Post-Revised NI | Δ ($B) | Δ% | Implied YoY Growth |
|---|---|---|---|---|---|
| FY+1 | | | | | |
| FY+2 | | | | | |
| FY+3 | | | | | |
| FY+4 | | | | | |
| FY+5 | | | | | |

**远端Curve Shape变化**: [slope up / slope down / duration extend / duration shorten / 无变化]

---

## 6. Terminal Value因子 — Pre vs Post

| 因子 | Pre-print | Post-print | Δ | Δ对TV的sensitivity |
|---|---|---|---|---|
| Long-term Operating Margin (%) | | | bps | +100bps ≈ +X% EV |
| Terminal Growth Rate g∞ (%) | | | bps | +50bps ≈ +X% TV |
| CAP 竞争优势期 (年) | | | yr | |
| Terminal ROIC (%) | | | bps | |
| TAM ($B) | | | $B | |
| Implied Market Share (%) | | | ppt | |
| Through-cycle NI Margin (%) | | | bps | |
| Terminal NI Base ($B) | | | $B | |

### 电话会议六大TV信号检测

| 信号 | 是否触发 | 方向 | 具体内容 |
|---|---|---|---|
| 1. 长期GM/OpM目标更新 | ☐ | ↑/↓ | |
| 2. TAM重新定义/扩张 | ☐ | ↑/↓ | |
| 3. 竞争壁垒事件(moat) | ☐ | ↑/↓ | |
| 4. 定价权信号 | ☐ | ↑/↓ | |
| 5. 增长渐近线语言 | ☐ | ↑/↓ | |
| 6. Capex强度指引 | ☐ | ↑/↓ | |

**TV Revision可持续性判定**: [sustainable / transient / mixed]
- 依据: [可复制性 / 客户集中度 / capex-ROIC一致性 / 持续季度数 / 广度 / 量vs价]

---

## 7. 三段PV计算与归因

> Claude: 使用WACC作为折现率，近端按季度折现(k/4 per quarter)，远端按年度折现，Terminal用Gordon Growth Model。

### PV计算

| 段 | Pre-print PV ($B) | Post-print PV ($B) | ΔPV ($B) | 对Stock Move贡献 (%) |
|---|---|---|---|---|
| 近段 (Q0-Q+6, ~2yr) | | | | |
| 远段 (FY+1 to FY+5) | | | | |
| Terminal Value | | | | |
| **Fundamental合计** | | | | |
| **Positioning残差** | — | — | | |
| **Total Stock Move** | — | — | | |

### 归因可视化

```
近段 Level:    [████████░░░░░░░░░░░░]  +X.X%  ($X.XB)
远段 Slope:    [██████████████░░░░░░]  +X.X%  ($X.XB)
Terminal:      [████████████████████]  +X.X%  ($X.XB)
Positioning:   [░░░░░░░░░░░░░░░░░░░░]  +X.X%  ($X.XB)
```

**主驱动段**: [近段Level / 远段Slope / Terminal Value]
**Holding Period建议**: [短期event T+5 / 中期1-4Q / 长期6-12M+]

---

## 8. Implied Terminal g Solve

| 指标 | Pre-print | Post-print | Δ |
|---|---|---|---|
| Stock Price | | | |
| PV of Explicit Period (近段+远段) | | | |
| Implied TV = MktCap − PV(explicit) | | | |
| Terminal NI Base | | | |
| Solved g∞ | | | bps |
| (k − g∞) spread | | | bps |

---

## 9. 【Cyclical Only】周期定位分析

> Claude: 仅当分类为cyclical或hybrid时填写此section。

### 9a. 历史NI QoQ序列 (12-16季度)

| 季度 | NI ($B) | QoQ% | 二阶导(加速度) |
|---|---|---|---|
| Q-12 | | | — |
| Q-11 | | | |
| Q-10 | | | |
| Q-9 | | | |
| Q-8 | | | |
| Q-7 | | | |
| Q-6 | | | |
| Q-5 | | | |
| Q-4 | | | |
| Q-3 | | | |
| Q-2 | | | |
| Q-1 | | | |
| **Q0 (本季)** | | | |

**NI QoQ趋势**: [加速增长 / 减速增长 / 下行 / 稳定]
**二阶导连续为负季度数**: X个季度
**周期位置判定**: [early-cycle / mid-cycle / late-cycle / peak / downturn / trough]

### 9b. 周期辅助指标

| 指标 | 当前值 | 3yr均值/阈值 | 信号 |
|---|---|---|---|
| 库存天数 (DIO) | | | ☐ >1.2×均值 |
| Book-to-Bill | | 1.0 | ☐ <1.0 |
| 产能利用率 | | 90% | ☐ >90% |
| ASP趋势 | 上升/持平/下降 | — | ☐ 下降 |
| 交付周期 | 扩张/稳定/收缩 | — | ☐ 收缩 |
| 现货 vs 合约价格 | 现货>/<合约 | — | ☐ 现货<合约 |
| GM vs 10yr均值 | 当前X% vs 均值Y% | — | ☐ >2σ |

**Peak Warning触发数**: X / 7
**Davis Double Kill风险**: [低 / 中 / 高 / 极高]

### 9c. Cyclical估值巅峰信号矩阵

| 信号 | 状态 | 权重 |
|---|---|---|
| NI QoQ加速度连续2Q+为负 | ☐ | 20% |
| GM已超过10yr均值2σ | ☐ | 15% |
| 管理层首次mention ASP压力 | ☐ | 15% |
| 三大竞争者同时aggressive capex | ☐ | 15% |
| 现货价格rollover而合约仍涨 | ☐ | 15% |
| Book-to-Bill连续2月<1.0 | ☐ | 10% |
| Sellside仍在raise但stock underperform | ☐ | 10% |
| **Composite Peak Score** | /100 | |

> Score ≥50: 显著peak risk，考虑trim long或initiate short
> Score ≥70: 高概率peak，Davis Double Kill setup

---

## 10. 输出格式

### 精简判定 (一段话)

> Claude: 用以下格式输出一段话精简判定:
>
> **[TICKER] [季度] Print归因**: Stock [+/-X.X%]。主驱动段: [近段/远段/Terminal]（贡献X.X%）。当季actual [beat/inline/miss] whisper [X%]。Guide [above/inline/below] whisper。[如有] TV信号: [具体]。[如cyclical] 周期位置: [位置], Peak Warning: [X/7]。Holding period: [短/中/长]。

### 完整分析

> Claude: 在精简判定之后，输出以上所有填完的表格，以及以下附加分析:
>
> 1. **Beat Quality深度分解**: revenue-driven vs margin-driven vs other
> 2. **Curve Shape变化叙事**: 用2-3句话描述pre vs post曲线shape如何变化
> 3. **Terminal Revision归因**: 具体哪个TV因子在驱动，是否sustainable
> 4. **[Cyclical] Davis Double Play/Kill当前状态**: E和M各自的方向和速度
> 5. **Comparable case**: 历史上类似的print pattern（同一公司或peer）及其后续走势
> 6. **Risk factors**: 本次归因可能错误的地方（positioning overlay、macro、event risk）

---

## 附录: 常用参数参考

| Sector | Default WACC | Typical CAP | TV占比 |
|---|---|---|---|
| Semiconductor (secular) | 9.5-10.5% | 12-18yr | 55-65% |
| Semiconductor (cyclical) | 10.5-12.0% | 8-12yr | 45-55% |
| Software (SaaS) | 9.0-10.0% | 15-20yr | 60-75% |
| Platform/Internet | 8.5-9.5% | 15-25yr | 65-80% |
| Hardware/Equipment | 10.0-11.0% | 10-15yr | 50-60% |
| Memory (DRAM/NAND) | 11.0-13.0% | 5-10yr | 35-50% |

| Terminal g∞ | 适用场景 |
|---|---|
| 2.0-2.5% | Cyclical，无structural growth |
| 2.5-3.0% | 成熟secular，GDP+inflation |
| 3.0-4.0% | Strong secular，structural tailwind |
| >4.0% | 极少数，需要强TAM expansion证据 |
