import Foundation

/// One line in the "要你操心的" section.
///
/// Wording discipline (auto-memory `cron-notify-three-tier-plain-language`):
/// this text is read by 大鸟, not by an engineer. No capability ids, no metric
/// ids, no English abbreviations, no internal codenames. "s2.ledger_invariants:
/// 5 findings" is wrong; "协作账本有 5 件事没收尾" is right.
struct Concern: Equatable, Identifiable, Sendable {
    let key: String
    let lamp: StatusLamp
    let title: String
    let detail: String?

    var id: String { key }
}

// MARK: - Relay

/// One agy quota group (e.g. Gemini Models): weekly and, when the group has
/// one, 5-hour remaining share with their reset times. Argon has no 5-hour window.
struct AgyQuotaGroup: Equatable, Sendable {
    let name: String
    let weeklyRemainingPercent: Double?
    let weeklyResetsAt: Date?
    let fiveHourRemainingPercent: Double?
    let fiveHourResetsAt: Date?
}

/// Everything the ⚡ menu bar item and its panel render.
struct RelayViewModel: Equatable, Sendable {
    let ccProcesses: Int?
    let ccBusy: Int?
    let ccIdle: Int?
    let ccRateLimited: Bool?
    let botsOnline: Int?
    let ccUsedPercent: Double?
    let ccResetsAt: Date?
    let ccQuotaObservedAt: Date?
    /// Rolling 5-hour reading; shown only while the observation lies inside the last 5 hours.
    let ccFiveHourUsedPercent: Double?
    let codexUsedPercent: Double?
    let codexResetsAt: Date?
    let codexQuotaObservedAt: Date?
    /// Grok account weekly pool. Not per-bot cost and never in a cost total.
    let grokUsedPercent: Double?
    let grokResetsAt: Date?
    /// When the shown reading, or the failed read behind the reason, happened.
    let grokQuotaObservedAt: Date?
    let grokPlanLabel: String?
    /// The account has no personal weekly quota (pooled or none included).
    let grokQuotaNotApplicable: Bool
    let grokQuotaReason: String?
    /// agy account quota groups in published order; nil when there is no current reading.
    let agyQuotaGroups: [AgyQuotaGroup]?
    /// When the shown reading, or the failed read behind the reason, happened.
    let agyQuotaObservedAt: Date?
    let agyQuotaReason: String?

    let inputTokens: Int?
    let inputTokensUncached: Int?
    let cacheReadTokens: Int?
    let cacheCreationTokens: Int?
    let outputTokens: Int?
    let turns: Int?
    let failures: Int?
    let inbound: Int?
    let inboundTypes: [String: Int]?
    let durationP50: Double?
    let hourlyOutputTokens: [Double]?
    let agents: [AgentUsage]?
    let sessions: [SessionUsage]?

    let deliverySent: Int?
    let deliveryAttempts: [String: Int]?

    /// 见 `MetricID.usageMalformedLines`：读数的可信度修饰符，不是指标。
    let malformedLines: Int?

    let hasSnapshot: Bool
    let usageSourceNote: String?
    let agentUsageStatus: MetricStatus?
    let codexQuotaReason: String?
    let ccQuotaReason: String?

    var agentUsageEmptyText: String {
        agentUsageStatus == .available ? "今天还没有已记录用量" : "用量数据不可用，并非今天没有用量"
    }

    init(metrics: SnapshotMetrics, hasSnapshot: Bool, at now: Date? = nil) {
        ccProcesses = metrics.int(MetricID.ccProcesses)
        ccBusy = metrics.int(MetricID.ccBusy)
        ccIdle = metrics.int(MetricID.ccIdle)
        ccRateLimited = metrics.bool(MetricID.ccRateLimited)
        botsOnline = metrics.int(MetricID.botsOnline)
        // Relay records CC quota only on a real CC turn, so the reading is the
        // latest observation inside the current weekly window: shown while the
        // window has not reset and the observation lies inside it.
        let ccObserved = metrics.rawMetric(MetricID.ccUsedPercent)?.observedAt
        let ccFresh = now.map { time in
            guard let ccObserved, let reset = metrics.ccResetsAt else { return false }
            return ccObserved <= time.addingTimeInterval(60) && reset > time
                && ccObserved > reset.addingTimeInterval(-7 * 24 * 3600)
        } ?? true
        ccUsedPercent = ccFresh ? metrics.double(MetricID.ccUsedPercent) : nil
        ccResetsAt = ccFresh ? metrics.ccResetsAt : nil
        ccQuotaObservedAt = ccFresh ? ccObserved : nil
        let fiveHourObserved = metrics.rawMetric(MetricID.ccFiveHourUsedPercent)?.observedAt
        let fiveHourFresh = now.map { time in
            guard let fiveHourObserved else { return false }
            return fiveHourObserved <= time.addingTimeInterval(60) && time.timeIntervalSince(fiveHourObserved) < 300 * 60
        } ?? true
        ccFiveHourUsedPercent = fiveHourFresh ? metrics.double(MetricID.ccFiveHourUsedPercent) : nil
        // 导出器给出无观测时间的 unknown 时，原因由导出器决定；只有确有读数但已过窗口才改写。
        let ccRawReason = metrics.rawMetric(MetricID.ccUsedPercent)?.reason
        ccQuotaReason = Self.ccQuotaReasonText(
            ccFresh ? ccRawReason : ccObserved == nil ? ccRawReason ?? "quota_window_elapsed" : "quota_window_elapsed")
        // The cost publisher reads Codex quota on its own schedule, so the
        // reading is the latest account observation inside the current weekly
        // window, under the same rule as CC above.
        let quotaObserved = metrics.rawMetric(MetricID.codexUsedPercent)?.observedAt
        let quotaFresh = now.map { time in
            guard let quotaObserved, let reset = metrics.codexResetsAt else { return false }
            return quotaObserved <= time.addingTimeInterval(60) && reset > time
                && quotaObserved > reset.addingTimeInterval(-7 * 24 * 3600)
        } ?? true
        codexUsedPercent = quotaFresh ? metrics.double(MetricID.codexUsedPercent) : nil
        codexResetsAt = quotaFresh ? metrics.codexResetsAt : nil
        codexQuotaObservedAt = quotaFresh ? quotaObserved : nil
        // Same window rule for Grok; its period start comes from the service.
        let grokRaw = metrics.rawMetric(MetricID.grokUsedPercent)
        let grokObserved = grokRaw?.observedAt
        let grokFresh = now.map { time in
            guard let grokObserved, let reset = metrics.grokResetsAt,
                  let start = metrics.grokPeriodStart else { return false }
            return grokObserved <= time.addingTimeInterval(60) && reset > time
                && grokObserved >= start.addingTimeInterval(-60)
        } ?? true
        grokUsedPercent = grokFresh ? metrics.double(MetricID.grokUsedPercent) : nil
        grokResetsAt = grokUsedPercent == nil ? nil : metrics.grokResetsAt
        let grokCode = grokUsedPercent != nil ? nil
            : grokRaw?.status == .available ? "quota_window_elapsed"
            : grokRaw?.reason ?? "quota_source_unavailable"
        // A failed read keeps its own time; an expired reading shows none.
        grokQuotaObservedAt = grokUsedPercent != nil || grokRaw?.status != .available ? grokObserved : nil
        grokPlanLabel = metrics.metric(MetricID.grokPlanLabel)?.value.text
        grokQuotaNotApplicable = Self.grokNotApplicable.contains(grokCode ?? "")
        grokQuotaReason = Self.grokQuotaReasonText(grokCode)
        // agy is read every publication cycle; a reading older than an hour,
        // or a bucket past its reset, is not shown as current.
        let agyRaw = metrics.rawMetric(MetricID.agyQuota)
        let agyFresh = now.map { time in
            guard let observed = agyRaw?.observedAt else { return false }
            return observed <= time.addingTimeInterval(60) && time.timeIntervalSince(observed) < 3600
        } ?? true
        let agyGroups = agyFresh ? Self.agyGroups(metrics.rows(MetricID.agyQuota), at: now) : nil
        agyQuotaGroups = agyGroups
        let agyCode = agyGroups != nil ? nil
            : agyRaw?.status == .available ? (agyFresh ? "quota_window_elapsed" : "quota_reading_stale")
            : agyRaw?.reason ?? "quota_source_unavailable"
        agyQuotaObservedAt = agyGroups != nil || agyRaw?.status != .available ? agyRaw?.observedAt : nil
        agyQuotaReason = Self.agyQuotaReasonText(agyCode)
        let usageObserved = metrics.rawMetric("usage.today.source_note")?.observedAt
        let usageFresh = now.map { time in
            guard let usageObserved else { return false }
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
            return usageObserved <= time && time.timeIntervalSince(usageObserved) < 45 * 60
                && calendar.isDate(usageObserved, inSameDayAs: time)
        } ?? true

        inputTokens = usageFresh ? metrics.int(MetricID.inputTokens) : nil
        inputTokensUncached = usageFresh ? metrics.int(MetricID.inputTokensUncached) : nil
        cacheReadTokens = usageFresh ? metrics.int(MetricID.cacheReadTokens) : nil
        cacheCreationTokens = usageFresh ? metrics.int(MetricID.cacheCreationTokens) : nil
        outputTokens = usageFresh ? metrics.int(MetricID.outputTokens) : nil
        turns = metrics.int(MetricID.turns)
        failures = metrics.int(MetricID.failures)
        inbound = metrics.int(MetricID.inbound)
        inboundTypes = metrics.counts(MetricID.inboundTypes)
        durationP50 = metrics.double(MetricID.durationP50)
        malformedLines = metrics.int(MetricID.usageMalformedLines)
        hourlyOutputTokens = metrics.hourlyOutputTokens
        agents = usageFresh ? metrics.agentUsage : nil
        sessions = metrics.sessionUsage
        billingFacts = metrics.billingFacts
        billingAttribution = metrics.billingAttribution
        managedCostShadow = metrics.managedCostShadow
        mtdAgentCosts = metrics.mtdAgentCosts
        mtdOutputTokens = metrics.int(MetricID.mtdOutputTokens)
        mtdDaysCovered = metrics.int(MetricID.mtdDaysCovered)
        billingAttributionQuality =
            metrics.rawMetric(MetricID.billingByAgent)?.quality
        billingAttributionReason =
            metrics.rawMetric(MetricID.billingByAgent)?.reason

        deliverySent = metrics.int(MetricID.deliverySent)
        deliveryAttempts = metrics.counts(MetricID.deliveryAttempts)

        usageSourceNote = usageFresh ? metrics.rawMetric("usage.today.source_note")?.reason : "当前日期的用量查询尚未更新"
        agentUsageStatus = usageFresh ? metrics.rawMetric(MetricID.byAgent)?.status : .unknown
        let codexRawReason = metrics.rawMetric(MetricID.codexUsedPercent)?.reason
        codexQuotaReason = Self.codexQuotaReasonText(
            quotaFresh ? codexRawReason
                : quotaObserved == nil ? codexRawReason ?? "quota_source_unavailable" : "quota_window_elapsed")
        self.hasSnapshot = hasSnapshot
    }

