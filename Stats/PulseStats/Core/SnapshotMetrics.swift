import Foundation

/// Strongly-typed reader over the flat `metrics` array.
///
/// Rule that governs this whole file: **a missing, unknown, or wrong-shaped
/// metric reads as `nil`, never as 0.** Every accessor returns an optional and
/// the views render "未知" on `nil`. There is no default anywhere below.
struct SnapshotMetrics: Equatable, Sendable {
    private let byID: [String: Metric]

    init(_ metrics: [Metric]) {
        byID = Dictionary(metrics.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
    }

    init(_ snapshot: PulseSnapshot?) {
        self.init(snapshot?.metrics ?? [])
    }

    func metric(_ id: String) -> Metric? {
        guard let metric = byID[id] else { return nil }
        // `warming_up` / `unknown` / `not_applicable` carry no usable reading.
        guard metric.status == .available else { return nil }
        return metric
    }

    /// Raw state is needed only where an unavailable metric's quality/reason
    /// drives an explicit "why unknown" label. It never exposes a usable value.
    func rawMetric(_ id: String) -> Metric? { byID[id] }

    /// Scalar reading. `nil` when the metric is absent, not yet available, or
    /// not a number.
    func double(_ id: String) -> Double? {
        metric(id)?.value.number
    }

    func int(_ id: String) -> Int? {
        safeInt(double(id)?.rounded())
    }

    func bool(_ id: String) -> Bool? {
        metric(id)?.value.boolean
    }

    func numbers(_ id: String) -> [Double]? {
        metric(id)?.value.numbers
    }

    func counts(_ id: String) -> [String: Int]? {
        guard let raw = metric(id)?.value.counts else { return nil }
        return raw.compactMapValues { safeInt($0.rounded()) }
    }

    func rows(_ id: String) -> [[String: MetricScalar]]? {
        metric(id)?.value.rows
    }

    func observedAt(_ id: String) -> Date? {
        metric(id)?.observedAt
    }

    var isEmpty: Bool { byID.isEmpty }
}

// MARK: - Metric id namespace (spec §4)

enum MetricID {
    static let ccProcesses = "relay.cc.processes"
    static let ccBusy = "relay.cc.busy"
    static let ccIdle = "relay.cc.idle"
    static let ccRateLimited = "relay.cc.rate_limited"
    static let botsOnline = "relay.bots_online"
    static let uptimeSeconds = "relay.uptime_seconds"
    static let ccUsedPercent = "relay.cc.used_percent"
    static let ccResetsAt = "relay.cc.resets_at"
    static let ccFiveHourUsedPercent = "relay.cc.5h_used_percent"
    static let codexUsedPercent = "relay.codex.used_percent"
    static let codexResetsAt = "relay.codex.resets_at"
    /// Grok account weekly pool, read by the cost publisher. Not per-bot cost.
    static let grokUsedPercent = "relay.grok.used_percent"
    static let grokResetsAt = "relay.grok.resets_at"
    static let grokPeriodStart = "relay.grok.period_start"
    static let grokPlanLabel = "relay.grok.plan_label"
    /// agy account quota: one row per current bucket (group, window, remaining_fraction, resets_at).
    static let agyQuota = "relay.agy.quota"

    /// 真正的新输入。`input_tokens` 是「新输入 + 缓存复读 + 缓存写入」的合计,
    /// 实测 1.2245 亿里 1.2069 亿是 cache_read——把它当「今天用了多少」会把
    /// 「重读了多少遍旧上下文」报成工作量,数字大一万倍且与工作量几乎无关。
    static let inputTokensUncached = "usage.today.input_tokens_uncached"
    static let cacheReadTokens = "usage.today.cache_read_tokens"
    static let cacheCreationTokens = "usage.today.cache_creation_tokens"
    static let inputTokens = "usage.today.input_tokens"
    static let outputTokens = "usage.today.output_tokens"
    static let turns = "usage.today.turns"
    static let failures = "usage.today.failures"
    static let inbound = "usage.today.inbound"
    static let inboundTypes = "usage.today.inbound_types"
    static let durationP50 = "usage.today.duration_p50"
    static let hourlyOutputTokens = "usage.hourly.output_tokens"
    static let byAgent = "usage.by_agent"
    static let bySession = "usage.by_session"

