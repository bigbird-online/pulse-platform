import Foundation
import XCTest
@testable import PulseStats

/// Panel content built from real metrics, plus the wording rules that keep the
/// "要你操心的" block readable by someone who does not know the codebase.
final class PanelViewModelTests: XCTestCase {
    // MARK: real payload end to end

    private func liveMetrics() throws -> SnapshotMetrics {
        SnapshotMetrics(try SnapshotDecoder().decode(try fixtureData("live-v2a")))
    }

    func testRelayModelReadsTheRealPayload() throws {
        let model = RelayViewModel(metrics: try liveMetrics(), hasSnapshot: true)
        XCTAssertEqual(model.ccProcesses, 3)
        XCTAssertEqual(model.ccBusy, 1)
        XCTAssertEqual(model.ccIdle, 2)
        XCTAssertEqual(model.ccRateLimited, false)
        XCTAssertEqual(model.botsOnline, 15)
        XCTAssertEqual(model.codexUsedPercent, 98.0)
        XCTAssertNotNil(model.codexResetsAt)
        XCTAssertEqual(model.inputTokens, 7137)
        XCTAssertEqual(model.outputTokens, 477_197)
        XCTAssertEqual(model.turns, 74)
        XCTAssertEqual(model.failures, 4)
        XCTAssertEqual(model.hourlyOutputTokens?.count, 24)
        XCTAssertEqual(model.agents?.count, 6)
        XCTAssertEqual(model.deliverySent, 179)
    }

    func testHarnessModelReadsTheRealPayload() throws {
        let model = HarnessViewModel(metrics: try liveMetrics(), hasSnapshot: true)
        XCTAssertEqual(model.weekCreated, 58)
        XCTAssertEqual(model.dayCreated, 3)
        XCTAssertEqual(model.dayCompleted, 2)
        XCTAssertEqual(model.pendingHandoffs, 5)
        XCTAssertEqual(model.stalled?.count, 3)
        XCTAssertEqual(model.maxStalledDays, 12)
        // 12 days stalled sits in the 7–14 band.
        XCTAssertEqual(model.weekLamp, .amber)
        // 2 of 3 completed is 66.7% → below 80%.
        XCTAssertEqual(model.completionLamp, .red)
    }

    func testAgentsAreSortedByOutputDescending() throws {
        let model = RelayViewModel(metrics: try liveMetrics(), hasSnapshot: true)
        let outputs = try XCTUnwrap(model.agents).map(\.outputTokens)
        XCTAssertEqual(outputs, outputs.sorted(by: >))
        XCTAssertEqual(model.agents?.first?.agent, "pulse")
    }

    func testMultiSessionAgentIsDetected() throws {
        let model = RelayViewModel(metrics: try liveMetrics(), hasSnapshot: true)
        XCTAssertEqual(model.multiSessionAgents.map(\.agent).sorted(), ["ken", "pulse"])
    }

    // MARK: no data means unknown, never zero

    func testEmptySnapshotYieldsUnknownsNotZeros() {
        let model = RelayViewModel(metrics: SnapshotMetrics([]), hasSnapshot: false)
        XCTAssertNil(model.outputTokens)
        XCTAssertNil(model.codexUsedPercent)
        XCTAssertNil(model.agents)
        XCTAssertEqual(model.ccMenuBarText, "未知")
        XCTAssertEqual(model.codexMenuBarText, "未知")
        XCTAssertEqual(model.ccLamp, .gray)
        XCTAssertEqual(model.codexLamp, .gray)
        // Nothing is known, so nothing is claimed to be wrong either.
        XCTAssertTrue(model.concerns.isEmpty)
    }

    func testMissingUsageWithFreshSnapshotDoesNotClaimNoUsage() {
        let model = RelayViewModel(metrics: SnapshotMetrics([]), hasSnapshot: true)
        XCTAssertEqual(model.agentUsageEmptyText, "用量数据不可用，并非今天没有用量")
    }

    func testMeasuredEmptyUsageHasDistinctWording() {
        let model = RelayViewModel(metrics: SnapshotMetrics([
            metric(MetricID.byAgent, .rows([]), unit: "list")
        ]), hasSnapshot: true)
        XCTAssertEqual(model.agentUsageEmptyText, "今天还没有已记录用量")
    }

    func testPresentationClockDoesNotResurrectStaleQuotaAndDailyUsage() throws {
        let future = ISO8601DateFormatter().date(from: "2026-09-15T00:00:00Z")!
        let model = RelayViewModel(metrics: try liveMetrics(), hasSnapshot: true, at: future)
        XCTAssertNil(model.codexUsedPercent)
        XCTAssertNil(model.outputTokens)
        XCTAssertNil(model.agents)
        XCTAssertNotEqual(model.agentUsageEmptyText, "今天还没有已记录用量")
    }

    func testWorkingTokensIncludeCacheCreationButExcludeCacheRead() {
        let metrics = SnapshotMetrics([
            metric(MetricID.inputTokensUncached, .number(10), unit: "tokens"),
            metric(MetricID.cacheCreationTokens, .number(300), unit: "tokens"),
            metric(MetricID.outputTokens, .number(20), unit: "tokens"),
            metric(MetricID.cacheReadTokens, .number(500), unit: "tokens"),
        ])

        let model = RelayViewModel(metrics: metrics, hasSnapshot: true)

        XCTAssertEqual(model.workingTokens, 330)
        XCTAssertEqual(model.inputTokensUncached, 10)
        XCTAssertEqual(model.cacheCreationTokens, 300)
        XCTAssertEqual(model.outputTokens, 20)
        XCTAssertEqual(model.cacheReadTokens, 500)
        XCTAssertEqual(
            model.inputTokensUncached! + model.cacheCreationTokens! + model.outputTokens!,
            model.workingTokens
        )
        XCTAssertNotEqual(model.workingTokens, 830)
    }

    func testMissingCacheCreationMakesWorkingTokensUnknown() {
        let metrics = SnapshotMetrics([
            metric(MetricID.inputTokensUncached, .number(10), unit: "tokens"),
            metric(MetricID.outputTokens, .number(20), unit: "tokens"),
            metric(MetricID.cacheReadTokens, .number(500), unit: "tokens"),
        ])

        let model = RelayViewModel(metrics: metrics, hasSnapshot: true)

        XCTAssertNil(model.cacheCreationTokens)
        XCTAssertNil(model.workingTokens)
        XCTAssertEqual(StatsFormat.compactCount(model.workingTokens), StatsFormat.unknown)
    }

    // MARK: throughput stats

    /// 四个数一起在：今天进出、本周进出。
    ///
    /// 这条断言换过一次方向。原版断言「今天的数字**不该**出现在格子里」——
    /// 那是我 2026-07-31 删掉两格时写下的，把自己的误判钉成了契约。大鸟当天
    /// 就报「今日的 wi 汇总也都没有了」。测试全绿而产品变坏，正是因为测试
    /// 忠实地保护了错误的意图。现在钉的是使用者真正要的：四个数都在。
    func testAllFourThroughputNumbersStayVisible() {
        let model = HarnessViewModel(
            metrics: SnapshotMetrics([
                metric(MetricID.wiDayCompleted, .number(5), unit: "count"),
                metric(MetricID.wiDayCreated, .number(3), unit: "count"),
                metric(MetricID.wiIsoWeekCompleted, .number(21), unit: "count"),
                metric(MetricID.wiIsoWeekCreated, .number(17), unit: "count"),
            ]),
            hasSnapshot: true
        )

        XCTAssertEqual(
            model.throughputStats.map(\.caption),
            ["今天收口", "今天新建", "本周收口", "本周新建"]
        )
        XCTAssertEqual(model.throughputStats.map(\.value), ["5", "3", "21", "17"])
        XCTAssertEqual(
            Set(model.throughputStats.map(\.id)).count,
            model.throughputStats.count
        )

        // The sentence stays too — it carries the verdict the tiles cannot.
        XCTAssertEqual(model.todayLine, "今天 收口 5 件 · 新建 3 件 · 净减 2 件")
    }

    /// 坏记录只在真跳过时才说话。
    ///
    /// 这个 key 平时整个不存在（生产者写的是 `if unreadable:`），所以「读不到」
    /// 是常态而非异常，不能因此在面板上留一行 `--`。
    func testUnreadableRecordsNoteOnlyAppearsWhenRecordsWereSkipped() {
        let silent = HarnessViewModel(
            metrics: SnapshotMetrics([
                metric(MetricID.wiDayCreated, .number(11), unit: "count"),
            ]),
            hasSnapshot: true
        )
        XCTAssertNil(silent.unreadableRecords)
        XCTAssertNil(silent.ledgerQualityNote)

        // 显式的 0 也不说话：0 条坏行没什么可报告的。
        let zero = HarnessViewModel(
            metrics: SnapshotMetrics([
                metric(MetricID.harnessUnreadableRecords, .number(0), unit: "count"),
            ]),
            hasSnapshot: true
        )
        XCTAssertEqual(zero.unreadableRecords, 0)
        XCTAssertNil(zero.ledgerQualityNote)

        let skipped = HarnessViewModel(
            metrics: SnapshotMetrics([
                metric(MetricID.wiDayCreated, .number(11), unit: "count"),
                metric(MetricID.harnessUnreadableRecords, .number(3), unit: "count"),
            ]),
            hasSnapshot: true
        )
        XCTAssertEqual(skipped.unreadableRecords, 3)
        XCTAssertEqual(
            skipped.ledgerQualityNote,
            "另有 3 条记录无法解析已跳过，上列计数是下界"
        )
    }

    /// 读不到就说读不到——格子里不许用 0 顶替 nil。
    func testUnknownThroughputCountsStayUnknown() {
        let model = HarnessViewModel(
            metrics: SnapshotMetrics([]), hasSnapshot: true
        )
        XCTAssertEqual(model.todayLine, "今天的进出还没读到")
        XCTAssertEqual(
            model.throughputStats.map(\.value),
            Array(repeating: StatsFormat.unknown, count: 4)
        )
    }

    // MARK: per-agent monthly usage

    /// 「每个 Agent 花了多少」必须有下文。
    ///
    /// 2026-07-30 一版更严格的 billing 实现把这块顶掉了：新判据要求 obs 里
    /// 有 billing_component_id（从来没有过），于是归因永远「证据不足」，而
    /// 原本能跑出数的实现被一并删除，面板上这一段直接消失且无人接替。
    /// 大鸟 07-31：「原版本呈现的是每个 Agent 耗费了多少 Token，新版本完全
    /// 丢弃了之前的功能，而且没有新东西替代」。这条断言把它钉住。
    func testPerAgentMonthlyUsageSurvivesAlongsideTheBill() throws {
        let rows: [[String: MetricScalar]] = [
            ["agent": .string("pulse"), "output_tokens": .number(2_907_862),
             "share": .number(0.297), "amortized_usd": .number(80.22)],
            ["agent": .string("ken"), "output_tokens": .number(1_068_688),
             "share": .number(0.109), "amortized_usd": .number(29.48)],
        ]
        let model = RelayViewModel(
            metrics: SnapshotMetrics([
                metric(MetricID.mtdByAgent, .rows(rows), unit: "list"),
                metric(MetricID.mtdOutputTokens, .number(9_786_764), unit: "tokens"),
                metric(MetricID.mtdDaysCovered, .number(17), unit: "count"),
            ]),
            hasSnapshot: true
        )

        let costs = try XCTUnwrap(model.mtdAgentCosts)
        XCTAssertEqual(costs.count, 2)
        XCTAssertEqual(costs.first?.agent, "pulse")
        XCTAssertEqual(costs.first?.outputTokens, 2_907_862)
        XCTAssertEqual(model.mtdOutputTokens, 9_786_764)
        XCTAssertEqual(model.mtdDaysCovered, 17)
        // 覆盖天数必须说出来：报「本月」却只覆盖 17 天而不说会被当成整月。
        let note = try XCTUnwrap(model.mtdUsageNote)
        XCTAssertTrue(note.contains("17"))
        // 也要说清这是估算，不是账单归因——两种数不能被读成一回事。
        XCTAssertTrue(note.contains("估算"))
    }

    /// 读不到就是读不到，不用空表冒充「这个月没人干活」。
    func testAbsentMonthlyUsageIsUnknownNotEmpty() {
        let model = RelayViewModel(metrics: SnapshotMetrics([]), hasSnapshot: true)
        XCTAssertNil(model.mtdAgentCosts)
        XCTAssertNil(model.mtdOutputTokens)
        XCTAssertNil(model.mtdUsageNote)
        XCTAssertTrue(model.visibleMtdAgentCosts.isEmpty)
    }

    // MARK: fixed-subscription billing