    /// Plain words for the exporter's stable CC quota reason codes.
    static func ccQuotaReasonText(_ code: String?) -> String? {
        switch code {
        case nil: return nil
        case "quota_source_unavailable": return "Relay 尚未记录 CC 额度，下一次 CC 回合后更新"
        case "quota_window_elapsed", "quota_outside_current_window": return "当前 7 天窗口内尚无 CC 额度读数，下一次 CC 回合后更新"
        default: return "CC 额度读数无效，暂不显示"
        }
    }

    /// Plain words for the exporter's stable Codex quota reason codes.
    static func codexQuotaReasonText(_ code: String?) -> String? {
        switch code {
        case nil: return nil
        case "quota_source_unavailable": return "尚未取得 Codex 额度读数，等待下一次账号查询"
        case "quota_window_elapsed", "quota_outside_current_window":
            return "当前 7 天窗口内尚无 Codex 额度读数，等待下一次账号查询"
        default: return "Codex 额度读数无效，暂不显示"
        }
    }

    static let grokNotApplicable: Set<String> = ["pooled_enterprise_allowance", "no_personal_allowance"]

    /// Plain words for the exporter's stable Grok quota reason codes.
    static func grokQuotaReasonText(_ code: String?) -> String? {
        switch code {
        case nil: return nil
        case "quota_source_unavailable": return "尚未取得 GROK 额度读数，等待下一次账号查询"
        case "quota_window_elapsed", "quota_outside_current_window":
            return "当前周期内尚无 GROK 额度读数，等待下一次账号查询"
        case "grok_login_invalid": return "Cursor 登录已失效，需在原登录入口重新登录"
        case "cursor_auth_db_unavailable", "cursor_token_missing": return "本机没有可用的 Cursor 登录，需先登录"
        case "grok_account_unavailable": return "读不到 Grok Bot 当前账号，暂不显示"
        case "account_mismatch": return "Cursor 登录账号与 Grok Bot 当前账号不一致，暂不显示"
        case "account_changed_during_read": return "读取期间切换了账号，等待下一次账号查询"
        case "quota_schema_changed", "quota_api_changed", "unexpected_billing_brand":
            return "额度接口的返回已变化，暂不显示"
        case "pooled_enterprise_allowance": return "该账号使用企业共享额度，没有个人周额度"
        case "no_personal_allowance": return "该账号没有个人周额度"
        default: return "GROK 额度读数无效，暂不显示"
        }
    }

    /// Groups from the exporter's bucket rows, in first-seen order; buckets
    /// past their reset are dropped. Nil when no group has a current bucket.
    static func agyGroups(_ rows: [[String: MetricScalar]]?, at now: Date?) -> [AgyQuotaGroup]? {
        guard let rows else { return nil }
        var order: [String] = []
        var buckets: [String: [String: (Double, Date)]] = [:]
        for row in rows {
            guard let group = row["group"]?.string, let window = row["window"]?.string,
                  let fraction = row["remaining_fraction"]?.number, (0...1).contains(fraction),
                  let seconds = row["resets_at"]?.number, seconds > 0 else { continue }
            let reset = Date(timeIntervalSince1970: seconds)
            if let now, reset <= now { continue }
            if buckets[group] == nil { order.append(group); buckets[group] = [:] }
            buckets[group]?[window] = (fraction * 100, reset)
        }
        let groups = order.map { name in
            AgyQuotaGroup(name: name,
                          weeklyRemainingPercent: buckets[name]?["weekly"]?.0,
                          weeklyResetsAt: buckets[name]?["weekly"]?.1,
                          fiveHourRemainingPercent: buckets[name]?["5h"]?.0,
                          fiveHourResetsAt: buckets[name]?["5h"]?.1)
        }
        return groups.isEmpty ? nil : groups
    }

    /// Plain words for the exporter's stable agy quota reason codes.
    static func agyQuotaReasonText(_ code: String?) -> String? {
        switch code {
        case nil: return nil
        case "quota_source_unavailable": return "尚未取得 agy 额度读数，等待下一次查询"
        case "quota_window_elapsed": return "额度窗口已重置，等待下一次查询"
        case "quota_reading_stale": return "agy 额度读数已超过 1 小时，不当作当前读数"
        case "agy_command_unavailable": return "本机找不到 agy 命令，暂不显示"
        case "agy_command_timeout": return "agy 额度查询超时，等待下一次查询"
        case "agy_command_failed": return "agy 额度查询失败，等待下一次查询"
        case "quota_schema_changed", "quota_output_too_large": return "agy 额度输出格式已变化，暂不显示"
        default: return "agy 额度读数无效，暂不显示"
        }
    }

    /// The scarcest current bucket across all groups decides the lamp and summary.
    var agyLowestRemainingPercent: Double? {
        agyQuotaGroups?.flatMap { [$0.weeklyRemainingPercent, $0.fiveHourRemainingPercent] }
            .compactMap { $0 }.min()
    }

    var agyLamp: StatusLamp { LampRules.quota(usedPercent: agyLowestRemainingPercent.map { 100 - $0 }) }

    /// Panel summary beside the agy row.
    var agyQuotaSummary: String {
        agyLowestRemainingPercent.map { "最低剩 " + StatsFormat.percent($0) } ?? "待更新"
    }

    /// Pure quota colour. Process and 429 health has its own row and concern;
    /// mixing it here made the same quota figure disagree with the menu bar.
    var ccLamp: StatusLamp {
        LampRules.quota(usedPercent: ccUsedPercent)
    }

    /// Liveness on its own, for the panel row that reports process count.
    var ccLivenessLamp: StatusLamp {
        LampRules.cc(processes: ccProcesses, rateLimited: ccRateLimited)
    }

    var codexLamp: StatusLamp { LampRules.quota(usedPercent: codexUsedPercent) }
    var grokLamp: StatusLamp { LampRules.quota(usedPercent: grokUsedPercent) }

    /// Panel summary beside the GROK row; the menu bar reads the same figure.
    var grokQuotaSummary: String {
        if let grokUsedPercent { return "已用 " + StatsFormat.percent(grokUsedPercent) }
        return grokQuotaNotApplicable ? "不适用" : "待更新"
    }

    /// Menu bar position 1 — CC weekly quota, symmetric with Codex beside it
    /// (owner, 2026-07-25: "一个 CC 的，一个 Codex 的").
    var ccMenuBarText: String { StatsFormat.percent(ccUsedPercent) }
    /// Menu bar position 2 — Codex weekly quota.
    var codexMenuBarText: String { StatsFormat.percent(codexUsedPercent) }

