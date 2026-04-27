# 📋 Financial Print Decomposition Template v1.0

> 用于每次财报发布后，系统性分解三段DCF (近段 / 远段 / 终值) 的NI曲线revision，识别market mispricing并构造L/S trade。

**Template编号**: PRINT-DECOMP-v1.0
**适用**: 单只股票的quarterly print事件
**所需时间**: 首次build ~3-4小时, 后续同股每季 ~30-60分钟

---

## 🚀 使用方法

1. 财报前 T-3 到 T-1：填 **Table A** (Pre-print Whisper Curve)
2. 财报当天 T-Day：Conference call同步填 **Table B** (Revision Log)
3. 财报后 T+1：完成 **Table B/C/D** (New Curve + Δ Decomposition)
4. 财报后 T+2：看actual stock move，填 **Table E**，决定 **Table F** (Trade Plan)
5. Exit时：填 **Table G** (Post-trade Journal)

**Prompt Claude时说**："用template分析 [TICKER] 的 [FQx'xx] print"

---

## EVENT HEADER

```
Ticker:              ___
Event:               FQ_ '__ print
Report Date:         ____/__/__
Pre-print Price:     $___
Post-print Price:    $___ (T+1 close)
Market Cap (pre):    $___B
Shares Outstanding:  ___B
```

---

## BASELINE & ASSUMPTIONS

```
Last FY Actual NI:           $___B
Last FY Revenue:             $___B
Last FY GM / NPM:            __% / __%
Discount Rate k:             __%  (r_f __% + ERP __% + idio __%)
Terminal g∞:                 __%
Explicit Period:             4 quarters + 4 years + terminal
Model Checksum (PV vs MCap): $___B vs $___B (gap: __%)
```

---

## 📋 TABLE A — PRE-PRINT WHISPER CURVE

*在财报前最后一天收盘后build。20个季度 (5年) + terminal。*

### Quarter Labeling Convention

| Label | 含义 | 状态 |
|---|---|---|
| **FQ0** | 即将print的那个季度 | Print后actual已知, 不折现进PV (已过去) |
| **FQ+1** | FQ0之后的第一个季度 | Print后由管理层guide |
| **FQ+2 to FQ+4** | 后续季度 | 基于whisper和guide推断 |