    func testBillingUsesIntegerCentsAndConservesAcrossTopSixAndTail() throws {
        let rows: [[String: MetricScalar]] = [
            ["agent": .string("a"), "output_tokens": .number(700), "attributed_cents": .number(7_000)],
            ["agent": .string("b"), "output_tokens": .number(600), "attributed_cents": .number(6_000)],
            ["agent": .string("c"), "output_tokens": .number(500), "attributed_cents": .number(5_000)],
            ["agent": .string("d"), "output_tokens": .number(400), "attributed_cents": .number(3_000)],
            ["agent": .string("e"), "output_tokens": .number(300), "attributed_cents": .number(2_000)],
            ["agent": .string("f"), "output_tokens": .number(200), "attributed_cents": .number(1_500)],
            ["agent": .string("g"), "output_tokens": .number(100), "attributed_cents": .number(500)],
        ]
        let model = RelayViewModel(
            metrics: SnapshotMetrics(billingMetrics(agentRows: rows)),
            hasSnapshot: true
        )
        let facts = try XCTUnwrap(model.billingFacts)
        let attribution = try XCTUnwrap(model.billingAttribution)
        XCTAssertEqual(facts.totalCents, 27_000)
        XCTAssertEqual(attribution.attributedCents, 25_000)
        XCTAssertEqual(attribution.unattributedCents, 2_000)
        XCTAssertEqual(
            attribution.agents.reduce(0) { $0 + $1.attributedCents },
            attribution.attributedCents
        )
        XCTAssertEqual(attribution.attributedCents + attribution.unattributedCents,
                       facts.totalCents)
        XCTAssertEqual(Set(attribution.agents.map(\.agent)).count,
                       attribution.agents.count)
        XCTAssertEqual(model.visibleBillingAgents.count, 6)
        XCTAssertEqual(model.hiddenBillingAttributedCents, 500)
        XCTAssertEqual(
            model.visibleBillingAgents.reduce(0) { $0 + $1.attributedCents }
                + model.hiddenBillingAttributedCents,
            attribution.attributedCents
        )
    }
    func testCodexTwentyDollarsRemainExplicitlyUnattributed() throws {
        let model = RelayViewModel(
            metrics: SnapshotMetrics(billingMetrics(agentRows: [[
                "agent": .string("pulse"), "output_tokens": .number(100),
                "attributed_cents": .number(25_000),
            ]])),
            hasSnapshot: true
        )
        let facts = try XCTUnwrap(model.billingFacts)
        let codex = try XCTUnwrap(
            facts.components.first { $0.componentID == "codex" }
        )
        XCTAssertEqual(codex.amountCents, 2_000)
        XCTAssertEqual(codex.attribution, "unattributed_no_agent_source")
        XCTAssertEqual(model.codexUnattributedCents, 2_000)
        XCTAssertFalse(try XCTUnwrap(model.billingAttribution).agents.contains {
            $0.agent.lowercased() == "codex"
        })
    }
    /// 坏日志行同理：只在真跳过时才修饰今天的用量读数。
    func testMalformedLinesNoteOnlyAppearsWhenLinesWereSkipped() {
        let silent = RelayViewModel(
            metrics: SnapshotMetrics([
                metric(MetricID.outputTokens, .number(1_000), unit: "count"),
            ]),
            hasSnapshot: true
        )
        XCTAssertNil(silent.malformedLines)
        XCTAssertNil(silent.usageQualityNote)

        let zero = RelayViewModel(
            metrics: SnapshotMetrics([
                metric(MetricID.usageMalformedLines, .number(0), unit: "count"),
            ]),
            hasSnapshot: true
        )
        XCTAssertEqual(zero.malformedLines, 0)
        XCTAssertNil(zero.usageQualityNote)

        let skipped = RelayViewModel(
            metrics: SnapshotMetrics([
                metric(MetricID.outputTokens, .number(1_000), unit: "count"),
                metric(MetricID.usageMalformedLines, .number(2), unit: "count"),
            ]),
            hasSnapshot: true
        )
        XCTAssertEqual(skipped.malformedLines, 2)
        XCTAssertEqual(
            skipped.usageQualityNote,
            "另有 2 行日志无法解析已跳过，今日用量是下界"
        )
    }

    /// 没有 Codex 组件时整行消失，而不是显示 $0。
    ///
    /// `nil` 与 `0` 在这里是两件事：前者是「这台机器没在跑 Codex」，后者会是
    /// 「跑了但没花钱」。把前者渲染成 $0 等于凭空报告一个不存在的组件。
    func testCodexUnattributedIsNilWithoutACodexComponent() {
        let model = RelayViewModel(
            metrics: SnapshotMetrics(billingFactMetrics(
                total: 25_000,
                componentRows: [[
                    "component_id": .string("claude_code"),
                    "amount_cents": .number(25_000),
                    "currency": .string("USD"),
                    "attribution": .string("cc_mtd_output_share"),
                ]]
            )),
            hasSnapshot: true
        )
        XCTAssertNil(model.codexUnattributedCents)
    }

    func testKnownBillFactsSurviveInsufficientAgentAttribution() throws {
        var metrics = billingFactMetrics()
        for id in [
            MetricID.billingPeriod, MetricID.billingByAgent,
            MetricID.billingAttributedCents, MetricID.billingUnattributedCents,
        ] {
            metrics.append(billingMetric(
                id, .null, status: .unknown, quality: .insufficient,
                reason: "MTD turn.end lacks billing_component_id"
            ))
        }
        let model = RelayViewModel(
            metrics: SnapshotMetrics(metrics), hasSnapshot: true
        )
        XCTAssertEqual(model.billingFacts?.totalCents, 27_000)
        XCTAssertEqual(model.billingFacts?.components.map(\.amountCents),
                       [25_000, 2_000])
        XCTAssertNil(model.billingAttribution)
        XCTAssertEqual(model.billingAttributionMessage,
                       "Agent归因证据不足；CC待来源确认")

        var damaged = billingFactMetrics()
        for id in [
            MetricID.billingPeriod, MetricID.billingByAgent,
            MetricID.billingAttributedCents, MetricID.billingUnattributedCents,
        ] {
            damaged.append(billingMetric(
                id, .null, status: .unknown, quality: .unavailable,
                reason: "degen-2026-07-20.jsonl:260 has invalid JSON"
            ))
        }
        let damagedModel = RelayViewModel(
            metrics: SnapshotMetrics(damaged), hasSnapshot: true
        )
        XCTAssertEqual(damagedModel.billingFacts?.totalCents, 27_000)
        XCTAssertNil(damagedModel.billingAttribution)
        XCTAssertEqual(damagedModel.billingAttributionMessage,
                       "Agent归因源不可用；历史观测损坏")
    }
    func testPartialPeriodNamesZeroCompleteDaysAndMissingCompletenessMarker() {
        let model = RelayViewModel(
            metrics: SnapshotMetrics(billingMetrics(agentRows: [[
                "agent": .string("pulse"), "output_tokens": .number(100),
                "attributed_cents": .number(25_000),
            ]])),
            hasSnapshot: true
        )
        XCTAssertEqual(model.billingCoverageNote,
                       "完整覆盖 0/25 天（无完备标记）")
        XCTAssertFalse(model.billingCoverageNote?.contains("没有用量") ?? true)
    }
    func testWrongMissingOrUnknownBillingValuesNeverBecomeZero() {
        let fractional = billingFactMetrics(total: 27_000.5)
        XCTAssertNil(SnapshotMetrics(fractional).billingFacts)
        XCTAssertNil(SnapshotMetrics([]).billingFacts)
        let unknown = [
            billingMetric(
                MetricID.billingTotalCents, .number(0),
                status: .unknown, quality: .unavailable
            ),
        ]
        XCTAssertNil(SnapshotMetrics(unknown).billingFacts)
        XCTAssertNil(
            RelayViewModel(metrics: SnapshotMetrics(unknown), hasSnapshot: true)
                .billingAttribution
        )
    }
    func testMalformedBillingFactsFailClosedAsAWhole() {
        let valid = billingComponentRows()
        var duplicate = valid
        duplicate.append(valid[0])
        var negative = valid
        negative[0]["amount_cents"] = .number(-1)
        let cases = [
            billingFactMetrics(componentRows: duplicate),
            billingFactMetrics(componentRows: negative),
            billingFactMetrics(total: 26_999),
        ]
        for metrics in cases {
            XCTAssertNil(
                SnapshotMetrics(metrics).billingFacts,
                "invalid component facts must not be compacted into a partial bill"
            )
        }
    }
    func testMalformedAttributionBundleFailsClosedAsAWhole() {
        let validRow: [String: MetricScalar] = [
            "agent": .string("pulse"), "output_tokens": .number(100),
            "attributed_cents": .number(25_000),
        ]
        let duplicate = billingMetrics(agentRows: [validRow, validRow])
        var negative = validRow
        negative["output_tokens"] = .number(-1)
        let missing = billingMetrics(agentRows: [validRow]).filter {
            $0.id != MetricID.billingUnattributedCents
        }
        var mixedQuality = billingMetrics(agentRows: [validRow]).filter {
            $0.id != MetricID.billingByAgent
        }
        mixedQuality.append(billingMetric(
            MetricID.billingByAgent, .rows([validRow]),
            quality: .validated, sampleCount: 1
        ))
        let badPeriods: [[[String: MetricScalar]]] = [
            [],
            [billingPeriodRow(), billingPeriodRow()],
            [billingPeriodRow(overrides: ["timezone": .string("UTC")])],
            [billingPeriodRow(overrides: ["month": .string("July")])],
            [billingPeriodRow(overrides: ["coverage": .string("unknown")])],
        ]
        var cases = [
            duplicate,
            billingMetrics(agentRows: [negative]),
            billingMetrics(agentRows: [validRow], attributed: 24_999),
            missing,
            mixedQuality,
        ]
        cases += badPeriods.map {
            billingMetrics(agentRows: [validRow], periodRows: $0)
        }
        for metrics in cases {
            XCTAssertNil(
                SnapshotMetrics(metrics).billingAttribution,
                "one malformed or inconsistent field must invalidate the bundle"
            )
        }
    }
    func testIntegerCentsFormattingIsExact() {
        XCTAssertEqual(StatsFormat.usdCents(27_000), "$270.00")
        XCTAssertEqual(StatsFormat.usdCents(2_000), "$20.00")
        XCTAssertEqual(StatsFormat.usdCents(1), "$0.01")
        XCTAssertEqual(StatsFormat.usdCents(nil), StatsFormat.unknown)
        XCTAssertEqual(StatsFormat.usdCents(-1), StatsFormat.unknown)
    }

    func testEmptyHarnessSnapshotYieldsUnknownsNotZeros() {
        let model = HarnessViewModel(metrics: SnapshotMetrics([]), hasSnapshot: false)
        XCTAssertEqual(model.weekMenuBarText, "未知")
        XCTAssertEqual(model.dayMenuBarText, "未知")
        XCTAssertEqual(model.completedMenuBarText, "未知")
        XCTAssertEqual(model.weekLamp, .gray)
        XCTAssertEqual(model.dayLamp, .gray)
        XCTAssertEqual(model.completionLamp, .gray)
        XCTAssertEqual(model.completionRateText, "未知")
        XCTAssertNil(model.maxStalledDays)
    }

    /// A metric present but flagged `unknown` must not be read as a value.
    func testUnavailableMetricStatusIsTreatedAsMissing() {
        let metrics = SnapshotMetrics([
            Metric(
                id: MetricID.outputTokens,
                capabilityID: "s1.request_trace_capture",
                status: .unknown,
                value: .number(0),
                unit: "tokens",
                window: "today_utc",
                quality: .unavailable,
                scope: "x"
            ),
        ])
        XCTAssertNil(metrics.int(MetricID.outputTokens))
        XCTAssertEqual(RelayViewModel(metrics: metrics, hasSnapshot: true).ccMenuBarText, "未知")
    }

    func testWrongLengthHourlySeriesIsRejectedRatherThanPadded() {
        let metrics = SnapshotMetrics([
            Metric(
                id: MetricID.hourlyOutputTokens,
                capabilityID: "s1.request_trace_capture",
                status: .available,
                value: .numbers([1, 2, 3]),
                unit: "tokens",
                window: "today_utc",
                quality: .validated,
                scope: "x"
            ),
        ])
        XCTAssertNil(metrics.hourlyOutputTokens, "padding to 24 would invent hours we never measured")
    }

    /// An empty list is a measurement ("nobody is stuck"); a missing list is not.
    func testEmptyStalledListDiffersFromMissingStalledList() {
        let measured = SnapshotMetrics([
            Metric(
                id: MetricID.wiStalled,
                capabilityID: "s2.ledger_invariants",
                status: .available,
                value: .rows([]),
                unit: "list",
                window: "current",
                quality: .validated,
                scope: "x"
            ),
        ])
        XCTAssertEqual(measured.stalledWorkItems, [])
        XCTAssertNil(SnapshotMetrics([]).stalledWorkItems)
    }

    // MARK: wording

    /// The concern text is read by 大鸟. No metric ids, no capability ids, no
    /// English abbreviations — see auto-memory
    /// `cron-notify-three-tier-plain-language`.
    func testConcernWordingContainsNoInternalJargon() throws {
        let relay = RelayViewModel(metrics: try liveMetrics(), hasSnapshot: true)
        let harness = HarnessViewModel(metrics: try liveMetrics(), hasSnapshot: true)
        let texts = (relay.concerns + harness.concerns).flatMap { [$0.title, $0.detail ?? ""] }
        XCTAssertFalse(texts.isEmpty)

        let banned = [
            "s1.", "s2.", "capability", "metric", "harness.wi", "usage.today",
            "relay.cc", "findings", "invariants", "ledger", "handoff", "work_item",
            "unknown", "null", "wi_",
        ]
        for text in texts {
            for token in banned {
                XCTAssertFalse(
                    text.lowercased().contains(token.lowercased()),
                    "concern text leaked internal jargon '\(token)': \(text)"
                )
            }
        }
    }

    func testStalledConcernSaysHowManyAndHowLongInPlainWords() throws {
        let harness = HarnessViewModel(metrics: try liveMetrics(), hasSnapshot: true)
        let concern = try XCTUnwrap(harness.concerns.first { $0.key == "stalled_wi" })
        XCTAssertEqual(concern.title, "3 件活挂着超过一周没动")
        XCTAssertEqual(concern.detail, "最久的一件已经 12 天")
        XCTAssertEqual(concern.lamp, .amber)
    }

    func testStalledConcernTurnsRedPastFourteenDays() {
        let metrics = SnapshotMetrics([
            Metric(
                id: MetricID.wiStalled,
                capabilityID: "s2.ledger_invariants",
                status: .available,
                value: .rows([[
                    "work_item_id": .string("wi_1"),
                    "title": .string("t"),
                    "days": .number(20),
                ]]),
                unit: "list",
                window: "current",
                quality: .validated,
                scope: "x"
            ),
        ])
        let model = HarnessViewModel(metrics: metrics, hasSnapshot: true)
        XCTAssertEqual(model.concerns.first?.lamp, .red)
        XCTAssertEqual(model.maxStalledDays, 20)
    }

    func testCodexConcernAppearsOnlyAboveNinetyPercent() throws {
        let relay = RelayViewModel(metrics: try liveMetrics(), hasSnapshot: true)
        let concern = try XCTUnwrap(relay.concerns.first { $0.key == "codex_quota" })
        XCTAssertEqual(concern.title, "Codex 这周的额度告急")
        XCTAssertTrue(try XCTUnwrap(concern.detail).contains("98%"))
    }