    /// 今天真花掉的 token：新输入 + 缓存创建 + 输出。**不含缓存复读**。
    ///
    /// 面板原先报 `inputTokens + outputTokens`，而 `inputTokens` 里含
    /// cache_read——2026-07-29 实测 1.2245 亿的输入里 1.2069 亿是缓存复读，
    /// 于是「今日 token」显示 1.2 亿，真实工作量只有 37 万。数字大三百倍，
    /// 且它的涨跌主要取决于上下文被重读几遍，跟今天干了多少活几乎无关。
    ///
    /// 缓存创建是今天实际写出的新缓存，必须进入工作量；缓存复读则单独由
    /// `cacheReadTokens` 报，并在文案上说清它不进入这个总量。
    ///
    /// 三个组成项缺一就返回未知。老快照没有 `cache_creation_tokens`，把缺失
    /// 当 0 会制造一个看似精确但实际偏低的总量。
    var workingTokens: Int? {
        guard let inputTokensUncached, let cacheCreationTokens, let outputTokens else {
            return nil
        }
        let (inputAndCache, firstOverflow) =
            inputTokensUncached.addingReportingOverflow(cacheCreationTokens)
        let (total, secondOverflow) = inputAndCache.addingReportingOverflow(outputTokens)
        guard !firstOverflow, !secondOverflow else { return nil }
        return total
    }

    var outputShare: String? { StatsFormat.share(outputTokens, of: workingTokens) }

    /// Reviewed bill facts remain visible even when agent attribution is
    /// unknown. The two state axes must never collapse into one another.
    let billingFacts: BillingFacts?
    let billingAttribution: BillingAttribution?
    /// Present only when the exporter feature flag supplied one current,
    /// conserving ComputeCost shadow bundle. Legacy fields remain alongside it.
    let managedCostShadow: ManagedCostShadow?

    /// 本月各 Agent 用掉多少输出 token，以及按占比估算的分摊。
    ///
    /// 2026-07-30 这块被一版更严格的 billing 实现顶掉：新判据要求 turn.end
    /// 显式带 billing_component_id，obs 里没有，于是归因永远「证据不足」，
    /// 而原来能跑出数的实现被一并删除。面板上「每个 Agent 花了多少」就此
    /// 没了下文，且没有任何东西接替 —— 大鸟 2026-07-31 指出这一点，此处恢复。
    ///
    /// 与 `billingAttribution` 并存，两者答的不是同一个问题：那个是账单
    /// 归因（$270 由哪些组件构成，精确到分，无证据就说无证据）；这个是用量
    /// 分摊（token 花在谁身上，按输出占比估算）。呈现上必须让读者分得清，
    /// 不能把估算当账单。
    let mtdAgentCosts: [AgentMonthlyCost]?
    let mtdOutputTokens: Int?
    /// obs 实际覆盖了几天——月累计可能缺天（轮转/清理/换机），报「本月」
    /// 却只覆盖 17 天而不说，读者会以为那是整月的数。
    let mtdDaysCovered: Int?
    private let billingAttributionQuality: MetricQuality?
    private let billingAttributionReason: String?

    /// 花得多的在前——这张表的用处就是看谁花得多。
    var visibleMtdAgentCosts: [AgentMonthlyCost] {
        Array((mtdAgentCosts ?? []).prefix(PanelThresholds.monthlyCostRows))
    }

    var hiddenMtdAgentCount: Int {
        max(0, (mtdAgentCosts?.count ?? 0) - PanelThresholds.monthlyCostRows)
    }

    var hiddenMtdAmortizedUSD: Double {
        (mtdAgentCosts ?? [])
            .dropFirst(PanelThresholds.monthlyCostRows)
            .reduce(0) { $0 + $1.amortizedUSD }
    }

    /// 口径一句话说清：分摊基准是什么、数据覆盖了几天。
    var mtdUsageNote: String? {
        guard mtdAgentCosts?.isEmpty == false else { return nil }
        let base = "按各家输出 token 占比估算，非账单归因"
        guard let days = mtdDaysCovered else { return base }
        return base + "；数据覆盖本月 \(days) 天"
    }

    /// 今天的用量读数为什么可能偏低。
    ///
    /// 只在真的跳过了行时才出现：`nil` 是「没有坏行」或「这个组件根本没上报」，
    /// 两者都不需要打扰读者。0 不会出现（生产者只在 > 0 时才写这个 key），
    /// 但真写了 0 也照样不显示——0 条坏行没什么可说的。
    var usageQualityNote: String? {
        guard let malformedLines, malformedLines > 0 else { return nil }
        return "另有 \(malformedLines) 行日志无法解析已跳过，今日用量是下界"
    }

    var visibleBillingAgents: [AgentBilling] {
        Array((billingAttribution?.agents ?? []).prefix(PanelThresholds.monthlyCostRows))
    }

    var hiddenBillingAttributedCents: Int {
        (billingAttribution?.agents ?? [])
            .dropFirst(PanelThresholds.monthlyCostRows)
            .reduce(0) { $0 + $1.attributedCents }
    }

    /// Codex 的固定 $20 没有 Agent 级来源可依，必须单列而不是摊给 Relay
    /// Agent——摊出去的那一刻，读者就无法再分辨哪一笔有证据、哪一笔是猜的。
    var codexUnattributedCents: Int? {
        guard let facts = billingFacts else { return nil }
        let codex = facts.components.filter {
            $0.componentID == "codex"
                && $0.attribution == "unattributed_no_agent_source"
        }
        guard !codex.isEmpty else { return nil }
        return codex.reduce(0) { $0 + $1.amountCents }
    }

    var billingAttributionMessage: String? {
        guard billingFacts != nil, billingAttribution == nil else { return nil }
        if billingAttributionQuality == .unavailable {
            let damaged =
                billingAttributionReason?.localizedCaseInsensitiveContains(
                    "invalid JSON"
                ) == true
                || billingAttributionReason?.localizedCaseInsensitiveContains(
                    "unreadable"
                ) == true
            return damaged
                ? "Agent归因源不可用；历史观测损坏"
                : "Agent归因源不可用"
        }
        let sourceUnproven =
            billingAttributionReason?.contains("billing_component_id") == true
            || billingAttributionReason?.localizedCaseInsensitiveContains(
                "provenance"
            ) == true
        return sourceUnproven
            ? "Agent归因证据不足；CC待来源确认"
            : "Agent归因证据不足"
    }

    var billingCoverageNote: String? {
        guard let period = billingAttribution?.period else { return nil }
        if period.coverage == .partial {
            return "完整覆盖 \(period.daysCovered)/\(period.daysExpected) 天（无完备标记）"
        }
        return "完整覆盖 \(period.daysCovered)/\(period.daysExpected) 天"
    }

    /// Agents running more than one session at once (spec §2.4).
    var multiSessionAgents: [AgentUsage] {
        (agents ?? []).filter(\.hasMultipleSessions)
    }

    /// Spec §2.2 item 1. Ordered worst-first; empty means genuinely nothing to
    /// do, which the view renders as "没事".
    var concerns: [Concern] {
        var result: [Concern] = []

        if let concern = quotaConcern(
            key: "cc_quota",
            product: "Claude",
            usedPercent: ccUsedPercent,
            resetsAt: ccResetsAt
        ) {
            result.append(concern)
        }

        if let concern = quotaConcern(
            key: "codex_quota",
            product: "Codex",
            usedPercent: codexUsedPercent,
            resetsAt: codexResetsAt
        ) {
            result.append(concern)
        }

        if ccRateLimited == true {
            result.append(
                Concern(
                    key: "cc_rate_limited",
                    lamp: .red,
                    title: "这一天里撞到过用量上限",
                    detail: "有请求被挡下来了，回复可能已经受影响"
                )
            )
        }

        if ccProcesses == 0 {
            result.append(
                Concern(
                    key: "cc_down",
                    lamp: .red,
                    title: "现在一个对话进程都没有",
                    detail: "消息进来可能没人接"
                )
            )
        }

        for agent in multiSessionAgents {
            let count = agent.sessionCount ?? 0
            let leftovers = leftoverDescription(for: agent.agent)
            result.append(
                Concern(
                    key: "multi_session_\(agent.agent)",
                    lamp: .amber,
                    title: "\(agent.agent) 同时开着 \(count) 个对话",
                    detail: leftovers
                )
            )
        }

        if let failures, failures > 0 {
            result.append(
                Concern(
                    key: "failures",
                    lamp: .amber,
                    title: "今天有 \(failures) 次回合没跑完",
                    detail: "共 \(StatsFormat.grouped(turns)) 个回合"
                )
            )
        }

        return result
    }

    /// Concern presence, severity and urgency wording all start from the same
    /// quota rule used by both visible lamps. Exhaustion at 100% is a factual
    /// wording refinement inside the already-red state, not another lamp gate.
    private func quotaConcern(
        key: String,
        product: String,
        usedPercent: Double?,
        resetsAt: Date?
    ) -> Concern? {
        let lamp = LampRules.quota(usedPercent: usedPercent)
        guard let usedPercent, lamp == .amber || lamp == .red else { return nil }
        let state: String
        if usedPercent >= 100 {
            state = "用完了"
        } else if lamp == .red {
            state = "告急"
        } else {
            state = "快用完了"
        }
        let reset = resetsAt.map { "\(StatsFormat.clockUTC($0)) 重置（UTC）" } ?? "重置时间未知"
        return Concern(
            key: key,
            lamp: lamp,
            title: "\(product) 这周的额度\(state)",
            detail: "已用 \(StatsFormat.percent(usedPercent))，\(reset)"
        )
    }