    static let billingPeriod = "billing.mtd.period"
    static let billingComponents = "billing.mtd.components"
    static let billingByAgent = "billing.mtd.by_agent"
    static let billingAttributedCents = "billing.mtd.attributed_cents"
    static let billingUnattributedCents = "billing.mtd.unattributed_cents"
    static let billingTotalCents = "billing.mtd.total_cents"
    /// 本月累计 + 订阅摊销(大鸟 2026-07-29：「摊销是针对 Agent 的，而不是
    /// 每一天」)。按各 agent 占本月输出 token 的比例分 $270。
    static let mtdByAgent = "usage.mtd.by_agent"
    static let mtdOutputTokens = "usage.mtd.output_tokens"
    static let mtdDaysCovered = "usage.mtd.days_covered"
    static let mtdSubscriptionUSD = "usage.mtd.subscription_usd"
    /// ComputeCost S2 is opt-in shadow data. Absence is the default and must
    /// leave all legacy billing/usage rendering unchanged.
    static let managedCostShadowSummary = "compute_cost.shadow.summary"
    static let managedCostShadowByAgent = "compute_cost.shadow.by_agent"

    static let deliverySent = "delivery.today.sent"
    static let deliveryAttempts = "delivery.today.attempts_histogram"

    static let wiWeekCreated = "harness.wi.week_created"
    /// 自然周(北京时区周一清零)。与 `wiWeekCreated`(滚动 7 天)是两个窗口,
    /// 大鸟 2026-07-29 要的是自然周——能跟上周比,滚动窗口天天在变没法比。
    static let wiIsoWeekCreated = "harness.wi.iso_week_created"
    static let wiIsoWeekCompleted = "harness.wi.iso_week_completed"
    static let wiDayCreated = "harness.wi.day_created"
    static let wiDayCompleted = "harness.wi.day_completed"
    /// Asia/Shanghai calendar periods ending today. Closed counts include
    /// completed and terminated WorkItems by their closing time.
    static let wiMonthCreated = "harness.wi.month_created"
    static let wiDayClosed = "harness.wi.day_closed"
    static let wiIsoWeekClosed = "harness.wi.iso_week_closed"
    static let wiMonthClosed = "harness.wi.month_closed"
    static let wiStalled = "harness.wi.stalled"
    static let wiInFlight = "harness.wi.in_flight"
    static let workBoard = "work.board"
    static let handoffPending = "harness.handoff.pending"

    /// 聚合时跳过的坏 YAML 记录数。**只在 > 0 时才存在**——生产者那侧写着
    /// `if unreadable:` 才 append，所以平时整个 key 读不到，这是正常的。
    ///
    /// 它不是一个「指标」，是上面那些 wi 计数的可信度修饰符：跳过了 3 条，
    /// 就意味着「今天新建 11」实际可能是 14。因此它没有自己的格子，只在
    /// 对应板块的注脚里追一句——常驻格子 99% 的时间显示 `--`，那种行对读者
    /// 不构成任何行动。
    static let harnessUnreadableRecords = "harness.unreadable_records"

    /// 解析用量日志时跳过的坏行数。同样只在 > 0 时存在，同样是修饰符而非
    /// 指标——坏行没有事件日归属，所以「今天」的用量是个下界，不是准数。
    static let usageMalformedLines = "usage.today.malformed_lines"
}

// MARK: - Row models

struct AgentUsage: Equatable, Identifiable, Sendable {
    let agent: String
    let outputTokens: Int
    let inputTokens: Int?
    let turns: Int?
    let sessionCount: Int?

    var id: String { agent }

    init?(row: [String: MetricScalar]) {
        guard let agent = row["agent"]?.string,
              let outputTokens = row["output_tokens"]?.int
        else { return nil }
        self.agent = agent
        self.outputTokens = outputTokens
        inputTokens = row["input_tokens"]?.int
        turns = row["turns"]?.int
        sessionCount = row["session_count"]?.int
    }

    init(agent: String, outputTokens: Int, inputTokens: Int? = nil, turns: Int? = nil, sessionCount: Int? = nil) {
        self.agent = agent
        self.outputTokens = outputTokens
        self.inputTokens = inputTokens
        self.turns = turns
        self.sessionCount = sessionCount
    }