    func testNoConcernsWhenEverythingIsFine() {
        let metrics = SnapshotMetrics([
            metric(MetricID.codexUsedPercent, .number(12), unit: "percent"),
            metric(MetricID.ccRateLimited, .boolean(false), unit: "boolean"),
            metric(MetricID.ccProcesses, .number(2), unit: "count"),
            metric(MetricID.failures, .number(0), unit: "count"),
        ])
        XCTAssertTrue(RelayViewModel(metrics: metrics, hasSnapshot: true).concerns.isEmpty)
    }

    // MARK: menu bar width

    private func assertWithinBudget(_ width: CGFloat, _ label: String) {
        XCTAssertLessThanOrEqual(
            width,
            MenuBarWidth.budget,
            "\(label): \(width)pt exceeds the \(MenuBarWidth.budget)pt budget"
        )
    }

    /// The v3 item on real production data.
    private func menuBarModel(
        _ metrics: SnapshotMetrics,
        tiers: AlertTiers = .zero,
        hasSnapshot: Bool = true,
        presentation: StatusPresentation? = nil
    ) -> MenuBarViewModel {
        MenuBarViewModel(
            metrics: metrics,
            alertTiers: tiers,
            hasSnapshot: hasSnapshot,
            presentation: presentation ?? StatusPresentation(
                runtimeHealth: hasSnapshot ? .ok : .unknown,
                coverage: hasSnapshot ? .full : .unknown
            )
        )
    }

    func testMenuBarWidthStaysWithinBudgetOnRealData() throws {
        let model = menuBarModel(try liveMetrics())
        assertWithinBudget(MenuBarWidth.merged(model), "real production data")
    }

    /// Unknowns are wider than digits — "未知" measures more than "51%".
    func testMenuBarWidthWithUnknownsStaysWithinBudget() {
        let model = menuBarModel(SnapshotMetrics([]), hasSnapshot: false)
        assertWithinBudget(MenuBarWidth.merged(model), "all unknown")
    }

    /// The widest the status word gets in practice: a three-digit act count.
    /// The observed steady state is 10 (spec §1 A2), so 100+ is headroom, not
    /// a prediction.
    func testMenuBarWidthAtWorstCaseStaysWithinBudget() {
        let metrics = SnapshotMetrics([
            metric(MetricID.ccUsedPercent, .number(100), unit: "percent"),
            metric(MetricID.codexUsedPercent, .number(100), unit: "percent"),
        ])
        let model = menuBarModel(metrics, tiers: AlertTiers(act: 128, watch: 0, done: 0))
        assertWithinBudget(MenuBarWidth.merged(model), "absolute worst case")
    }

    func testThreeDigitActionWithEveryStatusSuffixStaysWithinBudget() {
        let metrics = SnapshotMetrics([
            metric(MetricID.ccUsedPercent, .number(100), unit: "percent"),
            metric(MetricID.codexUsedPercent, .number(100), unit: "percent"),
        ])
        let presentations = [
            StatusPresentation(runtimeHealth: .stale, coverage: .full),
            StatusPresentation(runtimeHealth: .unknown, coverage: .full),
            StatusPresentation(runtimeHealth: .critical, coverage: .full),
            StatusPresentation(runtimeHealth: .warning, coverage: .full),
            StatusPresentation(runtimeHealth: .ok, coverage: .partial),
            StatusPresentation(runtimeHealth: .ok, coverage: .unknown),
            StatusPresentation(runtimeHealth: .warning, coverage: .unknown),
            StatusPresentation(runtimeHealth: .critical, coverage: .partial),
            StatusPresentation(runtimeHealth: .stale, coverage: .unknown),
        ]

        for presentation in presentations {
            let model = menuBarModel(
                metrics,
                tiers: AlertTiers(act: 128, watch: 0, done: 0),
                presentation: presentation
            )
            let label = "\(presentation.runtimeHealth.rawValue)"
                + "/\(presentation.coverage.rawValue)"
            assertWithinBudget(MenuBarWidth.merged(model), label)

            XCTAssertFalse(model.compactTitle.contains("CC"))
            XCTAssertFalse(model.compactTitle.contains("CODEX"))
            XCTAssertTrue(model.compactTitle.hasPrefix("PULSE"))
            XCTAssertFalse(model.compactTitle.contains("覆盖"))

        }
    }

    // MARK: menu bar model (spec v3 §3)

    /// v3 §3 is the one quota contract: menu bar, panel rows and concerns must
    /// not assign different colours or urgency words to the same reading.
    func testQuotaBoundariesAgreeAcrossRuleMenuPanelAndConcerns() {
        let cases: [(Double?, StatusLamp)] = [
            (nil, .gray),
            (89, .green),
            (90, .green),
            (95, .amber),
            (96, .red),
            (100, .red),
            (140, .red),
        ]

        for (usedPercent, expectedLamp) in cases {
            let metrics = SnapshotMetrics(
                usedPercent.map {
                    [
                        metric(MetricID.ccUsedPercent, .number($0), unit: "percent"),
                        metric(MetricID.codexUsedPercent, .number($0), unit: "percent"),
                    ]
                } ?? []
            )
            let relay = RelayViewModel(metrics: metrics, hasSnapshot: true)
            let menu = menuBarModel(metrics)
            let ccConcern = relay.concerns.first { $0.key == "cc_quota" }
            let codexConcern = relay.concerns.first { $0.key == "codex_quota" }

            XCTAssertEqual(LampRules.quota(usedPercent: usedPercent), expectedLamp)
            XCTAssertEqual(menu.ccLamp, expectedLamp, "CC menu at \(String(describing: usedPercent))")
            XCTAssertEqual(menu.codexLamp, expectedLamp, "Codex menu at \(String(describing: usedPercent))")
            XCTAssertEqual(relay.ccLamp, expectedLamp, "CC panel at \(String(describing: usedPercent))")
            XCTAssertEqual(relay.codexLamp, expectedLamp, "Codex panel at \(String(describing: usedPercent))")

            if expectedLamp == .amber || expectedLamp == .red {
                XCTAssertEqual(ccConcern?.lamp, expectedLamp)
                XCTAssertEqual(codexConcern?.lamp, expectedLamp)
                let ccExpectedTitle: String
                let codexExpectedTitle: String
                if let usedPercent, usedPercent >= 100 {
                    ccExpectedTitle = "Claude 这周的额度用完了"
                    codexExpectedTitle = "Codex 这周的额度用完了"
                } else if expectedLamp == .red {
                    ccExpectedTitle = "Claude 这周的额度告急"
                    codexExpectedTitle = "Codex 这周的额度告急"
                } else {
                    ccExpectedTitle = "Claude 这周的额度快用完了"
                    codexExpectedTitle = "Codex 这周的额度快用完了"
                }
                XCTAssertEqual(ccConcern?.title, ccExpectedTitle)
                XCTAssertEqual(codexConcern?.title, codexExpectedTitle)
            } else {
                XCTAssertNil(ccConcern)
                XCTAssertNil(codexConcern)
            }
        }
    }

    func testCCQuotaLampDoesNotMixInProcessOrRateLimitHealth() {
        let quotaUnknownButLive = SnapshotMetrics([
            metric(MetricID.ccProcesses, .number(3), unit: "count"),
            metric(MetricID.ccRateLimited, .boolean(false), unit: "boolean"),
        ])
        let liveRelay = RelayViewModel(metrics: quotaUnknownButLive, hasSnapshot: true)
        XCTAssertEqual(menuBarModel(quotaUnknownButLive).ccLamp, .gray)
        XCTAssertEqual(liveRelay.ccLamp, .gray)
        XCTAssertEqual(liveRelay.ccLivenessLamp, .green)

        let healthyQuotaButRateLimited = SnapshotMetrics([
            metric(MetricID.ccUsedPercent, .number(89), unit: "percent"),
            metric(MetricID.ccProcesses, .number(3), unit: "count"),
            metric(MetricID.ccRateLimited, .boolean(true), unit: "boolean"),
        ])
        let limitedRelay = RelayViewModel(metrics: healthyQuotaButRateLimited, hasSnapshot: true)
        XCTAssertEqual(menuBarModel(healthyQuotaButRateLimited).ccLamp, .green)
        XCTAssertEqual(limitedRelay.ccLamp, .green)
        XCTAssertEqual(limitedRelay.ccLivenessLamp, .red)
        XCTAssertEqual(
            limitedRelay.concerns.first { $0.key == "cc_rate_limited" }?.lamp,
            .red
        )
    }

    /// Spec §3 "两条线各管各的": a maxed-out quota must not make the status
    /// word red. 额度烧完不等于有事要人干.
    func testExhaustedQuotaStillReadsNormalWhenNothingNeedsAction() {
        let metrics = SnapshotMetrics([
            metric(MetricID.ccUsedPercent, .number(100), unit: "percent"),
            metric(MetricID.codexUsedPercent, .number(100), unit: "percent"),
        ])
        let model = menuBarModel(metrics, tiers: .zero)
        XCTAssertEqual(model.ccLamp, .red)
        XCTAssertEqual(model.statusLamp, .green)
        XCTAssertEqual(model.statusText, "正常")
    }

    func testActCountDrivesStatusWord() {
        let model = menuBarModel(SnapshotMetrics([]), tiers: AlertTiers(act: 3, watch: 60, done: 74))
        XCTAssertEqual(model.statusLamp, .red)
        XCTAssertEqual(model.statusText, "3 件要出手")
    }

    /// Only `act` reaches the bar. 60 watching + 74 resolved is a quiet bar.
    func testWatchAndDoneDoNotReachTheMenuBar() {
        let model = menuBarModel(SnapshotMetrics([]), tiers: AlertTiers(act: 0, watch: 60, done: 74))
        XCTAssertEqual(model.statusLamp, .green)
        XCTAssertEqual(model.statusText, "正常")
    }

    func testRuntimeWarningDrivesMenuBarStatus() {
        let model = menuBarModel(
            SnapshotMetrics([]),
            presentation: StatusPresentation(runtimeHealth: .warning, coverage: .full)
        )
        XCTAssertEqual(model.statusLamp, .amber)
        XCTAssertEqual(model.statusText, "运行有异常")
    }

    /// The menu bar carries no coverage axis: partial coverage still reads 「正常」.
    func testPartialCoverageDoesNotReachTheMenuBar() {
        let model = menuBarModel(
            SnapshotMetrics([]),
            presentation: StatusPresentation(runtimeHealth: .ok, coverage: .partial)
        )
        XCTAssertEqual(model.statusLamp, .green)
        XCTAssertEqual(model.statusText, "正常")
    }

    /// 运行轴照旧进菜单栏——摘掉的只有覆盖轴，别把两件事一起摘了。
    func testRuntimeIssueStillReachesTheMenuBarAlongsidePartialCoverage() {
        let model = menuBarModel(
            SnapshotMetrics([]),
            presentation: StatusPresentation(runtimeHealth: .warning, coverage: .partial)
        )
        XCTAssertEqual(model.statusLamp, .amber)
        XCTAssertEqual(model.statusText, "运行有异常")
    }

    /// No snapshot must not read as "正常" — that would be dressing an unknown
    /// up as a green, which the lamp rules exist to prevent.
    func testNoSnapshotReadsUnknownNotNormal() {
        let model = menuBarModel(SnapshotMetrics([]), hasSnapshot: false)
        XCTAssertEqual(model.statusLamp, .gray)
        XCTAssertEqual(model.statusText, "没数据")
    }

    // MARK: helper

    private func billingMetric(
        _ id: String,
        _ value: MetricValue,
        status: MetricStatus = .available,
        quality: MetricQuality = .validated,
        sampleCount: Int? = nil,
        reason: String? = nil
    ) -> Metric {
        Metric(
            id: id,
            capabilityID: "s1.request_trace_capture",
            status: status,
            value: value,
            unit: id.hasSuffix("_cents") ? "cents" : "list",
            window: "month_to_date_cst",
            sampleCount: sampleCount,
            quality: quality,
            scope: "test",
            reason: reason
        )
    }

    private func billingComponentRows() -> [[String: MetricScalar]] {
        [
            [
                "component_id": .string("claude_code"),
                "amount_cents": .number(25_000),
                "currency": .string("USD"),
                "attribution": .string("cc_mtd_output_share"),
            ],
            [
                "component_id": .string("codex"),
                "amount_cents": .number(2_000),
                "currency": .string("USD"),
                "attribution": .string("unattributed_no_agent_source"),
            ],
        ]
    }

    private func billingFactMetrics(
        total: Double = 27_000,
        componentRows: [[String: MetricScalar]]? = nil
    ) -> [Metric] {
        [
            billingMetric(
                MetricID.billingComponents,
                .rows(componentRows ?? billingComponentRows())
            ),
            billingMetric(MetricID.billingTotalCents, .number(total)),
        ]
    }

    private func billingMetrics(
        agentRows: [[String: MetricScalar]],
        periodRows: [[String: MetricScalar]]? = nil,
        attributed: Double = 25_000
    ) -> [Metric] {
        billingFactMetrics() + [
            billingMetric(
                MetricID.billingPeriod,
                .rows(periodRows ?? [billingPeriodRow()]),
                quality: .descriptive, sampleCount: 1
            ),
            billingMetric(
                MetricID.billingByAgent, .rows(agentRows),
                quality: .descriptive, sampleCount: 1
            ),
            billingMetric(
                MetricID.billingAttributedCents, .number(attributed),
                quality: .descriptive, sampleCount: 1
            ),
            billingMetric(
                MetricID.billingUnattributedCents, .number(2_000),
                quality: .descriptive, sampleCount: 1
            ),
        ]
    }

    private func billingPeriodRow(
        overrides: [String: MetricScalar] = [:]
    ) -> [String: MetricScalar] {
        var row: [String: MetricScalar] = [
            "month": .string("2026-07"),
            "timezone": .string("Asia/Shanghai"),
            "days_covered": .number(0),
            "days_expected": .number(25),
            "coverage": .string("partial"),
        ]
        row.merge(overrides) { _, latest in latest }
        return row
    }