    /// "2 个是老的（最后说话 01:48 / 04:32）" — plain words, UTC clock only,
    /// session ids stay out of the sentence (they are in the leaderboard).
    private func leftoverDescription(for agent: String) -> String? {
        let owned = (sessions ?? []).filter { $0.agent == agent }
        guard owned.count > 1 else { return nil }
        let stale = owned.dropFirst()
        let times = stale.compactMap { $0.lastActivity }.map(StatsFormat.timeOnlyUTC)
        guard !times.isEmpty else { return "\(stale.count) 个可能是没关掉的旧对话" }
        return "\(stale.count) 个可能是没关掉的旧对话（最后说话 \(times.joined(separator: " / "))，UTC）"
    }
}

// MARK: - 告警分诊（v3 三段式面板）

/// One alert, split into the parts the panel renders separately.
///
/// The exporter packs a whole line into `reason` (spec v3 §6 / S1):
///
/// ```
/// 07-26 10:15 起 · 最近 10:20 · 21 次 · pulse｜发生什么：…影响：…行动：…
/// ```
///
/// The `｜` divides **metadata the exporter computed** from **the escalation's
/// own words**. They are rendered at different weights, so they are split here
/// rather than in the view: this is parsing, and the view should receive
/// finished strings.
///
/// The times in `meta` are already 北京时间 — the exporter renders in
/// `DISPLAY_TZ` (spec §7.5). Re-converting them here would shift them a second
/// time, so nothing in this type touches a time zone.
struct TriagedAlert: Equatable, Identifiable, Sendable {
    let id: String
    let tier: AlertTier
    let title: String
    /// Everything before `｜` — when it happened, how often, whose it is.
    let meta: String?
    /// Everything after `｜` — the escalation's own message, in its own words.
    let body: String
    let occurredAt: Date
    let workItemID: String?
    let workItem: InFlightWorkItem?

    init(_ alert: Alert, workItem: InFlightWorkItem? = nil) {
        workItemID = alert.workItemID
        self.workItem = workItem?.workItemID == alert.workItemID ? workItem : nil
        id = alert.eventID
        tier = alert.tier
        title = alert.title
        occurredAt = alert.occurredAt

        // `｜` is U+FF5C, the full-width bar the exporter writes. Split on the
        // first one only: the escalation body may legitimately contain more.
        if let separator = alert.reason.range(of: "｜") {
            let head = alert.reason[..<separator.lowerBound].trimmingCharacters(in: .whitespaces)
            let tail = alert.reason[separator.upperBound...].trimmingCharacters(in: .whitespaces)
            // A separator with nothing after it would leave an empty body and
            // an orphaned meta line; keep the text visible in that case.
            meta = tail.isEmpty ? nil : (head.isEmpty ? nil : head)
            body = tail.isEmpty ? head : tail
        } else {
            meta = nil
            body = alert.reason
        }
    }
}

/// The panel's triage content, arranged by "现在还在不在 / 你要不要管".
///
/// Reducer-backed open incidents keep their own current-state projection.
/// Event-only alerts retain v3's three tiers (`act / watch / done`) instead of
/// being mixed with incidents merely because both arrived as JSON arrays.
struct GlobalAuthorityViewModel: Equatable, Sendable {
    private static let supportedSchema = "pulse_stats_global_read_model/v1"
    let projection: GlobalAuthorityProjection?

    private var isValidated: Bool {
        guard let projection else { return false }
        return projection.schemaVersion == Self.supportedSchema
            && projection.status == "validated"
            && projection.scope == "global"
            && projection.freshness == "fresh"
            && projection.requiredEndpointCount.map { $0 > 0 } == true
            && projection.materializedEndpointCount == projection.requiredEndpointCount
    }

    var lamp: StatusLamp {
        guard let projection else { return .gray }
        if isValidated { return .green }
        guard projection.schemaVersion == Self.supportedSchema else { return .gray }
        return projection.status == "partial" ? .amber : .gray
    }

    var title: String {
        guard let projection else { return "WI 账本未核验" }
        if isValidated { return "WI 账本已核验" }
        guard projection.schemaVersion == Self.supportedSchema else { return "WI 账本核验格式不兼容" }
        return projection.status == "partial" ? "WI 账本读取不完整" : "WI 账本未核验"
    }

    var detail: String {
        guard let projection else { return "尚未取得 WI 账本核验" }
        if isValidated {
            let revision = projection.authorityRevision.map(Self.shortDigest) ?? "revision 未知"
            return "WI 账本已完整读取，与本次快照为同一版本。\n\(revision)"
        }
        if projection.degradedReasons.contains("source_revision") {
            return "核验报告对应的账本版本与当前读取版本不同。\n这不是已发现数据损坏；本次只展示可读取的事项。"
        }
        guard !projection.degradedReasons.isEmpty else { return title }
        let labels = ["authority_revision":"账本版本依据", "source_revision":"实际读取版本",
                      "required_endpoint_coverage":"账本范围", "materialization":"账本读取完整性", "freshness":"数据时效"]
        let missing = projection.degradedReasons.map { labels[$0] ?? $0 }
        return "待核验：\(missing.joined(separator: "、"))"
    }

    private static func shortDigest(_ value: String) -> String {
        guard value.hasPrefix("sha256:") else { return value }
        return "rev " + value.dropFirst(7).prefix(8)
    }
}

struct TriageViewModel: Equatable, Sendable {
    let act: [TriagedAlert]
    let watch: [TriagedAlert]
    let done: [TriagedAlert]
    /// Current-state incidents are not alerts: reducer-backed open conditions
    /// stay visible until explicit recovery, while `alerts` are event-only
    /// notices. Keep a separate projection so neither is duplicated into the
    /// other's cards.
    let openIncidents: [Incident]
    /// Monitoring evidence and resource/business incidents are different scopes.
    /// Keep every reducer record; this is presentation, never a synthetic recovery.
    var monitoringIncidents: [Incident] { openIncidents.filter { $0.capabilityID == "meta.probe_heartbeat" } }
    var operationalIncidents: [Incident] { openIncidents.filter { $0.capabilityID != "meta.probe_heartbeat" } }
    func incidentLamp(platformLamp: StatusLamp) -> StatusLamp {
        if platformLamp == .red || operationalIncidents.contains(where: { $0.severity == .critical }) { return .red }
        if platformLamp == .amber || !operationalIncidents.isEmpty { return .amber }
        return platformLamp
    }
    let monitoringEvidenceAvailable: Bool
    /// The producer's own counts, used for headlines. See `AlertTiers`.
    let tiers: AlertTiers
    let hasSnapshot: Bool
    let authority: GlobalAuthorityViewModel

    /// Footer capability tally (spec §4): all four implementation states form
    /// a complete, mutually exclusive partition of the inventory.
    let capabilityTotal: Int
    let capabilityValidated: Int
    let capabilityPartial: Int
    let capabilityNotImplemented: Int
    let capabilityRetired: Int

    let generatedAt: Date?
    let producerName: String?
    private let statusSummary: SnapshotStatusSummary

    /// Deterministic snapshot-only convenience for tests and non-transport
    /// callers. The live App must use the designated initializer below with
    /// `SnapshotStore.presentation`, which adds cache/expiry degradation.
    init(snapshot: PulseSnapshot?) {
        self.init(
            snapshot: snapshot,
            presentation: StatusPresentation(
                runtimeHealth: snapshot?.overall.runtimeHealth ?? .unknown,
                coverage: snapshot?.overall.coverage ?? .unknown
            )
        )
    }