**示例** (TSM在2026/4/16 print FQ1'26):
- FQ0 = FQ1'26 (Jan-Mar 2026, 已print actual)
- FQ+1 = FQ2'26 (Apr-Jun 2026, 刚guide)
- FQ+2 = FQ3'26 (Jul-Sep 2026)
- FQ+3 = FQ4'26 (Oct-Dec 2026)
- FQ+4 = FQ1'27 (Jan-Mar 2027)

### Pre-print Curve

| Period | Rev ($B) | GM% | NPM% | NI ($B) | Growth | DF | PV ($B) | Source |
|---|---|---|---|---|---|---|---|---|
| FQ0 (anchor) | | | | | | N/A | N/A | Pre-announced or whisper |
| FQ+1 | | | | | | | | Next Q whisper |
| FQ+2 | | | | | | | | Whisper |
| FQ+3 | | | | | | | | Whisper |
| FQ+4 | | | | | | | | Whisper |
| **Y1 Subtotal** (FQ+1 to FQ+4) | | | | | | | | |
| Y+2 | | | | | YoY__% | | | |
| Y+3 | | | | | YoY__% | | | |
| Y+4 | | | | | YoY__% | | | |
| Y+5 | | | | | YoY__% | | | |
| **Explicit Subtotal** | | | | | | | **$___** | |
| **Terminal PV** | base: $__ | g∞: __% | | | | | **$___** | |
| **TOTAL PRE-PRINT PV** | | | | | | | **$___** | |

**注**: FQ0不折现进PV (已是历史), 但列在表中作为曲线锚点 & trajectory reference. Actual FQ0的数字会影响FQ+1起的曲线重绘.

**Reality Check**: Total PV应在当前market cap的±10%以内. 若差很多, back-solve k或g∞并记录implied wedge.

---

## 📋 TABLE B — POST-PRINT REVISION LOG

*财报后24-48h内build。每条commentary → 归类到6种revision type → 标Quality Tag。*

| # | Source | Cell Affected | Pre → Post | Tag |
|---|---|---|---|---|
| 1 | Print actual (Rev) | FQ0 Rev | $__ → $__ | 🟢 HARD |
| 2 | Print actual (GM) | FQ0 GM | __% → __% | 🟢 HARD |
| 3 | Print actual (EPS) | FQ0 NI | $__ → $__ | 🟢 HARD |
| 4 | Next Q guide | FQ+1 Rev/GM | __ → __ | 🟢 HARD |
| 5 | FY guide raise | Y1 growth | __% → __% | 🟡 MEDIUM |
| 6 | Capex / visibility | Y+2/+3 | | 🟡 MEDIUM |
| 7 | LT margin target | Terminal GM | __% → __% | 🟡 MEDIUM |
| 8 | TAM / moat commentary | g∞ | __% → __% | 🟠 SOFT |
| 9 | Product cycle commentary | Y+2-+5 growth | | 🟠 SOFT |
| 10 | Competitive moat event | Terminal k | | 🟠 SOFT |

### Quality Tag定义

| Tag | 含义 | Trading Implication |
|---|---|---|
| 🟢 **HARD** | 硬数字（actual / 明确guide） | 全定价 (100%) |
| 🟡 **MEDIUM** | 明确方向但需validate | 定价 70% |
| 🟠 **SOFT** | 纯commentary narrative | 定价 30-50%, 等validation |
| 🔴 **FRAGILE** | 依赖specific可证伪claim | 定价 10-20%, flag for monitoring |

### 6种Revision Type

1. Print actual (当季硬数据)
2. Guide (next Q)
3. Guide (full year)
4. Street revision implied (48h内街estimate revise)
5. Duration extension (commentary about future cycle)
6. Structural (LT margin, moat, TAM expansion, g∞)

---

## 📋 TABLE C — POST-PRINT NEW CURVE

*根据Table B的revision重新build整条curve。*

| Period | New Rev | New GM% | New NPM% | New NI | Growth | DF | New PV | Dominant Tag |
|---|---|---|---|---|---|---|---|---|
| FQ0 (anchor) | | | | | | N/A | N/A | 🟢 actual |
| FQ+1 | | | | | | | | 🟢 |
| FQ+2 | | | | | | | | 🟢/🟡 |
| FQ+3 | | | | | | | | 🟡 |
| FQ+4 | | | | | | | | 🟡 |
| **Y1 Subtotal** (FQ+1 to FQ+4) | | | | | | | | |
| Y+2 | | | | | | | | 🟡 |
| Y+3 | | | | | | | | 🟡/🟠 |
| Y+4 | | | | | | | | 🟠 |
| Y+5 | | | | | | | | 🟠 |
| **Explicit Subtotal** | | | | | | | **$___** | |
| **Terminal PV** | base: $__ | g∞: __% | | | | | **$___** | Tag: __ |
| **TOTAL POST-PRINT PV** | | | | | | | **$___** | |

---

## 📋 TABLE D — THREE-SEGMENT Δ DECOMPOSITION

*核心表。把A和C相减，看每段贡献多少Δ。*

| Segment | Pre PV | Post PV | Δ PV ($B) | % of Δ | Primary Driver | Dominant Tag |
|---|---|---|---|---|---|---|
| **近段** (FQ+1 to FQ+4, 即Y1) | | | **+$__** | __% | | 🟢 |
| **远段** (Y+2 to Y+5) | | | **+$__** | __% | | 🟡 |
| **终值** (Y+6 ∞) | | | **+$__** | __% | | 🟡/🟠 |
| **TOTAL** | | | **+$__** | 100% | | |

### 终值再拆一层 (若终值Δ > 30% of total)

| 终值子构件 | Δ PV ($B) | % of Terminal Δ | Quality |
|---|---|---|---|
| NI base carry-through (来自远段上修) | | | |
| Terminal NPM re-rate (来自LT margin target) | | | |
| g∞ shift (来自TAM/moat) | | | |
| k shift (来自ERP/geopolitical/rates) | | | |

---

## 📋 TABLE E — MODEL vs MARKET

| Metric | Value |
|---|---|
| Model Implied Δ股价 | +/- __% |
| Options Pre-print Implied Move | ±__% |
| Actual Stock Move (T+1 close) | +/- __% |
| **Gap = Actual − Model** | __pts |

### Scenario Matching

| Scenario | Gap vs Model | 诊断 | Action |
|---|---|---|---|
| **A: Actual << Model** | gap > -5pts | Market只定价HARD, 忽视MEDIUM/SOFT | 🟢🟢 Long, 1.5x |
| **B: Actual < Model** | gap -2 to -5pts | 定价部分 | 🟢 Long, 1.0x |
| **C: Actual ≈ Model** | gap ±2pts | Fair priced | 🟡 Skip |
| **D: Actual > Model** | gap +2 to +5pts | Overreact on SOFT | 🟠 Short, 0.5x |
| **E: Actual >> Model** | gap > +5pts | Severe overreact | 🔴 Short, 1.0x |

**This print matches Scenario: ___**

---

## 📋 TABLE F — TRADE PLAN

```
═══ DIRECTION ═══
Long / Short / Skip

═══ SIZING ═══
___x normal  (Layer 1 / 2 / 3 based on dominant segment)
  - Layer 1 (近段主导) = 0.5-1.0x, holding <1Q
  - Layer 2 (远段主导) = 1.0x, holding 1-4Q
  - Layer 3 (终值主导) = 1.5-2.0x, holding 4Q+

═══ ENTRY ═══
- (price level or condition)

═══ HOLDING PERIOD ═══
__ quarters

═══ VALIDATION CHECKPOINTS ═══
(用于confirm thesis的events/dates)
□ _____________ (expected date: ____)
□ _____________ (expected date: ____)
□ _____________ (expected date: ____)

═══ KILL TRIGGERS ═══
(触发立即exit的事件)
× _____________
× _____________
× _____________

═══ HEDGE ═══
- Pair: ___ @ __x notional
- OR protection: ___ (option structure)

═══ CATALYST CALENDAR ═══
  Next print:         ____/__
  Capex update:       ____/__
  Competitor prints:  ____/__
  Macro events:       ____/__
```

---

## 📋 TABLE G — POST-TRADE JOURNAL

*Exit后T+1周填。累积pod自己的pattern database。*

| Field | 值 |
|---|---|
| Entry price / date | |
| Exit price / date | |
| PnL (%) | |
| PnL ($ risk units / R) | |
| Model was: right / partial / wrong | |
| 近段 prediction accuracy | |
| 远段 prediction accuracy | |
| 终值 prediction accuracy | |
| Largest source of miss | |
| What to do differently next time | |

---

## 📐 核心公式参考

### 三段PV公式

**近段** (Year 1, quarterly)
$$PV_{near} = \sum_{t=1}^{4} \frac{NI_{t}}{(1+k)^{t/4}}$$

**远段** (Year 2-5, annual)
$$PV_{far} = \sum_{y=2}^{5} \frac{NI_{y}}{(1+k)^{y}}$$

**终值** (Year 6 to infinity)
$$PV_{terminal} = \frac{NI_{Y5} \times (1+g_\infty)}{k - g_\infty} \times \frac{1}{(1+k)^{5}}$$

### Δ Decomposition

$$\Delta PV_{total} = \Delta PV_{near} + \Delta PV_{far} + \Delta PV_{terminal}$$

$$\text{Implied股价 Δ\%} = \frac{\Delta PV_{total}}{\text{Market Cap}}$$

### 终值Δ再拆

$$\Delta PV_{terminal} = \frac{1}{(1+k)^5} \times \left[ \underbrace{\frac{\Delta NI_{base}}{k-g_\infty}}_{\text{Level}} + \underbrace{\frac{NI_{base} \cdot \Delta g_\infty}{(k-g_\infty)^2}}_{\text{g∞ shift}} - \underbrace{\frac{NI_{base} \cdot \Delta k}{(k-g_\infty)^2}}_{\text{k shift}} \right]$$

### 折现因子快速参考 (k=9%)

| Year | DF |
|---|---|
| 0 | 1.000 |
| 0.125 (Q1 mid) | 0.989 |
| 0.375 (Q2 mid) | 0.967 |
| 0.625 (Q3 mid) | 0.946 |
| 0.875 (Q4 mid) | 0.925 |
| 1.5 (Y2 mid) | 0.880 |
| 2.5 (Y3 mid) | 0.807 |
| 3.5 (Y4 mid) | 0.740 |
| 4.5 (Y5 mid) | 0.679 |
| 5 (Terminal) | 0.650 |

### Terminal永续乘数 (k=9%, g∞=3%)

$$M_{terminal} = \frac{1+g_\infty}{k - g_\infty} = \frac{1.03}{0.06} = 17.17$$

---

## 🎯 三层Signal架构参考

| Layer | 主导Segment | Holding Period | Sizing | Signal来源 |
|---|---|---|---|---|
| **Layer 1** | 近段 | <1 quarter | 0.5-1.0x | Whisper vs actual + next Q guide |
| **Layer 2** | 远段 | 1-4 quarters | 1.0x | Duration commentary + street revision |
| **Layer 3** | 终值 | 4+ quarters | 1.5-2.0x | LT margin / moat / TAM / g∞ |

**Rule**: Layer 3事件最容易被market underpriced，因为需要多季度validation。这是pod最大alpha来源。

---

## 📚 附录：常用指标缩写

| 缩写 | 全称 | 中文 |
|---|---|---|
| GM | Gross Margin | 毛利率 |
| OpM / OM | Operating Margin | 营业利润率 |
| NPM | Net Profit Margin | 净利率 |
| NI | Net Income | 净利润 |
| DF | Discount Factor | 折现因子 |
| PV | Present Value | 现值 |
| TV | Terminal Value | 终值 |
| MCap | Market Capitalization | 市值 |
| ERP | Equity Risk Premium | 股权风险溢价 |
| LT | Long-term | 长期 |
| QoQ | Quarter-over-Quarter | 环比 |
| YoY | Year-over-Year | 同比 |

---

**End of Template v1.0**

*Last updated: 2026/04/16*
*Next revision: Add options skew, insider trading, short interest overlays (v2.0)*