    private func metric(_ id: String, _ value: MetricValue, unit: String) -> Metric {
        Metric(
            id: id,
            capabilityID: "s1.request_trace_capture",
            status: .available,
            value: value,
            unit: unit,
            window: "current",
            quality: .validated,
            scope: "test"
        )
    }

    // MARK: 在办看板

    private func boardMetric(_ rows: [[String: MetricScalar]]) -> SnapshotMetrics {
        SnapshotMetrics([metric(MetricID.workBoard, .rows(rows), unit: "list")])
    }

    private func boardEntry(_ id: String, agent: String, kind: String = "wi", stuck: Bool = false,
                            workItem: String? = nil) -> [String: MetricScalar] {
        ["kind": .string(kind), "id": .string(id), "agent": .string(agent), "line": .string("第 \(id) 件"),
         "status": .string(stuck ? "10 天没推进" : "3 时前"), "stuck": .boolean(stuck), "main_only": .boolean(kind == "contract"),
         "work_item_id": .string(workItem ?? id)]
    }

    private func boardAgent(_ agent: String, summary: String = "") -> [String: MetricScalar] {
        ["kind": .string("agent"), "id": .string("agent:" + agent), "agent": .string(agent),
         "name": .string(agent.prefix(1).uppercased() + agent.dropFirst()), "summary": .string(summary)]
    }

    private func boardHead(wi: Int, contracts: Int, available: Bool = true) -> [String: MetricScalar] {
        ["kind": .string("board"), "id": .string("board"), "wi_total": .number(Double(wi)),
         "contract_total": .number(Double(contracts)), "contracts_available": .boolean(available)]
    }

    /// 计数守恒：每件 WI、每张合同恰好一行；合并的单件 Agent 也不丢行。
    func testWorkBoardKeepsEveryItemAndMergesQuietSingleAgents() {
        let model = WorkBoardViewModel(metrics: boardMetric([
            boardHead(wi: 5, contracts: 1),
            boardAgent("pulse", summary: "待接合同 1（5 天）"),
            boardEntry("op_wait", agent: "pulse", kind: "contract", stuck: true, workItem: "wi_platform"),
            boardEntry("wi_platform", agent: "pulse"),
            boardAgent("amanda", summary: "最近 7 时前"), boardEntry("wi_a1", agent: "amanda"), boardEntry("wi_a2", agent: "amanda"),
            boardAgent("ken"), boardEntry("wi_ken", agent: "ken"),
            boardAgent("duke"), boardEntry("wi_duke", agent: "duke"),
        ]), actWorkItemIDs: ["wi_platform"])
        let groups = try? XCTUnwrap(model.groups)
        XCTAssertEqual(groups?.map(\.title), ["Pulse", "Amanda", "Ken · Duke"])
        XCTAssertEqual(groups?.map(\.countText), ["WI 1", "WI 2", nil])
        XCTAssertEqual(groups?.last?.summary, "WI 各 1 · 均在推进")
        XCTAssertEqual(groups?.flatMap(\.entries).count, 6)
        XCTAssertEqual(groups?.first?.stuck, true)
        XCTAssertEqual(model.countText, "WI 5 · 合同 1")
        XCTAssertEqual(model.stuckCount, 1)
        // 「要你出手」里的 WI 只加标记；同号合同不算同一件事。
        let pulse = groups?.first?.entries ?? []
        XCTAssertEqual(pulse.map(\.inAct), [false, true])
        XCTAssertEqual(pulse.first?.mainOnly, true)
    }

    /// 行数与总数对不上、行不归属任何 Agent、或读不到时，不能显示成“没有在办”。
    func testWorkBoardRejectsLossyOrMissingRows() {
        let lossy = WorkBoardViewModel(metrics: boardMetric([
            boardHead(wi: 2, contracts: 0), boardAgent("pulse"), boardEntry("wi_one", agent: "pulse"),
        ]))
        XCTAssertNil(lossy.groups)
        XCTAssertNil(lossy.countText)
        let orphan = WorkBoardViewModel(metrics: boardMetric([
            boardHead(wi: 1, contracts: 0), boardAgent("pulse"), boardEntry("wi_one", agent: "aki"),
        ]))
        XCTAssertNil(orphan.groups)
        XCTAssertNil(WorkBoardViewModel(metrics: SnapshotMetrics([])).groups)
        let empty = WorkBoardViewModel(metrics: boardMetric([boardHead(wi: 0, contracts: 0)]))
        XCTAssertEqual(empty.groups, [])
        let noContracts = WorkBoardViewModel(metrics: boardMetric([
            boardHead(wi: 1, contracts: 0, available: false), boardAgent("pulse"), boardEntry("wi_one", agent: "pulse"),
        ]))
        XCTAssertEqual(noContracts.countText, "WI 1 · 合同 待核验")
    }

    // MARK: 在飞 WI 行（要你出手的 WI 身份与创建时长）

    /// 造一件在飞的活。
    private func inFlightRow(_ id: String, days: Double = 3, owner: String = "pulse") -> [String: MetricScalar] {
        ["work_item_id": .string(id), "title": .string("第 \(id) 件"), "status": .string("in_progress"),
         "days": .number(days), "owner": .string(owner)]
    }
}

/// The 三段式 panel's content model (spec v3 §4).
final class TriageViewModelTests: XCTestCase {
    /// A `reason` in the shape the exporter actually emits, taken verbatim
    /// from a 2026-07-26 production snapshot rather than invented.
    private static let productionReason =
        "07-19 11:46 起 · 最近 07-25 17:45 · 12 次 · pulse"
            + "｜发生什么：dual-heartbeat-watcher.service 非零退出。影响：ack/reply/lint 暂时无人盯。"
            + "行动：查 journalctl --user -u dual-heartbeat-watcher.service -n 120"

    private func alert(
        _ id: String,
        tier: AlertTier,
        title: String = "标题",
        reason: String = productionReason,
        occurredAt: Date = Date(timeIntervalSince1970: 1_784_000_000)
    ) -> Alert {
        Alert(
            eventID: id,
            projectionStatus: "ok",
            severity: .warning,
            capabilityID: "s2.escalation",
            title: title,
            occurredAt: occurredAt,
            reason: reason,
            tier: tier
        )
    }

    private func incidentJSON(
        _ id: String,
        state: String,
        severity: String = "warning",
        updatedAt: String = "2026-07-30T06:00:00Z"
    ) -> [String: Any] {
        [
            "incident_key": "capability:condition:\(id)",
            "episode_id": "\(id)#episode-1",
            "state": state,
            "severity": severity,
            "capability_id": "s2.ledger_invariants",
            "scope": id,
            "title": "事故 \(id)",
            "what_happened": "发生了 \(id)",
            "impact": "影响 \(id)",
            "suggested_action": "处理 \(id)",
            "opened_at": "2026-07-30T05:00:00Z",
            "updated_at": updatedAt,
            "resolved_at": state == "resolved" ? updatedAt : NSNull(),
            "occurrence_count": 1,
            "reopen_count": 0,
            "evidence": ["event:\(id)"],
            "recovery_evidence": state == "resolved" ? ["event:recovery-\(id)"] : [],
        ]
    }

    private func snapshot(incidents: [[String: Any]], openCount: Int) throws -> PulseSnapshot {
        let json = try JSONSerialization.jsonObject(with: fixtureData("ok-full"))
        var root = try XCTUnwrap(json as? [String: Any])
        var overall = try XCTUnwrap(root["overall"] as? [String: Any])
        overall["open_incident_count"] = openCount
        root["overall"] = overall
        root["incidents"] = incidents
        return try SnapshotDecoder().decode(JSONSerialization.data(withJSONObject: root))
    }

    // MARK: open incidents

    func testNoOpenIncidentsProducesNoVisibleIncidentCards() throws {
        let model = TriageViewModel(snapshot: try snapshot(incidents: [], openCount: 0))

        XCTAssertTrue(model.openIncidents.isEmpty)
    }

    func testOneOpenIncidentProducesOneActionableCard() throws {
        let snapshot = try snapshot(
            incidents: [incidentJSON("only", state: "open")],
            openCount: 1
        )
        let model = TriageViewModel(snapshot: snapshot)
        let incident = try XCTUnwrap(model.openIncidents.first)

        XCTAssertEqual(model.openIncidents.count, snapshot.overall.openIncidentCount)
        XCTAssertEqual(incident.title, "事故 only")
        XCTAssertEqual(incident.impact, "影响 only")
        XCTAssertNotNil(incident.updatedAt)
        XCTAssertEqual(incident.suggestedAction, "处理 only")
    }

    func testOpenIncidentsExcludeResolvedAndSortBySeverityThenRecentUpdate() throws {
        let snapshot = try snapshot(
            incidents: [
                incidentJSON(
                    "warning-newest",
                    state: "open",
                    severity: "warning",
                    updatedAt: "2026-07-30T09:00:00Z"
                ),
                incidentJSON(
                    "critical-older",
                    state: "open",
                    severity: "critical",
                    updatedAt: "2026-07-30T07:00:00Z"
                ),
                incidentJSON(
                    "critical-newer",
                    state: "open",
                    severity: "critical",
                    updatedAt: "2026-07-30T08:00:00Z"
                ),
                incidentJSON(
                    "resolved-latest",
                    state: "resolved",
                    severity: "critical",
                    updatedAt: "2026-07-30T10:00:00Z"
                ),
                // Same severity + timestamp, deliberately reverse input order:
                // the incident identity key is the stable final tie-break.
                incidentJSON(
                    "info-b",
                    state: "open",
                    severity: "info",
                    updatedAt: "2026-07-30T06:00:00Z"
                ),
                incidentJSON(
                    "info-a",
                    state: "open",
                    severity: "info",
                    updatedAt: "2026-07-30T06:00:00Z"
                ),
            ],
            openCount: 5
        )
        let model = TriageViewModel(snapshot: snapshot)

        XCTAssertEqual(model.openIncidents.count, snapshot.overall.openIncidentCount)
        XCTAssertEqual(
            model.openIncidents.map(\.title),
            [
                "事故 critical-newer",
                "事故 critical-older",
                "事故 warning-newest",
                "事故 info-a",
                "事故 info-b",
            ]
        )
        XCTAssertFalse(model.openIncidents.contains { $0.state == .resolved })
    }

    func testMissingDisplayFieldsStayVisibleAndUnknownStateStaysOut() throws {
        var missing = incidentJSON("missing", state: "open")
        missing.removeValue(forKey: "title")
        missing["impact"] = ""
        missing.removeValue(forKey: "suggested_action")
        missing.removeValue(forKey: "updated_at")
        let unknownState = incidentJSON("future", state: "paused")
        let knownTime = incidentJSON("known-time", state: "open")
        let snapshot = try snapshot(
            incidents: [missing, unknownState, knownTime],
            openCount: 2
        )
        let model = TriageViewModel(snapshot: snapshot)
        let incident = try XCTUnwrap(model.openIncidents.last)

        XCTAssertEqual(model.openIncidents.count, snapshot.overall.openIncidentCount)
        XCTAssertEqual(model.openIncidents.first?.title, "事故 known-time")
        XCTAssertEqual(incident.title, "未知事故")
        XCTAssertEqual(incident.impact, "影响未知")
        XCTAssertNil(incident.updatedAt)
        XCTAssertEqual(incident.suggestedAction, "建议动作未知")
        XCTAssertTrue(snapshot.incidents.contains { $0.state == .unknown })
    }

    func testUnknownStateCannotHideAReportedOpenIncidentCount() {
        XCTAssertThrowsError(
            try snapshot(
                incidents: [incidentJSON("future", state: "paused")],
                openCount: 1
            )
        ) { error in
            XCTAssertEqual(
                error as? SnapshotDecodeError,
                .invalidPayload("open_incident_count does not match incidents")
            )
        }
    }

    // MARK: reason splitting

    /// The metadata half and the escalation's own words render at different
    /// weights, so the split has to land in the right place.
    func testSplitsReasonAtTheFullWidthBar() {
        let triaged = TriagedAlert(alert("e1", tier: .act))
        XCTAssertEqual(triaged.meta, "07-19 11:46 起 · 最近 07-25 17:45 · 12 次 · pulse")
        XCTAssertTrue(triaged.body.hasPrefix("发生什么："))
        XCTAssertFalse(triaged.body.contains("｜"), "the separator itself must not survive into the body")
    }

    /// A pre-S1 reason has no separator at all; the whole string is the body
    /// and there is simply no metadata line.
    func testReasonWithoutSeparatorBecomesBodyOnly() {
        let triaged = TriagedAlert(alert("e2", tier: .watch, reason: "autonomous session wrap_missing"))
        XCTAssertNil(triaged.meta)
        XCTAssertEqual(triaged.body, "autonomous session wrap_missing")
    }

    /// A trailing separator with nothing after it must not produce an empty
    /// card — the text that *is* there stays visible.
    func testReasonWithEmptyTailKeepsTheTextVisible() {
        let triaged = TriagedAlert(alert("e3", tier: .watch, reason: "07-19 11:46 起 · 2 次｜"))
        XCTAssertNil(triaged.meta)
        XCTAssertEqual(triaged.body, "07-19 11:46 起 · 2 次")
    }

    /// Only the first separator splits: an escalation message may contain more.
    func testOnlyTheFirstSeparatorSplits() {
        let triaged = TriagedAlert(alert("e4", tier: .act, reason: "元信息｜正文 A｜正文 B"))
        XCTAssertEqual(triaged.meta, "元信息")
        XCTAssertEqual(triaged.body, "正文 A｜正文 B")
    }

    // MARK: grouping

    private func snapshot(
        alerts: [Alert],
        tiers: AlertTiers,
        runtimeHealth: RuntimeHealth = .ok,
        coverage: Coverage = .full,
        capabilities: [Capability] = [],
        globalAuthority: GlobalAuthorityProjection? = nil
    ) -> PulseSnapshot {
        PulseSnapshot(
            schemaVersion: PulseSnapshot.supportedSchemaVersion,
            generatedAt: Date(timeIntervalSince1970: 1_784_000_000),
            expiresAt: Date(timeIntervalSince1970: 1_784_000_180),
            baselineVersion: "test",
            overall: OverallStatus(
                runtimeHealth: runtimeHealth,
                coverage: coverage,
                openIncidentCount: 0,
                headline: "test"
            ),
            capabilities: capabilities,
            incidents: [],
            metrics: [],
            alerts: alerts,
            alertTiers: tiers,
            provenance: Provenance(
                producer: Producer(name: "pulse-stats-snapshot", version: "0.1.0"),
                sourceRevisions: [:],
                sources: []
            ),
            globalAuthority: globalAuthority
        )
    }