    init(snapshot: PulseSnapshot?, presentation: StatusPresentation) {
        hasSnapshot = snapshot != nil
        authority = GlobalAuthorityViewModel(projection: snapshot?.globalAuthority)
        tiers = snapshot?.alertTiers ?? .zero
        statusSummary = SnapshotStatusSummary(
            hasSnapshot: snapshot != nil,
            actCount: snapshot?.alertTiers.act ?? 0,
            presentation: presentation
        )
        generatedAt = snapshot?.generatedAt
        producerName = snapshot?.provenance.producer.name

        let alerts = snapshot?.alerts ?? []
        // Join only the explicit relation to the same snapshot's WI rows.
        // Missing or duplicate records do not borrow identity from display text.
        let inventory = Dictionary(grouping: SnapshotMetrics(snapshot).inFlightWorkItems ?? [], by: \.workItemID)
        let triaged = alerts.map { alert in
            let matches = alert.workItemID.flatMap { inventory[$0] } ?? []
            return TriagedAlert(alert, workItem: matches.count == 1 ? matches.first : nil)
        }.sorted { $0.occurredAt > $1.occurredAt }
        act = triaged.filter { $0.tier == .act }.sorted {
            switch ($0.workItem, $1.workItem) {
            case let (left?, right?) where left.workItemID != right.workItemID:
                return InFlightWorkItem.creationComesFirst(left, right)
            case (_?, nil): return true
            case (nil, _?): return false
            default: return $0.occurredAt == $1.occurredAt ? $0.id < $1.id : $0.occurredAt > $1.occurredAt
            }
        }
        watch = triaged.filter { $0.tier == .watch }
        done = triaged.filter { $0.tier == .done }

        // Product contract: critical → warning → info, then most recently
        // updated. Nil timestamps are honest unknowns and sort last; identity
        // keys make every tie deterministic.
        openIncidents = (snapshot?.incidents ?? [])
            .filter { $0.state == .open }
            .sorted(by: Self.incidentComesFirst)

        let capabilities = snapshot?.capabilities ?? []
        let heartbeatCapability = capabilities.first { $0.id == "meta.probe_heartbeat" }
        monitoringEvidenceAvailable = heartbeatCapability?.freshness.state == .fresh
            || heartbeatCapability?.freshness.state == .stale
        capabilityTotal = capabilities.count
        capabilityValidated = capabilities.filter { $0.implementationStatus == .activeValidated }.count
        capabilityPartial = capabilities.filter { $0.implementationStatus == .activePartial }.count
        capabilityNotImplemented = capabilities.filter { $0.implementationStatus == .notImplemented }.count
        capabilityRetired = capabilities.filter { $0.implementationStatus == .retired }.count
    }

    private static func incidentComesFirst(_ lhs: Incident, _ rhs: Incident) -> Bool {
        let lhsRank = severityRank(lhs.severity)
        let rhsRank = severityRank(rhs.severity)
        if lhsRank != rhsRank { return lhsRank > rhsRank }
        switch (lhs.updatedAt, rhs.updatedAt) {
        case let (lhsDate?, rhsDate?) where lhsDate != rhsDate:
            return lhsDate > rhsDate
        case (_?, nil):
            return true
        case (nil, _?):
            return false
        default:
            if lhs.incidentKey != rhs.incidentKey {
                return lhs.incidentKey < rhs.incidentKey
            }
            return lhs.episodeID < rhs.episodeID
        }
    }

    private static func severityRank(_ severity: Severity) -> Int {
        switch severity {
        case .critical: 3
        case .warning: 2
        case .info: 1
        }
    }

    /// Mirrored in the menu bar WI badge and the 「要你出手」 card.
    var actCount: Int { tiers.act }

    var statusLamp: StatusLamp {
        statusSummary.lamp
    }

    /// Header words beside the lamp: runtime state only, never the action count.
    var statusText: String? {
        statusSummary.headerText
    }

    /// Four explicit groups prevent partial or retired capabilities from
    /// disappearing into an unexplained gap. A measured empty inventory still
    /// reports all four zeroes; only a missing snapshot is "没拿到".
    var capabilityFooter: String {
        guard hasSnapshot else { return "能力清单还没拿到" }
        return "\(capabilityTotal) 项能力：\(capabilityValidated) 项已验证"
            + " · \(capabilityPartial) 项部分接入"
            + " · \(capabilityNotImplemented) 项未实现"
            + " · \(capabilityRetired) 项已退役"
    }

    /// One sentence for 已经过去, per spec §4 — a count, not a list.
    var doneSummary: String {
        guard hasSnapshot else { return "还没拿到数据" }
        guard tiers.done > 0 || !done.isEmpty else { return "近期没有已处理完的事" }
        return "系统自己处理完 \(max(tiers.done, done.count)) 件，留档不推送"
    }
}

// MARK: - 菜单栏（v3 合并项）

/// The single merged menu bar item (spec v3 §3).
///
/// v2 had two items because five figures had to go somewhere; with the three
/// harness counts pushed down into the panel, the reason to split is gone.
/// This model owns only what the bar shows: three quota slots, the WI count
/// and one status marker. Everything else about a snapshot is the panel's
/// business — Grok included, which keeps its panel row but left the bar.
struct MenuBarViewModel: Equatable, Sendable {
    let ccUsedPercent: Double?
    let codexUsedPercent: Double?
    /// agy's scarcest current bucket as a used share, so the slot reads like
    /// its neighbours; nil when the reading is stale, failed or all reset.
    let agyUsedPercent: Double?
    /// Read straight off `alert_tiers.act` — spec v3 §6: "App 端不自己数".
    let actCount: Int
    let hasSnapshot: Bool
    private let statusSummary: SnapshotStatusSummary
    private let currentPresentation: StatusPresentation
    private let requestSourceCurrent: Bool
    private let resources: OikosPlatformViewModel?

    init(
        metrics: SnapshotMetrics,
        alertTiers: AlertTiers,
        hasSnapshot: Bool,
        presentation: StatusPresentation,
        requestSourceCurrent: Bool = false,
        resources: OikosPlatformViewModel? = nil,
        now: Date? = nil
    ) {
        self.currentPresentation = presentation
        self.requestSourceCurrent = requestSourceCurrent
        self.resources = resources
        let relay = RelayViewModel(metrics: metrics, hasSnapshot: hasSnapshot, at: now)
        ccUsedPercent = relay.ccUsedPercent.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil }
        codexUsedPercent = relay.codexUsedPercent.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil }
        agyUsedPercent = relay.agyLowestRemainingPercent.flatMap { $0.isFinite && (0...100).contains($0) ? 100 - $0 : nil }
        actCount = alertTiers.act
        self.hasSnapshot = hasSnapshot
        statusSummary = SnapshotStatusSummary(
            hasSnapshot: hasSnapshot,
            actCount: alertTiers.act,
            presentation: presentation
        )
    }

    var menuQuotaText: String { codexUsedPercent.map { StatsFormat.percent($0) } ?? "—" }
    var menuCCText: String { ccUsedPercent.map { StatsFormat.percent($0) } ?? "—" }
    var menuAgyText: String { agyUsedPercent.map { StatsFormat.percent($0) } ?? "—" }
    /// Same producer-owned summary as the panel's 「要你出手」 tier.
    /// When a snapshot exists, retain its readable aggregate even if some
    /// request sources still need verification; the separate question marker
    /// carries that uncertainty. With no snapshot there is no number to show.
    var menuWIText: String { hasSnapshot ? String(actCount) : "—" }
    /// Operational marker that sits after the two numeric badges. The action
    /// count moved into the dedicated WI badge, so it must not be duplicated here.
    var menuBadge: String {
        if compactTitle == "PULSE !" { return "!" }
        if currentPresentation.pausedOvernight { return "◐" }
        if compactTitle.contains("待更新") { return "?" }
        if hasSnapshot && !requestSourceCurrent { return "?" }
        return ""
    }
    var menuAccessibilityLabel: String {
        let quota = codexUsedPercent.map { "Codex 7 天额度已用 " + StatsFormat.percent($0) } ?? "Codex 额度待更新，不以旧数或零替代"
        let cc = ccUsedPercent.map { "CC 7 天额度已用 " + StatsFormat.percent($0) } ?? "CC 额度待更新，不以旧数或零替代"
        let agy = agyUsedPercent.map { "agy 额度最紧一组已用 " + StatsFormat.percent($0) } ?? "agy 额度待更新，不以旧数或零替代"
        let wi: String
        if !hasSnapshot {
            wi = "要你出手数量待更新，不以旧数或零替代"
        } else if requestSourceCurrent {
            wi = "要你出手 \(actCount) 项"
        } else {
            wi = "已读要你出手 \(actCount) 项，部分请求来源待核验"
        }
        return "PULSE。" + quota + "。" + cc + "。" + agy + "。" + wi + "。" + compactHelp
    }

    var ccText: String { StatsFormat.percent(ccUsedPercent) }
    var codexText: String { StatsFormat.percent(codexUsedPercent) }

    var ccLamp: StatusLamp { LampRules.quota(usedPercent: ccUsedPercent) }
    var codexLamp: StatusLamp { LampRules.quota(usedPercent: codexUsedPercent) }
    var agyLamp: StatusLamp { LampRules.quota(usedPercent: agyUsedPercent) }

    /// Spec v3 §3: "两条线各管各的" — quota never colours the status word.
    /// A box at 100% quota can still read 正常 when runtime, coverage and the
    /// action tier are all clear, because "额度烧完不等于有事要人干".
    var statusLamp: StatusLamp {
        statusSummary.lamp
    }

    var statusText: String {
        statusSummary.text
    }

    /// The status sentence cut down to its first clause.
    ///
    /// The menu-bar sentence joins at most two clauses with ` · ` — action count
    /// and runtime health.
    /// Even two clauses can overrun the menu bar budget, and the old fallback
    /// resolved that by dropping the quotas. That was backwards: the quotas are
    /// the reason to look at this item at all, while "运行异常" is an internal
    /// diagnostic that can persist for days and prompts no action on its own.
    ///
    /// So when space runs short the *sentence* gives way, not the figures. The
    /// first clause is the one worth keeping — it is the one that says whether
    /// anything wants the owner. The rest stays in the panel and in VoiceOver.
    var shortStatusText: String {
        statusText.components(separatedBy: " · ").first ?? statusText
    }
    /// The menu is an entrance, not a permanent display of retired quotas.
    /// Only fresh verified human requests receive a count. Optional sources
    /// and their diagnostics stay in their own panel sections.
    var compactTitle: String {
        if !hasSnapshot { return "PULSE · 待更新" }
        if currentPresentation.runtimeHealth == .critical || currentPresentation.runtimeHealth == .warning
            || resources?.lamp == .red || resources?.lamp == .amber { return "PULSE !" }
        if currentPresentation.pausedOvernight { return "PULSE ◐" }
        if currentPresentation.runtimeHealth == .stale || currentPresentation.runtimeHealth == .unknown
            || resources?.available == false { return "PULSE · 待更新" }
        if requestSourceCurrent && actCount > 0 { return "PULSE · \(min(actCount,999)) 待参与" }
        return "PULSE"
    }
    var compactLamp: StatusLamp {
        if compactTitle == "PULSE !" { return currentPresentation.runtimeHealth == .critical || resources?.lamp == .red ? .red : .amber }
        if compactTitle.contains("待参与") { return .amber }
        return compactTitle == "PULSE" ? .green : .gray
    }
    var compactHelp: String {
        if compactTitle == "PULSE !" { return "已观测到运行或资源异常；打开查看具体情况。" }
        if currentPresentation.pausedOvernight { return "夜间按安排暂停后台刷新；打开面板仍会读取最新数据。" }
        if compactTitle.contains("待更新") { return "当前运行数据待更新；打开面板查看来源和原始采样时间。" }
        if requestSourceCurrent && actCount > 0 { return "已登记 \(actCount) 项需要你参与的事项；打开查看。" }
        return "当前已监测运行范围正常；打开查看任务、产出与成本。"
    }

}