    /// Spec §2.4: several live sessions under one agent is worth flagging.
    var hasMultipleSessions: Bool { (sessionCount ?? 0) > 1 }
}

/// 一个 agent 的本月摊销(大鸟 2026-07-29)。
///
/// 摊销对象是 agent 不是天：$270/月按各家占本月输出 token 的比例分。
/// 用输出而非输入做基准——输入 99%+ 是缓存复读，按它分等于按「谁的上下文长」
/// 分，那更多反映会话形态而非工作量。
struct AgentMonthlyCost: Equatable, Identifiable, Sendable {
    let agent: String
    let outputTokens: Int
    let share: Double
    let amortizedUSD: Double

    var id: String { agent }

    init?(row: [String: MetricScalar]) {
        guard let agent = row["agent"]?.string,
              let outputTokens = row["output_tokens"]?.int,
              let amortizedUSD = row["amortized_usd"]?.number
        else { return nil }
        self.agent = agent
        self.outputTokens = outputTokens
        self.amortizedUSD = amortizedUSD
        share = row["share"]?.number ?? 0
    }

    init(agent: String, outputTokens: Int, share: Double, amortizedUSD: Double) {
        self.agent = agent
        self.outputTokens = outputTokens
        self.share = share
        self.amortizedUSD = amortizedUSD
    }
}

struct SessionUsage: Equatable, Identifiable, Sendable {
    let agent: String
    let sessionPrefix: String
    let outputTokens: Int
    let turns: Int?
    let lastActivity: Date?

    var id: String { "\(agent)/\(sessionPrefix)" }

    init?(row: [String: MetricScalar]) {
        guard let agent = row["agent"]?.string,
              let sessionPrefix = row["session_prefix"]?.string,
              let outputTokens = row["output_tokens"]?.int
        else { return nil }
        self.agent = agent
        self.sessionPrefix = sessionPrefix
        self.outputTokens = outputTokens
        turns = row["turns"]?.int
        lastActivity = row["last_activity"].flatMap { $0.string }.flatMap(SessionUsage.parseDate)
    }

    init(agent: String, sessionPrefix: String, outputTokens: Int, turns: Int? = nil, lastActivity: Date? = nil) {
        self.agent = agent
        self.sessionPrefix = sessionPrefix
        self.outputTokens = outputTokens
        self.turns = turns
        self.lastActivity = lastActivity
    }

    static func parseDate(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) { return date }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: value)
    }
}

/// One non-terminal work item from `harness.wi.in_flight`.
///
/// The App uses it to show the WI identity and creation age on a「要你出手」
/// request. The「在办」card reads `work.board` instead; neither card turns the
/// recorded `main_only` label into a WI category.
struct InFlightWorkItem: Equatable, Identifiable, Sendable {
    let workItemID: String
    let title: String
    let status: String
    let days: Int
    let owner: String
    /// Days since the recorded creation, not last edit and not parsed from WI id.
    let openDays: Int?
    let createdAt: Date?
    /// Current effective workflow profile proven by SCL. Missing/ambiguous proof stays nil.
    let workflowProfile: String?
    var summaryLine: String {
        let value = owner.trimmingCharacters(in: .whitespacesAndNewlines)
        let person = value == "?" || value.isEmpty ? "负责人未提供"
            : value.prefix(1).uppercased() + value.dropFirst()
        return person + " · " + workItemID
    }
    var statusAgeText: String {
        let label = status.isEmpty || status == "?" ? "状态未提供" : status
        return label + " · " + (openDays.map { "\($0) 天" } ?? "时长未知")
    }
    /// User-requested age tint only; never an incident or runtime-health rule.
    var ageLamp: StatusLamp {
        guard let days = openDays else { return .gray }
        return days <= 7 ? .green : days <= 30 ? .amber : .red
    }
    static func creationComesFirst(_ lhs: Self, _ rhs: Self) -> Bool {
        switch (lhs.openDays, rhs.openDays) {
        case let (left?, right?) where left != right: return left > right
        case (_?, nil): return true
        case (nil, _?): return false
        default: break
        }
        switch (lhs.createdAt, rhs.createdAt) {
        case let (left?, right?) where left != right: return left < right
        case (_?, nil): return true
        case (nil, _?): return false
        default: return lhs.workItemID < rhs.workItemID
        }
    }

    var id: String { workItemID }