    func testGlobalAuthorityPresentationFailsClosedWithoutCarrier() {
        let model = TriageViewModel(snapshot: snapshot(alerts: [], tiers: .zero))

        XCTAssertEqual(model.authority.lamp, .gray)
        XCTAssertEqual(model.authority.title, "WI 账本未核验")
        XCTAssertFalse(model.authority.title.contains("全局"))
        XCTAssertFalse(model.authority.detail.contains("validated"))
    }

    func testGlobalAuthorityPresentationNamesTheValidatedWILedger() {
        let authority = GlobalAuthorityProjection(
            schemaVersion: "pulse_stats_global_read_model/v1",
            status: "validated",
            scope: "global",
            authorityRevision: "sha256:4a3acd1cb3af2c3bb72b3c74cb861e4d1ab66947e089688206e33c200af7e21b",
            requiredEndpointCount: 4,
            materializedEndpointCount: 4,
            materializationDigest: "sha256:b42272a4d8de947fd1f2448e1404bace265f0e89245cd0048c2f716da44628b3",
            freshness: "fresh",
            degradedReasons: []
        )
        let model = TriageViewModel(
            snapshot: snapshot(alerts: [], tiers: .zero, globalAuthority: authority)
        )

        XCTAssertEqual(model.authority.lamp, .green)
        XCTAssertEqual(model.authority.title, "WI 账本已核验")
        XCTAssertEqual(model.authority.detail, "WI 账本已完整读取，与本次快照为同一版本。\nrev 4a3acd1c")
        XCTAssertFalse(model.authority.detail.contains("端"))
    }

    func testUnsupportedNestedAuthoritySchemaCannotClaimValidatedGlobalTruth() {
        let authority = GlobalAuthorityProjection(
            schemaVersion: "pulse_stats_global_read_model/v2",
            status: "validated",
            scope: "global",
            authorityRevision: "sha256:4a3acd1cb3af2c3bb72b3c74cb861e4d1ab66947e089688206e33c200af7e21b",
            requiredEndpointCount: 4,
            materializedEndpointCount: 4,
            materializationDigest: "sha256:b42272a4d8de947fd1f2448e1404bace265f0e89245cd0048c2f716da44628b3",
            freshness: "fresh",
            degradedReasons: []
        )
        let model = TriageViewModel(
            snapshot: snapshot(alerts: [], tiers: .zero, globalAuthority: authority)
        )

        XCTAssertNotEqual(model.authority.lamp, .green)
        XCTAssertFalse(model.authority.title.contains("已核验"))
    }

    private func capability(_ id: String, status: ImplementationStatus) -> Capability {
        Capability(
            id: id,
            implementationStatus: status,
            health: status == .retired ? .retired : .ok,
            aggregationRole: .coverageOnly,
            scope: "test",
            reason: "test",
            freshness: Freshness(
                state: status == .retired || status == .notImplemented ? .notApplicable : .fresh,
                observedAt: nil,
                maxAgeSeconds: nil
            ),
            evidence: ["fixture:\(id)"]
        )
    }

    func testSplitsAlertsIntoThreeSections() {
        let model = TriageViewModel(
            snapshot: snapshot(
                alerts: [
                    alert("a", tier: .act),
                    alert("b", tier: .watch),
                    alert("c", tier: .done),
                    alert("d", tier: .watch),
                ],
                tiers: AlertTiers(act: 1, watch: 2, done: 1)
            )
        )
        XCTAssertEqual(model.act.map(\.id), ["a"])
        XCTAssertEqual(Set(model.watch.map(\.id)), ["b", "d"])
        XCTAssertEqual(model.done.map(\.id), ["c"])
    }

    /// Newest first: "「要你出手」是关于现在的，不是关于历史的" (spec §2).
    func testSectionsAreNewestFirst() {
        let old = Date(timeIntervalSince1970: 1_784_000_000)
        let recent = Date(timeIntervalSince1970: 1_784_900_000)
        let model = TriageViewModel(
            snapshot: snapshot(
                alerts: [
                    alert("old", tier: .act, occurredAt: old),
                    alert("new", tier: .act, occurredAt: recent),
                ],
                tiers: AlertTiers(act: 2, watch: 0, done: 0)
            )
        )
        XCTAssertEqual(model.act.map(\.id), ["new", "old"])
    }

    /// The count reads the producer's counter, not the array length —
    /// spec §6 "App 端不自己数". The header keeps the red lamp but no words.
    func testHeaderKeepsLampButNotActCount() {
        let model = TriageViewModel(
            snapshot: snapshot(alerts: [], tiers: AlertTiers(act: 10, watch: 60, done: 74))
        )
        XCTAssertEqual(model.actCount, 10)
        XCTAssertNil(model.statusText)
        XCTAssertEqual(model.statusLamp, .red)
        let warning = TriageViewModel(
            snapshot: snapshot(alerts: [], tiers: AlertTiers(act: 4, watch: 0, done: 0), runtimeHealth: .warning)
        )
        XCTAssertEqual(warning.statusText, "运行有异常")
        XCTAssertEqual(warning.statusLamp, .red)
    }

    func testRuntimeWarningNeverReadsNormal() {
        let model = TriageViewModel(
            snapshot: snapshot(
                alerts: [],
                tiers: .zero,
                runtimeHealth: .warning
            )
        )
        XCTAssertEqual(model.statusLamp, .amber)
        XCTAssertEqual(model.statusText, "运行有异常")
    }

    /// The panel header matches the menu bar; coverage stays in its own section.
    func testPanelHeaderOmitsMonitoringCoverage() {
        for coverage in [Coverage.partial, .unknown] {
            let model = TriageViewModel(
                snapshot: snapshot(
                    alerts: [],
                    tiers: .zero,
                    coverage: coverage
                )
            )
            XCTAssertEqual(model.statusLamp, .green)
            XCTAssertEqual(model.statusText, "正常")
        }
    }

    func testNoSnapshotIsUnknownNotNormal() {
        let model = TriageViewModel(snapshot: nil)
        XCTAssertEqual(model.statusLamp, .gray)
        XCTAssertFalse(model.hasSnapshot)
        XCTAssertEqual(model.capabilityFooter, "能力清单还没拿到")
    }

    func testCapabilityFooterPartitionsDynamicInventoryIntoFourGroups() {
        let model = TriageViewModel(
            snapshot: snapshot(
                alerts: [],
                tiers: .zero,
                capabilities: [
                    capability("validated-a", status: .activeValidated),
                    capability("validated-b", status: .activeValidated),
                    capability("partial", status: .activePartial),
                    capability("not-implemented", status: .notImplemented),
                    capability("retired", status: .retired),
                ]
            )
        )
        XCTAssertEqual(model.capabilityTotal, 5)
        XCTAssertEqual(model.capabilityValidated, 2)
        XCTAssertEqual(model.capabilityPartial, 1)
        XCTAssertEqual(model.capabilityNotImplemented, 1)
        XCTAssertEqual(model.capabilityRetired, 1)
        XCTAssertEqual(
            model.capabilityValidated
                + model.capabilityPartial
                + model.capabilityNotImplemented
                + model.capabilityRetired,
            model.capabilityTotal
        )
        XCTAssertEqual(
            model.capabilityFooter,
            "5 项能力：2 项已验证 · 1 项部分接入 · 1 项未实现 · 1 项已退役"
        )
    }

    func testCapabilityFooterShowsAllFourZeroGroupsForMeasuredEmptyInventory() {
        let model = TriageViewModel(snapshot: snapshot(alerts: [], tiers: .zero))
        XCTAssertEqual(
            model.capabilityFooter,
            "0 项能力：0 项已验证 · 0 项部分接入 · 0 项未实现 · 0 项已退役"
        )
    }
}


extension PanelViewModelTests {
    private func oikosRows() -> [[String: MetricScalar]] {
        ["C1-ownermac", "tcloud-node2", "C3-ecs", "bigbird-sg"].map { id in
            ["id": .string(id), "label": .string(id),
             "role": .string(["C3-ecs", "bigbird-sg"].contains(id) ? "runner" : "resident"), "status": .string("ok"),
             "disk_percent": .number(69), "memory_available_percent": .number(27),
             "swap_percent": .number(89), "inode_percent": .number(0), "writable": .string("ok"),
             "runner_state": .string("inactive"), "runner_expected": .string("inactive"),
             "confirmation_runs": .number(0), "delivery": .string("not_needed"), "reason": .string(""),
             "observed_at": .string("2026-09-18T00:00:00Z"), "expires_at": .string("2026-09-18T00:25:00Z"),
             "source_revision": .string(String(repeating: "a", count: 40))]
        }
    }
    private func oikosModel(_ rows: [[String: MetricScalar]], at stamp: String = "2026-09-18T00:01:00Z") -> OikosPlatformViewModel {
        OikosPlatformViewModel(metrics: SnapshotMetrics([metric("oikos.platform.nodes", .rows(rows), unit: "nodes")]), now: OikosNode.date(stamp)!)
    }
    func testOikosReadsProducerStatesWithoutTreatingHighSwapOrDisabledRunnerAsIncident() {
        let model = oikosModel(oikosRows())
        XCTAssertTrue(model.available); XCTAssertEqual(model.confirmedCount, 0)
        XCTAssertEqual(model.lamp, .green); XCTAssertEqual(model.nodes.first?.swap, 89)
    }
    func testOikosAcceptsTheAddedSv3NodeAndCountsResidents() {
        var rows = oikosRows(); var sv3 = rows[0]
        sv3["id"] = .string("tcloud-node3"); sv3["label"] = .string("tcloud-node3")
        rows.append(sv3)
        let model = oikosModel(rows)
        XCTAssertTrue(model.available); XCTAssertEqual(model.nodes.count, 5)
        XCTAssertTrue(model.reason.hasPrefix("3 个常驻节点 + Runner 池（2 台）"))
        XCTAssertTrue(oikosModel(oikosRows()).reason.hasPrefix("2 个常驻节点 + Runner 池（2 台）"))
        var unknown = rows; unknown[4]["id"] = .string("unregistered")
        XCTAssertFalse(oikosModel(unknown).available)
    }
    func testOikosCapacityBoundaryAndPendingAreNotConfirmedIncidents() {
        var rows = oikosRows(); rows[0]["status"] = .string("at_limit"); rows[1]["status"] = .string("pending")
        let model = oikosModel(rows)
        XCTAssertEqual(model.lamp, .amber); XCTAssertEqual(model.confirmedCount, 0)
    }
    func testOikosConfirmedFailureAndUndeliveredRouteRemainVisible() {
        var rows = oikosRows(); rows[0]["status"] = .string("incident"); rows[0]["delivery"] = .string("not_delivered")
        rows[0]["confirmation_runs"] = .number(3)
        let model = oikosModel(rows)
        XCTAssertEqual(model.confirmedCount, 1); XCTAssertEqual(model.lamp, .red)
        XCTAssertTrue(model.nodes[0].deliveryText.contains("尚未送达"))
    }
    func testOikosExpiredCacheMissingNodeDuplicateAndInvalidStateNeverTurnGreen() {
        XCTAssertFalse(oikosModel(oikosRows(), at: "2026-09-18T00:25:00Z").available)
        XCTAssertFalse(oikosModel(Array(oikosRows().dropLast())).available)
        var rows = oikosRows(); rows[0] = rows[1]
        XCTAssertFalse(oikosModel(rows).available)
        rows = oikosRows(); rows[0]["status"] = .string("future-state")
        XCTAssertEqual(oikosModel(rows).lamp, .gray)
        let missing = OikosPlatformViewModel(metrics: SnapshotMetrics([]), now: Date())
        XCTAssertFalse(missing.available); XCTAssertEqual(missing.lamp, .gray)
    }
}

extension TriageViewModelTests {
    func testHeartbeatGapsAreConservedSeparatelyFromOperationalIncidents() throws {
        var monitor = incidentJSON("old-probe", state: "open")
        monitor["capability_id"] = "meta.probe_heartbeat"
        let snap = try snapshot(incidents: [monitor, incidentJSON("real-business", state: "open")], openCount: 2)
        let model = TriageViewModel(snapshot: snap)
        XCTAssertEqual(model.openIncidents.count, 2)
        XCTAssertEqual(model.monitoringIncidents.count, 1)
        XCTAssertEqual(model.operationalIncidents.count, 1)
        XCTAssertEqual(Set((model.monitoringIncidents + model.operationalIncidents).map(\.episodeID)), Set(model.openIncidents.map(\.episodeID)))
        XCTAssertTrue(model.monitoringIncidents.allSatisfy { $0.recoveryEvidence.isEmpty })
    }
}


extension PanelViewModelTests {
    func testInvalidOikosPercentagesAreRejectedBeforeIntegerConversion() {
        for value in [Double.infinity, Double(Int.max), 40.5, -1, 101] {
            var rows = oikosRows(); rows[0]["disk_percent"] = .number(value)
            XCTAssertFalse(oikosModel(rows).available)
        }
    }
}

extension TriageViewModelTests {
    func testOperationalWarningCannotDowngradeConfirmedPlatformCritical() throws {
        let model = TriageViewModel(snapshot: try snapshot(incidents: [incidentJSON("warning", state: "open")], openCount: 1))
        XCTAssertEqual(model.incidentLamp(platformLamp: .red), .red)
        XCTAssertEqual(model.incidentLamp(platformLamp: .green), .amber)
        XCTAssertEqual(TriageViewModel(snapshot: nil).incidentLamp(platformLamp: .gray), .gray)
    }
}