// MARK: - harness

/// Everything the 🔧 menu bar item and its panel render.
struct HarnessViewModel: Equatable, Sendable {
    let weekCreated: Int?
    /// 自然周(北京周一清零)——大鸟 2026-07-29 要的「当周」。`weekCreated`
    /// 是滚动 7 天,天天在变没法跟上周比;这两个数必然不等,所以文案上各自
    /// 标清窗口,不许只写「本周」。
    let isoWeekCreated: Int?
    let isoWeekCompleted: Int?
    let dayCreated: Int?
    let dayCompleted: Int?
    let monthCreated: Int?
    let dayClosed: Int?
    let isoWeekClosed: Int?
    let monthClosed: Int?
    let pendingHandoffs: Int?
    let stalled: [StalledWorkItem]?
    /// 见 `MetricID.harnessUnreadableRecords`：上面各计数的可信度修饰符。
    let unreadableRecords: Int?
    let hasSnapshot: Bool

    init(metrics: SnapshotMetrics, hasSnapshot: Bool) {
        weekCreated = metrics.int(MetricID.wiWeekCreated)
        isoWeekCreated = metrics.int(MetricID.wiIsoWeekCreated)
        isoWeekCompleted = metrics.int(MetricID.wiIsoWeekCompleted)
        dayCreated = metrics.int(MetricID.wiDayCreated)
        dayCompleted = metrics.int(MetricID.wiDayCompleted)
        monthCreated = metrics.int(MetricID.wiMonthCreated)
        dayClosed = metrics.int(MetricID.wiDayClosed)
        isoWeekClosed = metrics.int(MetricID.wiIsoWeekClosed)
        monthClosed = metrics.int(MetricID.wiMonthClosed)
        pendingHandoffs = metrics.int(MetricID.handoffPending)
        stalled = metrics.stalledWorkItems
        unreadableRecords = metrics.int(MetricID.harnessUnreadableRecords)
        self.hasSnapshot = hasSnapshot
    }

    /// 仪表盘第一句：今天收口了几件、新建了几件、净额是多少。
    ///
    /// 两个数必须一起出现。单独一个「今天新建 11」读者无从判断好坏——11 件
    /// 新活配 11 件收口是持平,配 2 件收口就是在积压。净额说的是债在增还是在减。
    ///
    /// 任一数读不到就退回「不知道」,不猜、不用 0 顶替：0 是「查过了确实没有」,
    /// 与「读不到」是两回事。
    var todayLine: String {
        guard let dayCreated, let dayCompleted else {
            return "今天的进出还没读到"
        }
        let net = dayCompleted - dayCreated
        let verdict: String
        if net > 0 {
            verdict = "净减 \(net) 件"
        } else if net < 0 {
            verdict = "净增 \(-net) 件"
        } else {
            verdict = "持平"
        }
        return "今天 收口 \(dayCompleted) 件 · 新建 \(dayCreated) 件 · \(verdict)"
    }

    /// 上面那些 wi 计数为什么可能偏低。
    ///
    /// 与 `RelayViewModel.usageQualityNote` 同一个道理：跳过的记录没有进任何
    /// 一个计数，所以「今天新建 11」是下界而不是准数。只在 > 0 时出现。
    var ledgerQualityNote: String? {
        guard let unreadableRecords, unreadableRecords > 0 else { return nil }
        return "另有 \(unreadableRecords) 条记录无法解析已跳过，上列计数是下界"
    }

    /// 四个数一起看：今天进出，本周进出。
    ///
    /// 2026-07-31 我把「今天」两格删了，理由是上面 `todayLine` 已经说过一次。
    /// 大鸟当天就发现「今日的 wi 汇总也都没有了」——**那句话替代不了这两格**。
    /// 一句中文要读完才知道数字，四个格子是一眼扫过去；而且删掉之后，
    /// 「今天」与「本周」失去了并排对照，本周 42 收口配今天 1 收口是什么意思，
    /// 得靠读者自己在脑子里拼。
    ///
    /// 教训记在这里而不是 memo：**「重复」不等于「冗余」**。同一个数字用两种
    /// 形式出现，如果一种是叙述、一种是可扫视的表格，那是两个用途，不是重复。
    /// 判断一处呈现该不该删，得问「谁在用它、怎么用」，不能只看字面是否重出。
    var throughputStats: [(id: String, value: String, caption: String)] {
        [
            (
                id: "done",
                value: StatsFormat.compactCount(dayCompleted),
                caption: "今天收口"
            ),
            (
                id: "day",
                value: StatsFormat.compactCount(dayCreated),
                caption: "今天新建"
            ),
            (
                id: "wkdone",
                value: StatsFormat.compactCount(isoWeekCompleted),
                caption: "本周收口"
            ),
            (
                id: "wk",
                value: StatsFormat.compactCount(isoWeekCreated),
                caption: "本周新建"
            ),
        ]
    }

    /// `nil` when the stalled list is unavailable — distinct from an empty list,
    /// which legitimately means "nothing is stuck".
    var maxStalledDays: Int? {
        guard let stalled else { return nil }
        return stalled.map(\.days).max()
    }

    var weekLamp: StatusLamp {
        LampRules.workItemBatch(created: weekCreated, maxStalledDays: maxStalledDays)
    }

    var dayLamp: StatusLamp {
        LampRules.workItemBatch(created: dayCreated, maxStalledDays: maxStalledDays)
    }

    var completionLamp: StatusLamp {
        LampRules.completion(created: dayCreated, completed: dayCompleted)
    }

    var completionRateText: String {
        guard let dayCreated, let dayCompleted, dayCreated > 0 else { return StatsFormat.unknown }
        return StatsFormat.percent(Double(dayCompleted) / Double(dayCreated) * 100)
    }

    var weekMenuBarText: String { StatsFormat.compactCount(weekCreated) }
    var dayMenuBarText: String { StatsFormat.compactCount(dayCreated) }
    var completedMenuBarText: String { StatsFormat.compactCount(dayCompleted) }

    var concerns: [Concern] {
        var result: [Concern] = []

        if let stalled, !stalled.isEmpty {
            let longest = stalled.map(\.days).max() ?? 0
            result.append(
                Concern(
                    key: "stalled_wi",
                    lamp: longest > 14 ? .red : .amber,
                    title: "\(stalled.count) 件活挂着超过一周没动",
                    detail: "最久的一件已经 \(longest) 天"
                )
            )
        }

        if let pendingHandoffs, pendingHandoffs > 0 {
            result.append(
                Concern(
                    key: "pending_handoff",
                    lamp: .green,
                    title: "\(pendingHandoffs) 件派出去还没人接",
                    detail: "暂时不用管"
                )
            )
        }

        return result
    }
}


/// Oikos owns thresholds, confirmation and delivery. Swift only renders its states.
struct OikosNode: Equatable, Identifiable, Sendable {
    let id: String
    let label: String
    let role: String
    let status: String
    let disk: Int?
    let memory: Int?
    let swap: Int?
    let inode: Int?
    let writable: String
    let runner: String
    let runnerExpected: String
    let confirmations: Int?
    let delivery: String
    let reason: String
    let observedAt: Date
    let expiresAt: Date
    let revision: String