    init?(row: [String: MetricScalar]) {
        guard let workItemID = row["work_item_id"]?.string,
              let days = row["days"]?.int
        else { return nil }
        self.workItemID = workItemID
        self.days = days
        openDays = row["open_days"]?.int.flatMap { $0 >= 0 ? $0 : nil }
        createdAt = openDays == nil ? nil : row["created_at"]?.string.flatMap(SnapshotDecoder.parseISO8601)
        let recordedProfile = row["workflow_profile"]?.string
        workflowProfile = ["lightweight", "standard"].contains(recordedProfile ?? "")
            ? recordedProfile : nil
        title = row["title"]?.string ?? workItemID
        status = row["status"]?.string ?? "?"
        owner = row["owner"]?.string ?? "?"
    }
}

struct StalledWorkItem: Equatable, Identifiable, Sendable {
    let workItemID: String
    let title: String
    let days: Int
    let status: String?

    var id: String { workItemID }

    init?(row: [String: MetricScalar]) {
        // Spec §3.3: the id field is `work_item_id`, not `id`.
        guard let workItemID = row["work_item_id"]?.string,
              let days = row["days"]?.int
        else { return nil }
        self.workItemID = workItemID
        self.days = days
        title = row["title"]?.string ?? workItemID
        status = row["status"]?.string
    }

    init(workItemID: String, title: String, days: Int, status: String? = nil) {
        self.workItemID = workItemID
        self.title = title
        self.days = days
        self.status = status
    }
}

extension SnapshotMetrics {
    /// Agent leaderboard, output tokens descending. `nil` when unavailable —
    /// an empty array means "measured, nobody burned anything", which is a
    /// different statement and renders differently.
    var agentUsage: [AgentUsage]? {
        guard let rows = rows(MetricID.byAgent) else { return nil }
        return rows.compactMap(AgentUsage.init(row:)).sorted { $0.outputTokens > $1.outputTokens }
    }

    /// 本月累计各 agent 的摊销，钱多的在前——这一列的用处就是看谁花得多。
    var mtdAgentCosts: [AgentMonthlyCost]? {
        guard let rows = rows(MetricID.mtdByAgent) else { return nil }
        return rows.compactMap(AgentMonthlyCost.init(row:)).sorted { $0.amortizedUSD > $1.amortizedUSD }
    }

    var sessionUsage: [SessionUsage]? {
        guard let rows = rows(MetricID.bySession) else { return nil }
        return rows.compactMap(SessionUsage.init(row:)).sorted { $0.outputTokens > $1.outputTokens }
    }

    /// Sessions for one agent. v2-D will hang the drill-down off this; v2-C only
    /// uses it to caption the leaderboard rows.
    func sessions(for agent: String) -> [SessionUsage] {
        (sessionUsage ?? []).filter { $0.agent == agent }
    }

    var stalledWorkItems: [StalledWorkItem]? {
        guard let rows = rows(MetricID.wiStalled) else { return nil }
        return rows.compactMap(StalledWorkItem.init(row:)).sorted { $0.days > $1.days }
    }

    var inFlightWorkItems: [InFlightWorkItem]? {
        guard let rows = rows(MetricID.wiInFlight) else { return nil }
        return rows.compactMap(InFlightWorkItem.init(row:)).sorted(by: InFlightWorkItem.creationComesFirst)
    }

    var ccResetsAt: Date? {
        guard let seconds = double(MetricID.ccResetsAt), seconds > 0 else { return nil }
        return Date(timeIntervalSince1970: seconds)
    }

    var codexResetsAt: Date? {
        guard let seconds = double(MetricID.codexResetsAt), seconds > 0 else { return nil }
        return Date(timeIntervalSince1970: seconds)
    }

    var grokResetsAt: Date? {
        guard let seconds = double(MetricID.grokResetsAt), seconds > 0 else { return nil }
        return Date(timeIntervalSince1970: seconds)
    }

    var grokPeriodStart: Date? {
        guard let seconds = double(MetricID.grokPeriodStart), seconds > 0 else { return nil }
        return Date(timeIntervalSince1970: seconds)
    }

    /// 24 hourly buckets. Rejects a wrong-length array rather than padding it —
    /// padding would invent zeros for hours we never measured.
    var hourlyOutputTokens: [Double]? {
        guard let values = numbers(MetricID.hourlyOutputTokens), values.count == 24 else { return nil }
        return values
    }
}