extension PanelViewModelTests {
    func testCompactMenuIsBrandedAndNeverShowsCancelledQuotaOrUnverifiedRequestCount() {
        let empty = SnapshotMetrics([])
        func menu(_ health: RuntimeHealth = .ok, verified: Bool = false, night: Bool = false) -> MenuBarViewModel {
            MenuBarViewModel(metrics: empty, alertTiers: AlertTiers(act: 19, watch: 0, done: 0), hasSnapshot: true,
                presentation: StatusPresentation(runtimeHealth: health, coverage: .unknown, pausedOvernight: night),
                requestSourceCurrent: verified)
        }
        XCTAssertEqual(menu().compactTitle, "PULSE")
        XCTAssertEqual(menu().menuWIText, "19", "the WI badge mirrors the readable action summary even while sources are partial")
        XCTAssertEqual(menu().menuBadge, "?", "partial request-source freshness is explicit beside the retained count")
        XCTAssertTrue(menu().menuAccessibilityLabel.contains("已读要你出手 19 项，部分请求来源待核验"))
        XCTAssertEqual(menu(verified: true).compactTitle, "PULSE · 19 待参与")
        XCTAssertEqual(menu(verified: true).menuWIText, "19")
        let zero = MenuBarViewModel(metrics: empty, alertTiers: .zero, hasSnapshot: true,
            presentation: StatusPresentation(runtimeHealth: .ok, coverage: .unknown),
            requestSourceCurrent: true)
        XCTAssertEqual(zero.menuWIText, "0", "a fresh measured zero must remain visible as zero")
        XCTAssertEqual(menu(verified: true).menuBadge, "", "WI count is rendered in its own badge, not duplicated as a suffix")
        XCTAssertEqual(menu(.critical, verified: true).compactTitle, "PULSE !")
        XCTAssertEqual(menu(.critical, verified: true).menuWIText, "19")
        XCTAssertEqual(menu(.critical, verified: true).menuBadge, "!")
        XCTAssertEqual(menu(.critical).compactLamp, .red)
        XCTAssertEqual(menu(.warning).compactLamp, .amber)
        XCTAssertEqual(menu(.stale).compactTitle, "PULSE · 待更新")
        XCTAssertEqual(menu(.stale, night: true).compactTitle, "PULSE ◐")
        XCTAssertTrue(menu(.stale, night: true).compactHelp.contains("夜间"))
        XCTAssertLessThan(MenuBarWidth.merged(menu(verified: true)), 180)
    }

    func testExpiredOikosSampleStaysReadableButCannotClaimCurrentHealthy() {
        let model = oikosModel(oikosRows(), at: "2026-09-18T00:25:00Z")
        XCTAssertFalse(model.available)
        XCTAssertEqual(model.lamp, .gray)
        XCTAssertEqual(model.nodes.count, 4)
        let future = oikosModel(oikosRows(), at: "2026-09-17T23:59:59Z")
        XCTAssertFalse(future.available)
        XCTAssertTrue(future.nodes.isEmpty)
        XCTAssertTrue(future.reason.contains("时间异常"))
        XCTAssertTrue(model.reason.contains("过期"))
        let menu = MenuBarViewModel(metrics: SnapshotMetrics([]), alertTiers: .zero, hasSnapshot: true,
            presentation: StatusPresentation(runtimeHealth: .ok, coverage: .unknown), resources: model)
        XCTAssertEqual(menu.compactTitle, "PULSE · 待更新")
    }
}


extension PanelViewModelTests {
    func testQuotaAlwaysSurvivesStatusAndUnknownIsNotZero() {
        let metrics=SnapshotMetrics([metric(MetricID.codexUsedPercent,.number(97),unit:"percent")])
        for health in [RuntimeHealth.ok,.critical,.stale,.unknown] {
            let model=MenuBarViewModel(metrics:metrics,alertTiers:AlertTiers(act:19,watch:0,done:0),hasSnapshot:true,
                presentation:StatusPresentation(runtimeHealth:health,coverage:.unknown))
            XCTAssertEqual(model.menuQuotaText,"97%")
            XCTAssertEqual(model.menuWIText,"19")
            XCTAssertTrue(model.menuAccessibilityLabel.contains("Codex 7 天额度已用 97%"))
            XCTAssertTrue(model.menuAccessibilityLabel.contains("已读要你出手 19 项"))
            if health == .critical { XCTAssertEqual(model.menuBadge,"!") }
        }
        let noSnapshot=MenuBarViewModel(metrics:metrics,alertTiers:AlertTiers(act:19,watch:0,done:0),hasSnapshot:false,
            presentation:StatusPresentation(runtimeHealth:.unknown,coverage:.unknown))
        XCTAssertEqual(noSnapshot.menuWIText,"—")
        XCTAssertTrue(noSnapshot.menuAccessibilityLabel.contains("要你出手数量待更新"))
        let absent=MenuBarViewModel(metrics:SnapshotMetrics([]),alertTiers:.zero,hasSnapshot:false,
            presentation:StatusPresentation(runtimeHealth:.unknown,coverage:.unknown))
        XCTAssertEqual(absent.menuQuotaText,"—")
        let expired=MenuBarViewModel(metrics:metrics,alertTiers:.zero,hasSnapshot:true,
            presentation:StatusPresentation(runtimeHealth:.ok,coverage:.unknown),now:Date.distantFuture)
        XCTAssertEqual(expired.menuQuotaText,"—")
    }
    func testOutstandingSummaryUsesOriginalCreatedAgeNotUpdatedAgeOrWIName() throws {
        var row=inFlightRow("wi_20260906_779",days:11,owner:"pulse")
        row["open_days"] = .number(12)
        let item=try XCTUnwrap(InFlightWorkItem(row:row))
        XCTAssertEqual(item.summaryLine,"Pulse · wi_20260906_779")
        XCTAssertEqual(item.statusAgeText,"in_progress · 12 天")
        XCTAssertEqual(item.days,11)
        row["open_days"] = .null
        XCTAssertTrue(try XCTUnwrap(InFlightWorkItem(row:row)).statusAgeText.contains("时长未知"))
    }
    func testReceiptFailureDescribesCostImpactNotMachineIncident() {
        let source=CostSourceDetail(label:"stats-transport-tcloud",kind:"receipts",status:"unreachable",
            reason:"source_unreadable_or_invalid",filesSeen:0,filesRead:0,filesInvalid:0,excludedDirectories:0,
            latestUsageAt:nil,errors:[:],sourceRef:"reference")
        XCTAssertTrue(source.hasReadFailure)
        XCTAssertEqual(source.displayName,"TCloud 用量同步")
        XCTAssertTrue(source.failureImpact.contains("最新用量可能尚未计入"))
        XCTAssertTrue(source.failureImpact.contains("不是节点宕机"))
    }
}


extension PanelViewModelTests {
    func testWIAgeTintBoundariesDoNotUseLastUpdate() throws {
        for (age, lamp) in [(0,StatusLamp.green),(7,.green),(8,.amber),(30,.amber),(31,.red)] {
            var row=inFlightRow("wi_20260906_779",days:999)
            row["open_days"] = .number(Double(age))
            let item=try XCTUnwrap(InFlightWorkItem(row:row))
            XCTAssertEqual(item.ageLamp,lamp)
            XCTAssertEqual(item.statusAgeText,"in_progress · \(age) 天")
            XCTAssertEqual(item.days,999)
        }
        for value in [MetricScalar.null,.number(-1)] {
            var row=inFlightRow("wi_19990101_001",days:999)
            row["open_days"]=value
            XCTAssertEqual(try XCTUnwrap(InFlightWorkItem(row:row)).ageLamp,.gray)
        }
    }
    func testCreationOrderIgnoresEditsAndRetainsUnknownAndSameDayRecords() throws {
        var newer=inFlightRow("wi_new",days:100)
        newer["open_days"] = .number(2);newer["created_at"] = .string("2026-09-17T00:00:00Z")
        var older=inFlightRow("wi_old",days:0)
        older["open_days"] = .number(12);older["created_at"] = .string("2026-09-07T00:00:00Z")
        var sameDay=older;sameDay["work_item_id"] = .string("wi_a_later")
        sameDay["created_at"] = .string("2026-09-07T01:00:00Z")
        let unknown=inFlightRow("wi_19990101_001",days:999)
        let rows=SnapshotMetrics([metric(MetricID.wiInFlight,.rows([unknown,newer,sameDay,older]),unit:"list")]).inFlightWorkItems
        XCTAssertEqual(rows?.map(\.id),["wi_old","wi_a_later","wi_new","wi_19990101_001"])
    }
    func testWorkflowProfileUsesOnlyExplicitStandardAttributes() throws {
        var lightweight=inFlightRow("wi_light")
        lightweight["workflow_profile"] = .string("lightweight")
        XCTAssertEqual(
            try XCTUnwrap(InFlightWorkItem(row:lightweight)).workflowProfile,
            "lightweight"
        )

        var standard=inFlightRow("wi_standard")
        standard["workflow_profile"] = .string("standard")
        XCTAssertEqual(
            try XCTUnwrap(InFlightWorkItem(row:standard)).workflowProfile,
            "standard"
        )

        standard["workflow_profile"] = .string("legacy")
        XCTAssertNil(try XCTUnwrap(InFlightWorkItem(row:standard)).workflowProfile)
        standard.removeValue(forKey:"workflow_profile")
        XCTAssertNil(try XCTUnwrap(InFlightWorkItem(row:standard)).workflowProfile)
    }
}

extension TriageViewModelTests {
    func testActionJoinsExplicitWIAndSortsByItsCreationNotRequestTime() throws {
        let base=try snapshot(incidents:[],openCount:0)
        let encoder=JSONEncoder();encoder.dateEncodingStrategy = .iso8601
        var raw=try XCTUnwrap(JSONSerialization.jsonObject(with:encoder.encode(base)) as? [String:Any])
        raw["alerts"] = [
            ["event_id":"recent-request","projection_status":"event_only","severity":"warning","capability_id":"notify.action_package","title":"Newer WI","occurred_at":"2026-09-19T02:00:00Z","reason":"old words｜keep body","tier":"act","work_item_id":"wi_new"],
            ["event_id":"older-request","projection_status":"event_only","severity":"warning","capability_id":"notify.action_package","title":"Older WI","occurred_at":"2026-09-18T02:00:00Z","reason":"wrong owner｜keep exact text","tier":"act","work_item_id":"wi_old"],
            ["event_id":"unlinked","projection_status":"event_only","severity":"warning","capability_id":"notify.action_package","title":"Without relation","occurred_at":"2026-09-19T03:00:00Z","reason":"pulse wi_old｜do not infer ID","tier":"act"]]
        raw["metrics"] = [["id":"harness.wi.in_flight","capability_id":"notify.action_package","status":"available","value":[
            ["work_item_id":"wi_new","title":"Newer WI","status":"ready","owner":"aki","days":99,"open_days":2,"needs_owner":true],
            ["work_item_id":"wi_old","title":"Older WI","status":"in_progress","owner":"pulse","days":0,"open_days":12,"needs_owner":true]
        ],"unit":"list","window":"current","quality":"validated","scope":"test"]]
        let snap=try SnapshotDecoder().decode(JSONSerialization.data(withJSONObject:raw))
        let model=TriageViewModel(snapshot:snap)
        XCTAssertEqual(model.act.map(\.id),["older-request","recent-request","unlinked"])
        XCTAssertEqual(model.act[0].workItem?.summaryLine,"Pulse · wi_old")
        XCTAssertEqual(model.act[0].workItem?.statusAgeText,"in_progress · 12 天")
        XCTAssertEqual(model.act[0].body,"keep exact text")
        XCTAssertNil(model.act[2].workItem)
        XCTAssertEqual(model.act.count,3)
        var metricRows=try XCTUnwrap(raw["metrics"] as? [[String:Any]])
        var values=try XCTUnwrap(metricRows[0]["value"] as? [[String:Any]])
        values.append(values[1]);metricRows[0]["value"]=values;raw["metrics"]=metricRows
        let duplicated=TriageViewModel(snapshot:try SnapshotDecoder().decode(JSONSerialization.data(withJSONObject:raw)))
        XCTAssertNil(duplicated.act.first { $0.id=="older-request" }?.workItem)
        XCTAssertEqual(duplicated.act.count,3,"Unknown identity must not delete a request")
        var requests=try XCTUnwrap(raw["alerts"] as? [[String:Any]])
        requests[0]["work_item_id"]=42;raw["alerts"]=requests
        let malformed=TriageViewModel(snapshot:try SnapshotDecoder().decode(JSONSerialization.data(withJSONObject:raw)))
        XCTAssertNil(malformed.act.first { $0.id=="recent-request" }?.workItemID)
        XCTAssertEqual(malformed.act.count,3)
    }
}


extension PanelViewModelTests {
    private func ccQuota(observed: Date, resets: Date, percent: Double = 4) -> SnapshotMetrics {
        SnapshotMetrics([
            Metric(id: MetricID.ccUsedPercent, capabilityID: "s1.request_trace_capture", status: .available,
                   value: .number(percent), unit: "percent", window: "provider_week", quality: .validated,
                   scope: "CC subscription; seven_day window", observedAt: observed),
            Metric(id: MetricID.ccResetsAt, capabilityID: "s1.request_trace_capture", status: .available,
                   value: .number(resets.timeIntervalSince1970), unit: "unix_seconds", window: "provider_week",
                   quality: .validated, scope: "CC subscription; seven_day window", observedAt: observed),
        ])
    }