    init?(row: [String: MetricScalar]) {
        guard let id = row["id"]?.string, let label = row["label"]?.string,
              let status = row["status"]?.string,
              ["ok", "at_limit", "pending", "incident", "unknown"].contains(status),
              let observed = Self.date(row["observed_at"]?.string),
              let expiry = Self.date(row["expires_at"]?.string), expiry > observed,
              let revision = row["source_revision"]?.string else { return nil }
        self.id = id; self.label = label; self.status = status
        role = row["role"]?.string ?? "unknown"
        observedAt = observed; expiresAt = expiry; self.revision = revision
        for key in ["disk_percent", "memory_available_percent", "swap_percent", "inode_percent"] {
            if let number = row[key]?.number {
                guard number.isFinite, number.rounded() == number, (0...100).contains(number) else { return nil }
            } else if status != "unknown" { return nil }
        }
        guard revision.count == 40, revision.allSatisfy({ $0.isHexDigit }), expiry.timeIntervalSince(observed) <= 1500 else { return nil }
        disk = row["disk_percent"]?.int; memory = row["memory_available_percent"]?.int
        swap = row["swap_percent"]?.int; inode = row["inode_percent"]?.int
        writable = row["writable"]?.string ?? "unknown"
        if ["ok", "at_limit"].contains(status) && writable != "ok" { return nil }
        runner = row["runner_state"]?.string ?? "unknown"
        runnerExpected = row["runner_expected"]?.string ?? "unknown"
        confirmations = row["confirmation_runs"]?.int
        delivery = row["delivery"]?.string ?? "unknown"
        reason = row["reason"]?.string ?? ""
    }

    static func date(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }

    var lamp: StatusLamp {
        switch status {
        case "ok": return .green
        case "incident": return .red
        case "at_limit", "pending": return .amber
        default: return .gray
        }
    }
    var statusText: String {
        switch status {
        case "ok": return "正常"
        case "at_limit": return "容量边界"
        case "pending": return "待确认 \(confirmations.map(String.init) ?? "?")/3"
        case "incident": return "已确认异常"
        default: return "采样不完整"
        }
    }
    var deliveryText: String {
        switch delivery {
        case "accepted_or_notified": return "处置入口已接受或兜底通知已送达；尚非处理完成"
        case "not_delivered": return "处置和通知尚未送达，探针下轮重试"
        default: return "处置状态未核验"
        }
    }
}

struct OikosPlatformViewModel: Equatable, Sendable {
    let nodes: [OikosNode]
    let available: Bool
    let reason: String
    let observedAt: Date?
    var confirmedCount: Int { nodes.filter { $0.status == "incident" }.count }
    var unknownCount: Int { nodes.filter { $0.status == "unknown" }.count }
    var lamp: StatusLamp {
        if !available { return .gray }
        if confirmedCount > 0 { return .red }
        if nodes.contains(where: { $0.status == "pending" || $0.status == "at_limit" }) { return .amber }
        return unknownCount > 0 ? .gray : .green
    }
    init(metrics: SnapshotMetrics, now: Date) {
        let metric = metrics.rawMetric("oikos.platform.nodes")
        observedAt = metric?.observedAt
        let rows = metrics.rows("oikos.platform.nodes") ?? []
        let parsed = rows.compactMap(OikosNode.init(row:))
        let required: Set<String> = ["C1-ownermac", "tcloud-node2", "C3-ecs", "bigbird-sg"]
        let optional: Set<String> = ["tcloud-node3"]
        let ids = Set(parsed.map(\.id))
        let complete = parsed.count == rows.count && ids.count == parsed.count
            && required.isSubset(of: ids) && ids.isSubset(of: required.union(optional))
            && Set(parsed.map(\.observedAt)).count == 1 && Set(parsed.map(\.revision)).count == 1
        let knownPast = complete && parsed.allSatisfy { $0.observedAt <= now }
        let fresh = knownPast && parsed.allSatisfy { now < $0.expiresAt }
        available = fresh
        nodes = knownPast ? parsed : []
        let residents = parsed.filter { $0.role == "resident" }.count
        let runners = parsed.count - residents
        reason = fresh ? "\(residents) 个常驻节点 + Runner 池（\(runners) 台）；资源健康不等于全部业务链路通过"
            : complete && !knownPast ? "资源报告时间异常，等待当前采样"
            : complete ? "资源采样已过期，点击刷新取得新证据"
            : metric?.reason ?? "尚未接入 Oikos 资源快照，不能判断当前事故数"
    }
}


/// Read-only service rows. Source timestamps, not UI refresh time, bound validity.
struct AutomationTaskRow: Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let owner: String
    let status: String
    let lamp: StatusLamp
    let reason: String
    let expectation: String
    let plan: String
    let progress: String
    let node: String
    let reference: String
    let lastResultAt: Date?
    let nextAt: Date?
    let sourceAt: Date?
    /// Exporter-flagged row needing a look, such as an inbox patrol that ran late or failed.
    let attention: Bool
    /// Optional exporter line listed under 历史问题 and in the row's details.
    let note: String
    /// One purpose group from the exporter; a row without one is「用途待确认」.
    let category: String
    let categoryTitle: String
    let categoryRank: Int
    /// The plan column's two lines, e.g.「每日」over「06:15」.
    let planHead: String
    let planSub: String
    let coverage: String

    init?(row: [String: MetricScalar], now: Date) {
        guard let id = row["id"]?.string, !id.isEmpty,
              let title = row["title"]?.string, !title.isEmpty,
              let owner = row["owner"]?.string else { return nil }
        self.id = id; self.title = title; self.owner = owner
        sourceAt = OikosNode.date(row["source_at"]?.string)
        let expiry = OikosNode.date(row["expires_at"]?.string)
        let fresh: Bool
        if let sourceAt, let expiry {
            fresh = sourceAt <= now && now < expiry && expiry.timeIntervalSince(sourceAt) <= 2700
        } else { fresh = false }
        let colours = ["green": StatusLamp.green, "orange": .amber, "red": .red, "gray": .gray]
        let nativeLamp = colours[row["color"]?.string ?? ""]
        let nativeStatus = row["status"]?.string
        status = fresh && nativeLamp != nil ? nativeStatus ?? "结果待核验" : "数据待更新"
        lamp = fresh && nativeStatus?.isEmpty == false ? nativeLamp ?? .amber : .amber
        reason = fresh ? row["reason"]?.string ?? "来源尚未给出结果依据" : "任务来源已过期；不能凭旧读数判断暂停、成功或失败。"
        expectation = row["expectation"]?.string ?? "未取得任务约定"
        plan = row["plan"]?.string ?? "未取得有效计划"
        progress = fresh ? row["progress"]?.string ?? "未取得进度" : "等待新鲜来源"
        node = row["node"]?.string ?? "未知"
        reference = row["reference"]?.string ?? ""
        lastResultAt = OikosNode.date(row["last_result_at"]?.string)
        nextAt = OikosNode.date(row["next_at"]?.string)
        attention = fresh && row["attention"]?.bool == true
        note = fresh ? row["note"]?.string ?? "" : ""
        if let id = row["category"]?.string, !id.isEmpty, let title = row["category_title"]?.string, !title.isEmpty {
            category = id; categoryTitle = title; categoryRank = row["category_rank"]?.int ?? Int.max
        } else {
            category = "ungrouped"; categoryTitle = "用途待确认"; categoryRank = Int.max
        }
        if let head = row["plan_head"]?.string {
            planHead = head; planSub = row["plan_sub"]?.string ?? ""
        } else if let open = plan.range(of: " (") {
            // An exporter without plan lines: the expression over its zone.
            planHead = String(plan[..<open.lowerBound]); planSub = String(plan[open.upperBound...].dropLast())
        } else {
            planHead = plan; planSub = ""
        }
        coverage = row["coverage"]?.string ?? ""
    }

    /// Upper line of the task column: the actual node(s); a row with none says so.
    var nodeLine: String {
        node == "未运行" || node == "未知" || node.isEmpty ? "节点待定"
            : node.split(separator: ",").joined(separator: " / ")
    }
    /// The recent-result column: day over clock in 北京时间, or a plain dash.
    var lastResultLines: (String, String) {
        guard let at = lastResultAt else { return ("—", "无结果") }
        let text = StatsFormat.clockBeijing(at)
        let parts = text.split(separator: " ", maxSplits: 1).map(String.init)
        guard parts.count == 2 else { return (text, "") }
        return (parts[0].replacingOccurrences(of: "-", with: "/"), parts[1])
    }
    var isRunning: Bool { lamp == .amber && ["执行中", "排队中", "补跑中"].contains(status) }
}

/// One purpose group of the automation card.
struct AutomationTaskGroup: Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let tasks: [AutomationTaskRow]
    /// 「4 项 · 1 失败 · 2 待核对 · 1 未运行」; colour words count rows, not jobs.
    var summary: String {
        let failed = tasks.filter { $0.lamp == .red }.count
        let pending = tasks.filter { $0.lamp == .amber && !$0.isRunning }.count
        let idle = tasks.filter { $0.lamp == .gray }.count
        return (["\(tasks.count) 项"] + [(failed, "失败"), (pending, "待核对"), (idle, "未运行")]
            .filter { $0.0 > 0 }.map { "\($0.0) \($0.1)" }).joined(separator: " · ")
    }
}