    /// Relay observes CC quota only on real CC turns: a days-old reading inside
    /// the current weekly window is still the latest fact; outside it is unknown.
    func testCCQuotaShowsLatestObservationInsideCurrentWeeklyWindowOnly() {
        let now = Date(timeIntervalSince1970: 1_790_800_000)
        let resets = now.addingTimeInterval(2 * 86400)
        func menu(_ metrics: SnapshotMetrics) -> MenuBarViewModel {
            MenuBarViewModel(metrics: metrics, alertTiers: .zero, hasSnapshot: true,
                presentation: StatusPresentation(runtimeHealth: .ok, coverage: .unknown), now: now)
        }
        let inside = ccQuota(observed: now.addingTimeInterval(-4 * 86400), resets: resets)
        let relay = RelayViewModel(metrics: inside, hasSnapshot: true, at: now)
        XCTAssertEqual(relay.ccUsedPercent, 4)
        XCTAssertEqual(relay.ccResetsAt, resets)
        XCTAssertEqual(relay.ccQuotaObservedAt, now.addingTimeInterval(-4 * 86400))
        XCTAssertNil(relay.ccQuotaReason)
        XCTAssertEqual(menu(inside).menuCCText, "4%")
        XCTAssertTrue(menu(inside).menuAccessibilityLabel.contains("CC 7 天额度已用 4%"))

        let cases: [(String, SnapshotMetrics)] = [
            ("previous window", ccQuota(observed: now.addingTimeInterval(-6 * 86400), resets: resets)),
            ("window reset", ccQuota(observed: now.addingTimeInterval(-3600), resets: now)),
            ("future observation", ccQuota(observed: now.addingTimeInterval(120), resets: resets)),
            ("zero is not a substitute", ccQuota(observed: now.addingTimeInterval(-6 * 86400), resets: resets, percent: 0)),
            ("absent", SnapshotMetrics([])),
        ]
        for (label, metrics) in cases {
            let gated = RelayViewModel(metrics: metrics, hasSnapshot: true, at: now)
            XCTAssertNil(gated.ccUsedPercent, label)
            XCTAssertNil(gated.ccResetsAt, label)
            XCTAssertNil(gated.ccQuotaObservedAt, label)
            XCTAssertEqual(gated.ccLamp, .gray, label)
            XCTAssertEqual(gated.ccQuotaReason, "当前 7 天窗口内尚无 CC 额度读数，下一次 CC 回合后更新", label)
            XCTAssertEqual(menu(metrics).menuCCText, "—", label)
            XCTAssertTrue(menu(metrics).menuAccessibilityLabel.contains("CC 额度待更新，不以旧数或零替代"), label)
        }
    }

    /// An exporter unknown without observed_at carries its own reason; it is not
    /// rewritten into "window elapsed".
    func testCCQuotaPassesExporterReasonWhenNothingWasObserved() {
        let now = Date(timeIntervalSince1970: 1_790_800_000)
        for (code, text) in [
            ("quota_source_unavailable", "Relay 尚未记录 CC 额度，下一次 CC 回合后更新"),
            ("invalid_quota_window", "CC 额度读数无效，暂不显示"),
        ] {
            let metrics = SnapshotMetrics([
                Metric(id: MetricID.ccUsedPercent, capabilityID: "s1.request_trace_capture", status: .unknown,
                       value: .null, unit: "percent", window: "provider_week", quality: .insufficient,
                       scope: "CC subscription; seven_day window", observedAt: nil, reason: code),
            ])
            let relay = RelayViewModel(metrics: metrics, hasSnapshot: true, at: now)
            XCTAssertNil(relay.ccUsedPercent, code)
            XCTAssertEqual(relay.ccQuotaReason, text, code)
        }
    }

    func testCCQuotaReasonCodesReadAsPlainWords() {
        XCTAssertNil(RelayViewModel.ccQuotaReasonText(nil))
        XCTAssertEqual(RelayViewModel.ccQuotaReasonText("quota_source_unavailable"),
                       "Relay 尚未记录 CC 额度，下一次 CC 回合后更新")
        XCTAssertEqual(RelayViewModel.ccQuotaReasonText("invalid_quota_window"), "CC 额度读数无效，暂不显示")
    }

    private func codexQuota(observed: Date, resets: Date, percent: Double = 12) -> SnapshotMetrics {
        let scope = "Codex account; codex bucket; 10080-minute window"
        return SnapshotMetrics([
            Metric(id: MetricID.codexUsedPercent, capabilityID: "s1.request_trace_capture", status: .available,
                   value: .number(percent), unit: "percent", window: "provider_week", quality: .validated,
                   scope: scope, observedAt: observed),
            Metric(id: MetricID.codexResetsAt, capabilityID: "s1.request_trace_capture", status: .available,
                   value: .number(resets.timeIntervalSince1970), unit: "unix_seconds", window: "provider_week",
                   quality: .validated, scope: scope, observedAt: observed),
        ])
    }

    /// The cost publisher reads Codex quota on its own schedule; like CC, the
    /// latest reading inside the current weekly window stays on screen.
    func testCodexQuotaShowsLatestObservationInsideCurrentWeeklyWindowOnly() {
        let now = Date(timeIntervalSince1970: 1_790_800_000)
        let resets = now.addingTimeInterval(2 * 86400)
        func menu(_ metrics: SnapshotMetrics) -> MenuBarViewModel {
            MenuBarViewModel(metrics: metrics, alertTiers: .zero, hasSnapshot: true,
                presentation: StatusPresentation(runtimeHealth: .ok, coverage: .unknown), now: now)
        }
        let ages: [TimeInterval] = [46 * 60, 3 * 3600, 4 * 86400]
        for age in ages {
            let inside = codexQuota(observed: now.addingTimeInterval(-age), resets: resets)
            let relay = RelayViewModel(metrics: inside, hasSnapshot: true, at: now)
            XCTAssertEqual(relay.codexUsedPercent, 12, "age \(age)")
            XCTAssertEqual(relay.codexResetsAt, resets, "age \(age)")
            XCTAssertEqual(relay.codexQuotaObservedAt, now.addingTimeInterval(-age), "age \(age)")
            XCTAssertNil(relay.codexQuotaReason, "age \(age)")
            XCTAssertEqual(menu(inside).menuQuotaText, "12%", "age \(age)")
        }

        let cases: [(String, SnapshotMetrics)] = [
            ("previous window", codexQuota(observed: now.addingTimeInterval(-6 * 86400), resets: resets)),
            ("window reset", codexQuota(observed: now.addingTimeInterval(-3600), resets: now)),
            ("future observation", codexQuota(observed: now.addingTimeInterval(120), resets: resets)),
        ]
        for (label, metrics) in cases {
            let gated = RelayViewModel(metrics: metrics, hasSnapshot: true, at: now)
            XCTAssertNil(gated.codexUsedPercent, label)
            XCTAssertNil(gated.codexResetsAt, label)
            XCTAssertNil(gated.codexQuotaObservedAt, label)
            XCTAssertEqual(gated.codexLamp, .gray, label)
            XCTAssertEqual(gated.codexQuotaReason, "当前 7 天窗口内尚无 Codex 额度读数，等待下一次账号查询", label)
            XCTAssertEqual(menu(metrics).menuQuotaText, "—", label)
        }
    }

    func testCodexQuotaReasonCodesReadAsPlainWords() {
        let now = Date(timeIntervalSince1970: 1_790_800_000)
        for (code, text) in [
            ("quota_source_unavailable", "尚未取得 Codex 额度读数，等待下一次账号查询"),
            ("quota_window_elapsed", "当前 7 天窗口内尚无 Codex 额度读数，等待下一次账号查询"),
            ("quota_outside_current_window", "当前 7 天窗口内尚无 Codex 额度读数，等待下一次账号查询"),
            ("invalid_quota_window", "Codex 额度读数无效，暂不显示"),
        ] {
            let metrics = SnapshotMetrics([
                Metric(id: MetricID.codexUsedPercent, capabilityID: "s1.request_trace_capture", status: .unknown,
                       value: .null, unit: "percent", window: "provider_week", quality: .insufficient,
                       scope: "Codex account; codex bucket; 10080-minute window", observedAt: nil, reason: code),
            ])
            let relay = RelayViewModel(metrics: metrics, hasSnapshot: true, at: now)
            XCTAssertNil(relay.codexUsedPercent, code)
            XCTAssertEqual(relay.codexQuotaReason, text, code)
        }
        XCTAssertNil(RelayViewModel.codexQuotaReasonText(nil))
        XCTAssertEqual(RelayViewModel(metrics: SnapshotMetrics([]), hasSnapshot: true, at: now).codexQuotaReason,
                       "尚未取得 Codex 额度读数，等待下一次账号查询")
    }

    /// CODEX, CC and WI share one slot width; the optional marker follows them.
    func testMenuBarArtworkPlacesCCBesideCodexWithinBudget() {
        let metrics = SnapshotMetrics([
            metric(MetricID.ccUsedPercent, .number(100), unit: "percent"),
            metric(MetricID.codexUsedPercent, .number(100), unit: "percent"),
        ])
        let model = MenuBarViewModel(metrics: metrics, alertTiers: AlertTiers(act: 128, watch: 0, done: 0),
            hasSnapshot: true, presentation: StatusPresentation(runtimeHealth: .critical, coverage: .unknown))
        XCTAssertEqual(model.menuCCText, "100%")
        XCTAssertEqual(model.menuBadge, "!")
        let slots = MenuBarArtwork.quotaWidth(model.menuQuotaText) + MenuBarArtwork.badgeGap
            + MenuBarArtwork.quotaWidth(model.menuCCText) + MenuBarArtwork.badgeGap
            + MenuBarArtwork.quotaWidth(model.menuWIText)
        let width = MenuBarArtwork.image(for: model).size.width
        XCTAssertGreaterThan(width, slots, "the status marker follows the three slots")
        XCTAssertLessThanOrEqual(MenuBarWidth.merged(model), MenuBarWidth.budget)
        let quiet = MenuBarViewModel(metrics: SnapshotMetrics([]), alertTiers: .zero, hasSnapshot: true,
            presentation: StatusPresentation(runtimeHealth: .ok, coverage: .full), requestSourceCurrent: true)
        XCTAssertEqual(quiet.menuCCText, "—")
        XCTAssertEqual(MenuBarArtwork.image(for: quiet).size.width,
            MenuBarArtwork.quotaWidth("—") * 3 + MenuBarArtwork.badgeGap * 3 + MenuBarArtwork.quotaWidth("0"))
    }

    // MARK: GROK weekly quota

    private func grokQuota(observed: Date, start: Date, resets: Date, percent: Double = 87.38812,
                           label: String? = "Grok Bot Plan") -> SnapshotMetrics {
        let scope = "Grok account weekly pool; not per-bot cost"
        func reading(_ id: String, _ value: MetricValue, _ unit: String) -> Metric {
            Metric(id: id, capabilityID: "s1.request_trace_capture", status: .available, value: value,
                   unit: unit, window: "provider_week", quality: .validated, scope: scope, observedAt: observed)
        }
        var rows = [
            reading(MetricID.grokUsedPercent, .number(percent), "percent"),
            reading(MetricID.grokResetsAt, .number(resets.timeIntervalSince1970), "unix_seconds"),
            reading(MetricID.grokPeriodStart, .number(start.timeIntervalSince1970), "unix_seconds"),
        ]
        if let label { rows.append(reading(MetricID.grokPlanLabel, .text(label), "text")) }
        return SnapshotMetrics(rows)
    }

    /// What the exporter publishes for an unavailable or not-applicable fact.
    private func grokUnavailable(_ code: String, observed: Date?, label: String? = nil) -> SnapshotMetrics {
        let scope = "Grok account weekly pool; not per-bot cost"
        var rows = [MetricID.grokUsedPercent, MetricID.grokResetsAt, MetricID.grokPeriodStart].map { id in
            Metric(id: id, capabilityID: "s1.request_trace_capture", status: .unknown, value: .null,
                   unit: id == MetricID.grokUsedPercent ? "percent" : "unix_seconds", window: "provider_week",
                   quality: .insufficient, scope: scope, observedAt: observed, reason: code)
        }
        if let label {
            rows.append(Metric(id: MetricID.grokPlanLabel, capabilityID: "s1.request_trace_capture", status: .available,
                               value: .text(label), unit: "text", window: "provider_week", quality: .validated,
                               scope: scope, observedAt: observed))
        }
        return SnapshotMetrics(rows)
    }

    private func grokMenu(_ metrics: SnapshotMetrics, at now: Date) -> MenuBarViewModel {
        MenuBarViewModel(metrics: metrics, alertTiers: .zero, hasSnapshot: true,
            presentation: StatusPresentation(runtimeHealth: .ok, coverage: .unknown), now: now)
    }

    /// The panel keeps the Grok reading; the menu bar no longer carries it.
    func testGrokQuotaStaysInPanelButLeavesMenuBar() {
        let now = Date(timeIntervalSince1970: 1_792_569_600)
        let start = now.addingTimeInterval(-86400)
        let resets = start.addingTimeInterval(7 * 86400)
        let observed = now.addingTimeInterval(-20 * 60)
        let metrics = grokQuota(observed: observed, start: start, resets: resets)
        let relay = RelayViewModel(metrics: metrics, hasSnapshot: true, at: now)
        XCTAssertEqual(relay.grokUsedPercent, 87.38812)
        XCTAssertEqual(relay.grokResetsAt, resets)
        XCTAssertEqual(relay.grokQuotaObservedAt, observed)
        XCTAssertEqual(relay.grokPlanLabel, "Grok Bot Plan")
        XCTAssertNil(relay.grokQuotaReason)
        XCTAssertEqual(relay.grokQuotaSummary, "已用 87%")
        let menu = grokMenu(metrics, at: now)
        XCTAssertFalse(menu.menuAccessibilityLabel.localizedCaseInsensitiveContains("grok"))
        XCTAssertEqual(menu, grokMenu(SnapshotMetrics([]), at: now), "a Grok reading changes nothing on the bar")
        XCTAssertEqual(menu.menuAgyText, "—")
        XCTAssertEqual(MenuBarArtwork.image(for: menu).size, MenuBarArtwork.image(for: grokMenu(SnapshotMetrics([]), at: now)).size)
    }

    /// Login, account, API change or stale reading: a dash with the reason, never 0%.
    func testGrokQuotaUnavailableShowsDashAndReasonNeverZero() {
        let now = Date(timeIntervalSince1970: 1_792_569_600)
        let start = now.addingTimeInterval(-86400)
        let resets = start.addingTimeInterval(7 * 86400)
        let failedAt = now.addingTimeInterval(-5 * 60)
        let cases: [(String, SnapshotMetrics, String, Date?)] = [
            ("login", grokUnavailable("grok_login_invalid", observed: failedAt),
             "Cursor 登录已失效，需在原登录入口重新登录", failedAt),
            ("mismatch", grokUnavailable("account_mismatch", observed: failedAt),
             "Cursor 登录账号与 Grok Bot 当前账号不一致，暂不显示", failedAt),
            ("switched", grokUnavailable("account_changed_during_read", observed: failedAt),
             "读取期间切换了账号，等待下一次账号查询", failedAt),
            ("schema", grokUnavailable("quota_schema_changed", observed: failedAt),
             "额度接口的返回已变化，暂不显示", failedAt),
            ("no login", grokUnavailable("cursor_auth_db_unavailable", observed: failedAt),
             "本机没有可用的 Cursor 登录，需先登录", failedAt),
            ("missing", SnapshotMetrics([]), "尚未取得 GROK 额度读数，等待下一次账号查询", nil),
            ("exporter missing", grokUnavailable("quota_source_unavailable", observed: nil),
             "尚未取得 GROK 额度读数，等待下一次账号查询", nil),
            ("reset passed", grokQuota(observed: now.addingTimeInterval(-3600), start: start.addingTimeInterval(-7 * 86400),
                                       resets: now),
             "当前周期内尚无 GROK 额度读数，等待下一次账号查询", nil),
            ("previous period", grokQuota(observed: start.addingTimeInterval(-3600), start: start, resets: resets),
             "当前周期内尚无 GROK 额度读数，等待下一次账号查询", nil),
            ("future", grokQuota(observed: now.addingTimeInterval(300), start: start, resets: resets),
             "当前周期内尚无 GROK 额度读数，等待下一次账号查询", nil),
        ]
        for (label, metrics, reason, observed) in cases {
            let relay = RelayViewModel(metrics: metrics, hasSnapshot: true, at: now)
            XCTAssertNil(relay.grokUsedPercent, label)
            XCTAssertNil(relay.grokResetsAt, label)
            XCTAssertEqual(relay.grokQuotaObservedAt, observed, label)
            XCTAssertEqual(relay.grokQuotaReason, reason, label)
            XCTAssertEqual(relay.grokQuotaSummary, "待更新", label)
            XCTAssertFalse(relay.grokQuotaNotApplicable, label)
            XCTAssertEqual(relay.grokLamp, .gray, label)
        }
    }

    /// A pooled enterprise allowance or no included allowance has no personal weekly quota.
    func testGrokPooledAllowanceIsNotApplicableNotAFabricatedQuota() {
        let now = Date(timeIntervalSince1970: 1_792_569_600)
        for (code, reason) in [
            ("pooled_enterprise_allowance", "该账号使用企业共享额度，没有个人周额度"),
            ("no_personal_allowance", "该账号没有个人周额度"),
        ] {
            let metrics = grokUnavailable(code, observed: now, label: "Grok Bot Plan")
            let relay = RelayViewModel(metrics: metrics, hasSnapshot: true, at: now)
            XCTAssertNil(relay.grokUsedPercent, code)
            XCTAssertTrue(relay.grokQuotaNotApplicable, code)
            XCTAssertEqual(relay.grokQuotaSummary, "不适用", code)
            XCTAssertEqual(relay.grokQuotaReason, reason, code)
            XCTAssertEqual(relay.grokPlanLabel, "Grok Bot Plan", code)
        }
    }

    /// AGY takes the slot after CC that GROK held, the same width as the others, inside the budget.
    func testMenuBarArtworkPlacesAgyAfterCCWithinBudget() {
        let spent: [[String: MetricScalar]] = [["group": .string("Gemini Models"), "window": .string("weekly"),
            "remaining_fraction": .number(0), "resets_at": .number(4_000_000_000)]]
        let metrics = SnapshotMetrics([
            metric(MetricID.ccUsedPercent, .number(100), unit: "percent"),
            metric(MetricID.codexUsedPercent, .number(100), unit: "percent"),
            Metric(id: MetricID.agyQuota, capabilityID: "s1.request_trace_capture", status: .available,
                   value: .rows(spent), unit: "buckets", window: "current", quality: .validated,
                   scope: "agy account quota groups; not per-agent cost", observedAt: nil, reason: nil),
        ])
        let model = MenuBarViewModel(metrics: metrics, alertTiers: AlertTiers(act: 128, watch: 0, done: 0),
            hasSnapshot: true, presentation: StatusPresentation(runtimeHealth: .critical, coverage: .unknown))
        XCTAssertEqual(model.menuAgyText, "100%")
        let slots = MenuBarArtwork.quotaWidth("100%") * 3 + MenuBarArtwork.badgeGap * 3
            + MenuBarArtwork.quotaWidth(model.menuWIText)
        XCTAssertGreaterThan(MenuBarArtwork.image(for: model).size.width, slots)
        XCTAssertLessThanOrEqual(MenuBarWidth.merged(model), MenuBarWidth.budget)
    }
}


extension PanelViewModelTests {
    /// The published rows as recorded from `agy -p /quota` on 2026-11-20.
    private func agyQuota(observed: Date?, status: MetricStatus = .available, reason: String? = nil,
                          rows: [[String: MetricScalar]]? = nil) -> SnapshotMetrics {
        func row(_ group: String, _ window: String, _ fraction: Double, _ reset: String) -> [String: MetricScalar] {
            ["group": .string(group), "window": .string(window), "remaining_fraction": .number(fraction),
             "resets_at": .number(SnapshotDecoder.parseISO8601(reset)!.timeIntervalSince1970)]
        }
        let recorded = [
            row("Gemini Models", "weekly", 0.9910842776298523, "2026-11-27T04:09:24Z"),
            row("Gemini Models", "5h", 0.9861072897911072, "2026-11-20T15:30:36Z"),
            row("Claude and GPT models", "weekly", 1, "2026-11-27T10:38:17Z"),
            row("Claude and GPT models", "5h", 1, "2026-11-20T15:38:17Z"),
            row("Gemini 4 Argon", "weekly", 1, "2026-11-27T10:38:17Z"),
        ]
        return SnapshotMetrics([Metric(id: MetricID.agyQuota, capabilityID: "s1.request_trace_capture", status: status,
            value: status == .available ? .rows(rows ?? recorded) : .null, unit: "buckets", window: "current",
            quality: status == .available ? .validated : .insufficient,
            scope: "agy account quota groups; not per-agent cost", observedAt: observed, reason: reason)])
    }

    /// Three groups with remaining share and reset time; Argon has only its weekly window.
    func testAgyQuotaShowsThreeGroupsWithRemainingAndReset() {
        let now = SnapshotDecoder.parseISO8601("2026-11-20T10:48:24Z")!
        let observed = now.addingTimeInterval(-600)
        let relay = RelayViewModel(metrics: agyQuota(observed: observed), hasSnapshot: true, at: now)
        let groups = relay.agyQuotaGroups ?? []
        XCTAssertEqual(groups.map(\.name), ["Gemini Models", "Claude and GPT models", "Gemini 4 Argon"])
        XCTAssertEqual(groups[0].weeklyRemainingPercent ?? 0, 99.108, accuracy: 0.001)
        XCTAssertEqual(groups[0].weeklyResetsAt, SnapshotDecoder.parseISO8601("2026-11-27T04:09:24Z"))
        XCTAssertEqual(groups[0].fiveHourRemainingPercent ?? 0, 98.611, accuracy: 0.001)
        XCTAssertEqual(groups[0].fiveHourResetsAt, SnapshotDecoder.parseISO8601("2026-11-20T15:30:36Z"))
        XCTAssertEqual(groups[1].weeklyRemainingPercent, 100)
        XCTAssertEqual(groups[2].weeklyRemainingPercent, 100)
        XCTAssertNil(groups[2].fiveHourRemainingPercent, "Argon has no 5-hour window")
        XCTAssertNil(groups[2].fiveHourResetsAt)
        XCTAssertEqual(relay.agyQuotaObservedAt, observed)
        XCTAssertNil(relay.agyQuotaReason)
        XCTAssertEqual(relay.agyQuotaSummary, "最低剩 99%")
        XCTAssertEqual(relay.agyLamp, .green)
        XCTAssertEqual(CostDashboardView.agyBucketText(100, nil), "剩 100% · 未知 重置")
    }

    /// A failed read, a stale reading or a reset window shows the reason, never an old figure.
    func testAgyQuotaFailureOrStaleReadingShowsReasonNotOldFigure() {
        let now = SnapshotDecoder.parseISO8601("2026-11-20T10:48:24Z")!
        let failedAt = now.addingTimeInterval(-60)
        let cases: [(String, SnapshotMetrics, Date, String, Date?)] = [
            ("failed", agyQuota(observed: failedAt, status: .unknown, reason: "agy_command_failed"), now,
             "agy 额度查询失败，等待下一次查询", failedAt),
            ("timeout", agyQuota(observed: failedAt, status: .unknown, reason: "agy_command_timeout"), now,
             "agy 额度查询超时，等待下一次查询", failedAt),
            ("schema", agyQuota(observed: failedAt, status: .unknown, reason: "quota_schema_changed"), now,
             "agy 额度输出格式已变化，暂不显示", failedAt),
            ("missing", SnapshotMetrics([]), now, "尚未取得 agy 额度读数，等待下一次查询", nil),
            ("stale", agyQuota(observed: now.addingTimeInterval(-3700)), now,
             "agy 额度读数已超过 1 小时，不当作当前读数", nil),
            ("all reset", agyQuota(observed: now.addingTimeInterval(8 * 86400 - 600)), now.addingTimeInterval(8 * 86400),
             "额度窗口已重置，等待下一次查询", nil),
        ]
        for (label, metrics, at, reason, observed) in cases {
            let relay = RelayViewModel(metrics: metrics, hasSnapshot: true, at: at)
            XCTAssertNil(relay.agyQuotaGroups, label)
            XCTAssertEqual(relay.agyQuotaReason, reason, label)
            XCTAssertEqual(relay.agyQuotaObservedAt, observed, label)
            XCTAssertEqual(relay.agyQuotaSummary, "待更新", label)
            XCTAssertEqual(relay.agyLamp, .gray, label)
        }
        // A bucket past its reset is dropped; the rest of its group stays.
        let afterFiveHour = SnapshotDecoder.parseISO8601("2026-11-20T15:35:00Z")!
        let relay = RelayViewModel(metrics: agyQuota(observed: afterFiveHour.addingTimeInterval(-300)),
                                   hasSnapshot: true, at: afterFiveHour)
        XCTAssertNil(relay.agyQuotaGroups?[0].fiveHourRemainingPercent)
        XCTAssertNotNil(relay.agyQuotaGroups?[0].weeklyRemainingPercent)
        XCTAssertEqual(relay.agyQuotaGroups?[1].fiveHourRemainingPercent, 100)
    }

    private func agyMenu(_ metrics: SnapshotMetrics, at now: Date) -> MenuBarViewModel {
        MenuBarViewModel(metrics: metrics, alertTiers: .zero, hasSnapshot: true,
            presentation: StatusPresentation(runtimeHealth: .ok, coverage: .unknown), now: now)
    }

    /// The AGY slot reads the scarcest current bucket across all groups, as a used share.
    func testAgyMenuBarSlotReadsLowestRemainingBucket() {
        let now = SnapshotDecoder.parseISO8601("2026-11-20T10:48:24Z")!
        func row(_ group: String, _ window: String, _ fraction: Double) -> [String: MetricScalar] {
            ["group": .string(group), "window": .string(window), "remaining_fraction": .number(fraction),
             "resets_at": .number(now.addingTimeInterval(3600).timeIntervalSince1970)]
        }
        let rows = [row("Gemini Models", "weekly", 0.80), row("Gemini Models", "5h", 0.60),
                    row("Claude and GPT models", "weekly", 0.07), row("Claude and GPT models", "5h", 0.30),
                    row("Gemini 4 Argon", "weekly", 0.95)]
        let metrics = agyQuota(observed: now.addingTimeInterval(-600), rows: rows)
        let menu = agyMenu(metrics, at: now)
        XCTAssertEqual(menu.agyUsedPercent ?? 0, 93, accuracy: 0.0001)
        XCTAssertEqual(menu.menuAgyText, "93%")
        XCTAssertEqual(menu.agyLamp, .amber)
        XCTAssertEqual(menu.agyLamp, RelayViewModel(metrics: metrics, hasSnapshot: true, at: now).agyLamp,
                       "bar and panel agree on the lamp")
        XCTAssertTrue(menu.menuAccessibilityLabel.contains("agy 额度最紧一组已用 93%"))
        // The recorded 2026-11-20 reading: Gemini 5h at 98.6% remaining is the scarcest.
        let recorded = agyMenu(agyQuota(observed: now.addingTimeInterval(-600)), at: now)
        XCTAssertEqual(recorded.menuAgyText, "1%")
        XCTAssertEqual(recorded.agyLamp, .green)
        // A fully spent bucket is a reading, not an unknown.
        XCTAssertEqual(agyMenu(agyQuota(observed: now, rows: [row("Gemini Models", "weekly", 0)]), at: now)
            .menuAgyText, "100%")
    }

    /// Stale, failed, missing or fully reset agy readings show a dash, never 0% or an old figure.
    func testAgyMenuBarSlotUnknownShowsDashNeverZero() {
        let now = SnapshotDecoder.parseISO8601("2026-11-20T10:48:24Z")!
        let cases: [(String, SnapshotMetrics, Date)] = [
            ("failed", agyQuota(observed: now.addingTimeInterval(-60), status: .unknown, reason: "agy_command_failed"), now),
            ("unavailable", agyQuota(observed: nil, status: .unknown, reason: "agy_command_unavailable"), now),
            ("missing", SnapshotMetrics([]), now),
            ("stale", agyQuota(observed: now.addingTimeInterval(-3700)), now),
            ("all reset", agyQuota(observed: now.addingTimeInterval(8 * 86400 - 600)), now.addingTimeInterval(8 * 86400)),
        ]
        for (label, metrics, at) in cases {
            let menu = agyMenu(metrics, at: at)
            XCTAssertNil(menu.agyUsedPercent, label)
            XCTAssertEqual(menu.menuAgyText, "—", label)
            XCTAssertEqual(menu.agyLamp, .gray, label)
            XCTAssertTrue(menu.menuAccessibilityLabel.contains("agy 额度待更新，不以旧数或零替代"), label)
        }
    }
}