struct AutomationTasksViewModel: Equatable, Sendable {
    let tasks: [AutomationTaskRow]
    init(metrics: SnapshotMetrics, now: Date) {
        let raw = metrics.rows("automation.tasks") ?? []
        let parsed = raw.compactMap { AutomationTaskRow(row: $0, now: now) }
        tasks = parsed.count == raw.count && Set(parsed.map(\.id)).count == parsed.count ? parsed : []
        var order: [String] = []
        var members: [String: [AutomationTaskRow]] = [:]
        for task in tasks.enumerated().sorted(by: { ($0.element.categoryRank, $0.offset) < ($1.element.categoryRank, $1.offset) }).map(\.element) {
            if members[task.category] == nil { order.append(task.category) }
            members[task.category, default: []].append(task)
        }
        groups = order.map { id in
            AutomationTaskGroup(id: id, title: members[id]![0].categoryTitle, tasks: members[id]!)
        }
    }
    let groups: [AutomationTaskGroup]
    /// Purpose groups other than「用途待确认」.
    var purposeCount: Int { groups.filter { $0.id != "ungrouped" }.count }
    /// 「1 项失败 · 4 项待核对 · 1 项执行中 · 6 项未运行」, or a calm line.
    var summary: String {
        let parts = [(tasks.filter { $0.lamp == .red }.count, "失败"),
                     (tasks.filter { $0.lamp == .amber && !$0.isRunning }.count, "待核对"),
                     (tasks.filter(\.isRunning).count, "执行中"),
                     (tasks.filter { $0.lamp == .gray }.count, "未运行")].filter { $0.0 > 0 }
        return parts.isEmpty ? "本期没有失败、待核对或未运行的任务" : parts.map { "\($0.0) 项\($0.1)" }.joined(separator: " · ")
    }
    /// Rows whose earlier rounds stay open; listed apart, never merged into this round.
    var history: [AutomationTaskRow] { tasks.filter { $0.attention && !$0.note.isEmpty } }
    /// The service rows' read coverage of node sources, e.g.「已读 3/3 个节点的任务来源」.
    var coverage: String { tasks.first { $0.coverage.hasPrefix("已读") }?.coverage ?? "" }
}

/// 「在办」的一行：一张协作合同或一件 WI。全部文字由 exporter 生成，App 只排版。
struct WorkBoardEntry: Identifiable, Equatable, Sendable {
    enum Kind: String, Sendable { case contract, wi }
    let id: String
    let kind: Kind
    let agent: String
    /// 方向 + 对方 + 目的首句（合同），或摘要（WI）。
    let line: String
    /// 右侧状态与时长，如「待接 5 天」「17 时前」。
    let status: String
    /// 卡住：合同超时未接或待发起方处理；WI 停滞。
    let stuck: Bool
    /// 合同只投递给主身。WI 不用这个标记分类。
    let mainOnly: Bool
    let workItemID: String
    let workItemTitle: String
    // 点开后显示的核对凭据。
    let route: String
    let purpose: String
    let next: String
    let timeline: [String]
    let owner: String
    let holder: String
    let updatedAt: Date?
    let createdAt: Date?
    /// 同一件 WI 已在「要你出手」；这里只加标记，不展开第二份。
    var inAct = false

    init?(row: [String: MetricScalar]) {
        guard let kind = row["kind"]?.string.flatMap(Kind.init(rawValue:)),
              let id = row["id"]?.string, !id.isEmpty,
              let agent = row["agent"]?.string, !agent.isEmpty,
              let line = row["line"]?.string,
              let status = row["status"]?.string,
              let stuck = row["stuck"]?.bool else { return nil }
        self.kind = kind; self.id = id; self.agent = agent; self.line = line
        self.status = status; self.stuck = stuck
        mainOnly = row["main_only"]?.bool == true
        workItemID = row["work_item_id"]?.string ?? ""
        workItemTitle = row["work_item_title"]?.string ?? ""
        route = row["route"]?.string ?? ""
        purpose = row["purpose"]?.string ?? ""
        next = row["next"]?.string ?? ""
        timeline = (row["timeline"]?.string ?? "").split(separator: "\n").map(String.init)
        owner = row["owner"]?.string ?? ""
        holder = row["holder"]?.string ?? ""
        updatedAt = OikosNode.date(row["updated_at"]?.string)
        createdAt = OikosNode.date(row["created_at"]?.string)
    }
}

/// 一个折叠行：一个 Agent，或几个各只有一件在推进 WI 的 Agent 合成的一行。
struct WorkBoardGroup: Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    /// 「WI 2」；合成行与没有 WI 的 Agent 为 nil。
    let countText: String?
    /// 折叠行的说明，如「待接合同 4（最久 5 天） · 交回待处理 2」。
    let summary: String
    let stuck: Bool
    let entries: [WorkBoardEntry]
}

struct WorkBoardViewModel: Equatable, Sendable {
    /// nil：看板读不到或行数与总数对不上，不能当作“没有在办”。
    let groups: [WorkBoardGroup]?
    let wiTotal: Int
    let contractTotal: Int
    /// 合同来源可用；不可用时只显示 WI。
    let contractsAvailable: Bool

    var countText: String? {
        groups == nil ? nil : "WI \(wiTotal) · 合同 " + (contractsAvailable ? "\(contractTotal)" : "待核验")
    }
    var stuckCount: Int { (groups ?? []).reduce(0) { $0 + $1.entries.filter(\.stuck).count } }

    init(metrics: SnapshotMetrics, actWorkItemIDs: Set<String> = []) {
        let rows = metrics.rows(MetricID.workBoard) ?? []
        guard let head = rows.first, head["kind"]?.string == "board",
              let wiTotal = head["wi_total"]?.int, let contractTotal = head["contract_total"]?.int else {
            groups = nil; self.wiTotal = 0; self.contractTotal = 0; contractsAvailable = false
            return
        }
        self.wiTotal = wiTotal
        self.contractTotal = contractTotal
        contractsAvailable = head["contracts_available"]?.bool == true
        var agents: [(row: [String: MetricScalar], entries: [WorkBoardEntry])] = []
        var valid = true
        for row in rows.dropFirst() {
            if row["kind"]?.string == "agent" {
                agents.append((row, []))
            } else if var entry = WorkBoardEntry(row: row), !agents.isEmpty,
                      entry.agent == agents[agents.count - 1].row["agent"]?.string {
                entry.inAct = entry.kind == .wi && actWorkItemIDs.contains(entry.workItemID)
                agents[agents.count - 1].entries.append(entry)
            } else {
                valid = false
            }
        }
        let entries = agents.flatMap(\.entries)
        // 计数守恒：每件 WI、每张合同恰好一行，且与 exporter 的总数一致。
        let conserved = valid
            && entries.filter { $0.kind == .wi }.count == wiTotal
            && entries.filter { $0.kind == .contract }.count == contractTotal
            && Set(entries.map(\.id)).count == entries.count
            && agents.allSatisfy { !$0.entries.isEmpty }
        guard conserved else { groups = nil; return }
        var result: [WorkBoardGroup] = []
        var quiet: [(name: String, entry: WorkBoardEntry)] = []
        var quietSlot: Int?
        for (row, members) in agents {
            let name = row["name"]?.string ?? members[0].agent
            let stuck = members.contains(where: \.stuck)
            let wis = members.filter { $0.kind == .wi }.count
            // 只有一件在推进的 WI、没有合同的 Agent 合成一行，避免一屏全是单件 Agent。
            if !stuck && wis == 1 && members.count == 1 {
                quiet.append((name, members[0]))
                if quietSlot == nil { quietSlot = result.count }
                continue
            }
            result.append(WorkBoardGroup(id: row["id"]?.string ?? "agent:" + members[0].agent, title: name,
                countText: wis > 0 ? "WI \(wis)" : nil, summary: row["summary"]?.string ?? "", stuck: stuck, entries: members))
        }
        if quiet.count == 1, let slot = quietSlot {
            let only = agents.first { $0.entries.first?.id == quiet[0].entry.id }
            result.insert(WorkBoardGroup(id: "agent:" + quiet[0].entry.agent, title: quiet[0].name, countText: "WI 1",
                summary: only?.row["summary"]?.string ?? "", stuck: false, entries: [quiet[0].entry]), at: slot)
        } else if quiet.count > 1, let slot = quietSlot {
            result.insert(WorkBoardGroup(id: "agents:" + quiet.map(\.entry.agent).joined(separator: ","),
                title: quiet.map(\.name).joined(separator: " · "), countText: nil,
                summary: "WI 各 1 · 均在推进", stuck: false, entries: quiet.map(\.entry)), at: slot)
        }
        groups = result
    }
}
