import CryptoKit
import Foundation
import zlib

/// Only a typed presentation of ComputeCost's query. Never recomputes prices,
/// subscription allocations, WI attribution, or totals from rows.
struct CostDashboard: Codable, Equatable, Sendable {
    static let supportedSchema = "pulse_stats_cost_dashboard/v1"
    let schemaVersion: String
    let generatedAt: Date
    let expiresAt: Date
    let sourceRevision: String?
    let status: String
    let reason: String?
    let views: [CostQueryView]
    let subscriptions: [CostSubscription]
    let openrouter: CostProviderActivity?
    let sha256: String?
    /// Name, one-line purpose and progress of each WI a row names.
    var workItems: [CostWorkItem]? = nil
    /// GitHub merges outside the core and direct pushes to main, if audited.
    var pullRequestAnomalies: PullRequestAnomalies? = nil
    /// Pushed observations the publisher had not all judged yet: the amounts are incomplete.
    var collectionBacklog: CollectionBacklog? = nil

    /// A compatible publication with a sane clock. Its content stays on screen,
    /// marked with its own time, until a newer publication replaces it.
    func isDisplayable(at now: Date) -> Bool {
        schemaVersion == Self.supportedSchema && generatedAt <= now
            && expiresAt.timeIntervalSince(generatedAt) > 0
            && expiresAt.timeIntervalSince(generatedAt) <= Self.maxValidity
    }

    /// The longest validity a publisher sets: 75 minutes on Oracle, 45 on ownermac.
    static let maxValidity: TimeInterval = 75 * 60

    func isFresh(at now: Date) -> Bool { isDisplayable(at: now) && now < expiresAt }

    /// Displayable amounts exist; an `unavailable` publication carries none.
    func hasContent(at now: Date) -> Bool { isDisplayable(at: now) && status == "available" }

    /// The Beijing day the views describe. Each view's window is that day's.
    var dataDay: Date { Self.beijing.startOfDay(for: generatedAt) }

    static var beijing: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }

    /// The "today" view only while it still describes the current Beijing day.
    func todayActivityView(at now: Date) -> CostQueryView? {
        guard Self.beijing.isDate(generatedAt, inSameDayAs: now) else { return nil }
        return activityView(period: .today, at: now)
    }

    func view(period: CostPeriod, dimension: CostDimension, at now: Date) -> CostQueryView? {
        guard hasContent(at: now) else { return nil }
        let matches = views.filter { $0.period == period.rawValue && $0.dimension == dimension.rawValue }
        guard matches.count == 1, let view = matches.first,
              view.sourceSchema == "computecost_collaboration_query/v1",
              view.query.scope == "activity", view.query.timezone == "Asia/Shanghai",
              view.query.groupBy == [dimension.rawValue], view.query.watermark == view.watermark,
              view.summary.cost.isValid else { return nil }
        return view
    }

    /// The cost grid has a joint key and a strict actual-usage denominator.
    /// Older marginal-only caches can supply the headline, never synthetic rows.
    func activityView(period: CostPeriod, at now: Date) -> CostQueryView? {
        guard hasContent(at: now) else { return nil }
        let legacyDimensions = ["agent", "wi", "entrypoint", "node", "runtime", "model"]
        let dimensions = matchesTenantDimensions(period: period) ? ["tenant"] + legacyDimensions : legacyDimensions
        let matches = views.filter { $0.period == period.rawValue && $0.dimension == "activity" }
        guard matches.count == 1, let view = matches.first,
              view.sourceSchema == "computecost_collaboration_query/v1",
              view.query.scope == "activity", view.query.timezone == "Asia/Shanghai",
              view.query.groupBy == dimensions, view.query.watermark == view.watermark,
              view.summary.cost.isValid, view.summary.usage.eventCount >= 0,
              Set(view.groups.map(\.id)).count == view.groups.count else { return nil }
        var count = 0
        for group in view.groups {
            guard Set(group.key.keys) == Set(dimensions), group.cost.isValid,
                  let usage = group.usage, usage.eventCount > 0 else { return nil }
            let (next, overflow) = count.addingReportingOverflow(usage.eventCount)
            guard !overflow else { return nil }; count = next
        }
        guard count == view.summary.usage.eventCount else { return nil }
        let calendar = Self.beijing
        guard let end = calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: generatedAt)) else { return nil }
        let today = calendar.startOfDay(for: generatedAt)
        let start: Date
        switch period {
        case .today: start = today
        case .week:
            let offset = (calendar.component(.weekday, from: today) + 5) % 7
            start = calendar.date(byAdding: .day, value: -offset, to: today)!
        case .month: start = calendar.date(from: calendar.dateComponents([.year, .month], from: today))!
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        guard view.query.fromDate == formatter.string(from: start), view.query.toDate == formatter.string(from: end) else { return nil }
        return view.naming(workItems ?? [])
    }

    private func matchesTenantDimensions(period: CostPeriod) -> Bool {
        views.first { $0.period == period.rawValue && $0.dimension == "activity" }?.query.groupBy.first == "tenant"
    }

    /// Why nothing is shown. Expiry and a new Beijing day are not reasons:
    /// those publications stay visible under `notice`.
    func unavailableReason(at now: Date) -> String {
        guard schemaVersion == Self.supportedSchema else { return "成本数据格式不兼容" }
        guard isDisplayable(at: now) else { return "成本快照时间异常" }
        return CostText.reason(reason ?? "query_source_unavailable")
    }

    /// The line above retained figures once they are no longer current, or nil.
    func notice(at now: Date, refresh: CostRefreshStatus?) -> CostDataNotice? {
        let calendar = Self.beijing
        let sameDay = calendar.isDate(generatedAt, inSameDayAs: now)
        guard hasContent(at: now), !isFresh(at: now) || !sameDay else { return nil }
        var parts: [String] = []
        if !isFresh(at: now) {
            parts.append("已过期 \(max(1, Int(now.timeIntervalSince(expiresAt) / 60))) 分钟")
            if let state = refresh?.stateText(after: generatedAt, at: now) { parts.append(state) }
        }
        let day = sameDay ? nil
            : "以下为 \(calendar.component(.month, from: generatedAt)) 月 \(calendar.component(.day, from: generatedAt)) 日数据"
        return CostDataNotice(headline: "数据截至 " + StatsFormat.clockBeijing(generatedAt)
                                + (parts.isEmpty ? "" : "（" + parts.joined(separator: "｜") + "）"), day: day)
    }
}

/// Retained figures stay readable; this says how old they are and why.
struct CostDataNotice: Equatable, Sendable {
    let headline: String
    let day: String?
    var text: String { [day, headline].compactMap { $0 }.joined(separator: " · ") }
}

/// Peers whose spooled observation bundles the publication left for later collections.
struct CollectionBacklog: Codable, Equatable, Sendable {
    let status: String
    let reason: String
    let peers: [Peer]

    struct Peer: Codable, Equatable, Sendable {
        let id: String
        let deferredBundles: Int
    }

    /// Beside the total: these figures do not hold every observation yet.
    var tileText: String {
        "未收齐：" + peers.map { "\($0.id) 待读 \($0.deferredBundles) 包" }.joined(separator: "、")
    }

    /// Above the 产出与成本 table: the whole table is an incomplete result.
    static let tableHeadline = "数据未收齐，金额与归属不完整"
    var tableDetail: String {
        peers.map { "\($0.id) 还有 \($0.deferredBundles) 包观察数据待读" }.joined(separator: "、")
            + "。表中金额偏低，未识别、待核验与异常行可能只是数据未到；收齐前不要把这些数字当作完整结果。"
    }
}

/// Entries booked in a view's window against already closed weeks.
/// Only display: the exporter has checked that `total` is own usage plus these.
struct CostAdjustments: Codable, Equatable, Sendable {
    let entryCount: Int
    let unpricedEntryCount: Int
    let knownTotalTokens: Int
    let pricedMachineCashUsd: String

    var amount: Decimal? { CostText.signedDecimal(pricedMachineCashUsd) }
    /// "含调整 +$0.30" beside a total; nil when nothing was booked.
    var noteText: String? {
        guard entryCount > 0, let amount else { return nil }
        return "含调整 " + (amount < 0 ? "−" : "+") + StatsFormat.usdDecimal(amount.magnitude)
    }
}

/// Own usage plus adjustments: the figure a reader shows when present.
struct CostAdjustedTotal: Codable, Equatable, Sendable {
    let knownTotalTokens: Int?
    let pricedMachineCashUsd: String
    var amount: Decimal? { CostText.signedDecimal(pricedMachineCashUsd) }
}

/// When the previous week was closed, at which ledger watermark, and which peers it lacked.
struct CostClosedWeek: Codable, Equatable, Sendable {
    struct Gap: Codable, Equatable, Sendable {
        let peer: String
        let reason: String
    }
    let weekStart: String
    let closedAt: String
    let watermark: Int
    let gaps: [Gap]
}

/// The publisher's latest cycle (`cost_refresh`), never a cost fact itself.
struct CostRefreshStatus: Codable, Equatable, Sendable {
    let state: String
    let startedAt: Date?
    let finishedAt: Date?
    let reason: String?
    var peers: [Peer]? = nil

    struct Peer: Codable, Equatable, Sendable {
        let id: String
        let status: String
        let reason: String?
        var deferredBundles: Int? = nil
    }

    /// Why a peer's source was not (completely) read in the latest completed cycle.
    func peerReason(node: String?) -> String? {
        guard state == "completed", let node,
              let peer = peers?.first(where: { $0.id == node && ["unreachable_or_invalid", "partial"].contains($0.status) })
        else { return nil }
        if let deferred = peer.deferredBundles {
            return CostText.refreshReason(peer.reason) + "，待读 \(deferred) 包"
        }
        return CostText.refreshReason(peer.reason)
    }

    /// Peers paused by decision: not collected and outside current coverage, never a failure.
    /// Every cycle state carries them.
    var pausedPeers: [Peer] { peers?.filter { $0.status == "paused" } ?? [] }

    func pausedReason(node: String?) -> String? {
        guard let node, let peer = pausedPeers.first(where: { $0.id == node }) else { return nil }
        return CostText.refreshReason(peer.reason)
    }

    /// Only states about a cycle after the shown data; others say nothing new.
    func stateText(after generatedAt: Date, at now: Date) -> String? {
        switch state {
        case "running":
            guard let startedAt, startedAt <= now else { return nil }
            return "刷新中"
        case "failed":
            guard let finishedAt, finishedAt > generatedAt, finishedAt <= now else { return nil }
            return "上次刷新失败：" + CostText.refreshReason(reason)
        default:
            return nil
        }
    }
}

enum CostPeriod: String, CaseIterable, Identifiable {
    case today, week, month
    var id: String { rawValue }
    var title: String {
        switch self { case .today: return "今日"; case .week: return "本周"; case .month: return "本月" }
    }
}

enum CostDimension: String, CaseIterable, Identifiable {
    case agent, node, runtime, model, entrypoint, wi
    var id: String { rawValue }
    var title: String {
        switch self {
        case .agent: return "Agent"
        case .node: return "节点"
        case .runtime: return "Runtime"
        case .model: return "模型"
        case .entrypoint: return "交互入口"
        case .wi: return "WI"
        }
    }
}

enum CostTab: String, CaseIterable, Identifiable {
    case collaboration, procurement
    var id: String { rawValue }
    var title: String { self == .collaboration ? "协作成本" : "采购费用" }
}

struct CostQueryView: Codable, Equatable, Sendable {
    let period: String
    let dimension: String
    let sourceSchema: String
    let query: CostQueryScope
    let watermark: Int
    let recordedAt: Date?
    let catalogSha256: String?
    let catalogObservedAt: Date?
    let reportSha256: String
    let summary: CostQuerySummary
    var groups: [CostQueryGroup]
    var outcomes: WIOutcomeResult? = nil
    var outcomeOnlyGroups: [CostQueryGroup]? = nil
    var pullRequestOutputs: WIPullRequestOutputs? = nil
    var pullRequestOnlyGroups: [CostQueryGroup]? = nil
    /// Each parent WI's own amount and PRs plus its children's; display only.
    var workItemFamilies: [CostWorkItemFamily]? = nil
    var activeAgents: [String]? = nil
    var populationScope: String? = nil
    var stages: CostStageProjection? = nil
    /// Entries against closed weeks booked in this window; absent from older publications.
    var adjustments: CostAdjustments? = nil
    var total: CostAdjustedTotal? = nil
    var adjustmentOnlyGroups: [CostQueryGroup]? = nil
    var closedWeek: CostClosedWeek? = nil
    var validStages: CostStageProjection? {
        guard let stages, stages.isValid(in: query, recordedAt: recordedAt) else { return nil }
        return stages
    }
    var populationScopeText: String {
        populationScope == "all_registered_tenants" ? "全平台 · 已登记活动" : populationScope == "filtered_activity" ? "筛选后的已登记活动" : "已登记活动 · 租户未标注"
    }

    var validOutcomes: WIOutcomeResult? {
        guard let outcomes, outcomes.isValid else { return nil }
        let formatter = DateFormatter(); formatter.locale = Locale(identifier:"en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: query.timezone); formatter.dateFormat = "yyyy-MM-dd"
        guard outcomes.items.allSatisfy({ item in
            guard let at = item.occurredAt else { return false }
            let day = formatter.string(from: at)
            return query.fromDate <= day && day < query.toDate
        }) else { return nil }
        let refs = Dictionary(uniqueKeysWithValues: outcomes.items.map { ($0.outcomeRef, $0) })
        for row in groups + (outcomeOnlyGroups ?? []) {
            guard let o = row.outcomes, o.isValid,
                  o.refs.allSatisfy({ refs[$0]?.wi == row.key["wi"] }),
                  o.knownCount == o.refs.filter({ refs[$0]?.counted == true }).count else { return nil }
        }
        return outcomes
    }
    /// Kernel-merged PRs, only when they conserve and fall inside this period.
    var validPullRequests: WIPullRequestOutputs? {
        guard let pullRequestOutputs, pullRequestOutputs.isValid else { return nil }
        let formatter = DateFormatter(); formatter.locale = Locale(identifier:"en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: query.timezone); formatter.dateFormat = "yyyy-MM-dd"
        let inPeriod = pullRequestOutputs.workItems.allSatisfy { item in
            item.pullRequests.allSatisfy { pr in
                guard let at = pr.mergedAt else { return false }
                let day = formatter.string(from: at)
                return query.fromDate <= day && day < query.toDate
            }
        }
        return inPeriod ? pullRequestOutputs : nil
    }
    /// Dashboard “PR 数”: kernel-merged PRs this period; partial shows a lower bound,
    /// unavailable shows “—” (never 0).
    var mergedPullRequestText: String {
        guard let prs = validPullRequests else { return "—" }
        switch prs.status {
        case "complete": return prs.count.map(String.init) ?? "—"
        case "partial": return "≥\(prs.knownCount)"
        default: return "—"
        }
    }
    /// The same rows, each carrying the published name of the WI it names
    /// and that WI's merged PRs in this period.
    func naming(_ items: [CostWorkItem]) -> CostQueryView {
        let byID = Dictionary(items.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        let pullRequests = validPullRequests
        let families = Dictionary((workItemFamilies ?? []).filter(\.isValid).map { ($0.wi, $0) },
                                  uniquingKeysWith: { first, _ in first })
        func named(_ row: CostQueryGroup) -> CostQueryGroup {
            var copy = row; copy.workItem = row.key["wi"].flatMap { byID[$0] }
            copy.parentWorkItem = copy.workItem?.parent.flatMap { byID[$0] }
            copy.pullRequests = row.key["wi"].flatMap { pullRequests?.summary(for: $0) }
            copy.family = row.key["wi"].flatMap { families[$0] }
            return copy
        }
        var copy = self
        copy.groups = groups.map(named)
        copy.outcomeOnlyGroups = outcomeOnlyGroups?.map(named)
        copy.pullRequestOnlyGroups = pullRequestOnlyGroups?.map(named)
        return copy
    }
    var presentationRows: [CostQueryGroup] {
        var rows: [CostQueryGroup]
        if validOutcomes == nil {
            rows = groups.map { row in var copy = row; copy.outcomes = nil; return copy }
        } else {
            rows = groups + (outcomeOnlyGroups ?? []).filter {
                $0.usage?.eventCount == 0 && ($0.outcomes?.knownCount ?? 0) > 0 && $0.cost.amount == nil
            }
        }
        guard validPullRequests != nil else { return rows }
        let shown = Set(rows.compactMap { $0.key["wi"] })
        return rows + (pullRequestOnlyGroups ?? []).filter {
            $0.usage?.eventCount == 0 && ($0.outputCount ?? 0) > 0 && $0.cost.amount == nil
                && !shown.contains($0.key["wi"] ?? "")
        }
    }
    /// The publisher's distinct agent names. An agent active under several
    /// tenants is one agent; `multiTenantAgents` names them for the detail.
    var activeAgentCount: Int? {
        let unknown = ["unassigned", "ambiguous", "not_applicable"]
        let costAgents = presentationRows.compactMap { row -> String? in
            guard let agent = row.key["agent"], !unknown.contains(agent) else { return nil }
            return agent
        }
        let outcomeAgents = (validOutcomes?.items ?? []).compactMap { item -> String? in
            guard item.counted, let agent = item.responsible, !unknown.contains(agent) else { return nil }
            return agent
        }
        guard let activeAgents, Set(activeAgents).count == activeAgents.count,
              Set(activeAgents) == Set(costAgents + outcomeAgents) else { return nil }
        return activeAgents.count
    }
    var multiTenantAgents: [String] {
        guard query.groupBy.contains("tenant") else { return [] }
        let named = Dictionary(grouping: groups, by: { $0.key["agent"] ?? "unassigned" })
        return named.filter { agent, rows in
            !["unassigned", "ambiguous", "not_applicable"].contains(agent)
                && Set(rows.compactMap { $0.key["tenant"] }).count > 1
        }.keys.sorted()
    }
    /// Dashboard total: the complete amount when the query proves one,
    /// otherwise every priced row of the same query. Older publications
    /// without the priced total fall back to the partial amount.
    var totalCostExact: String? {
        let cost = summary.cost
        guard ownCostIsCoherent else { return nil }
        if cost.machineCashUsd != nil { return cost.machineCashUsd }
        guard cost.pricedEventCount != nil || cost.pricedMachineCashUsd != nil else { return cost.partialMachineCashUsd }
        return cost.pricedMachineCashUsd
    }
    /// False when the own cost contradicts itself: then no amount is shown.
    /// True with no amount means own usage is merely unpriced.
    private var ownCostIsCoherent: Bool {
        let cost = summary.cost
        guard cost.isValid else { return false }
        if cost.machineCashUsd != nil || (cost.pricedEventCount == nil && cost.pricedMachineCashUsd == nil) { return true }
        return cost.pricedTotalIsValid(eventCount: summary.usage.eventCount)
    }
    var totalCostAmount: Decimal? { CostText.decimal(totalCostExact) }
    /// The published total (own usage plus adjustments) when it can be shown.
    private var publishedAdjustedTotal: Decimal? {
        guard let adjustments, adjustments.entryCount > 0, adjustments.amount != nil, ownCostIsCoherent else { return nil }
        return total?.amount
    }
    /// The headline amount: own usage plus adjustments when the publisher closes weeks.
    /// Unpriced own usage still shows the priced entries; an unreadable total leaves the own amount.
    var adjustedTotalAmount: Decimal? { publishedAdjustedTotal ?? totalCostAmount }
    /// Beside the headline amount: the entries in it and what it does not cover.
    var adjustmentNote: String? {
        guard total != nil, let note = adjustments?.noteText else { return nil }
        guard publishedAdjustedTotal != nil else { return "调整未计入：合计无效" }
        var gaps: [String] = []
        if totalCostAmount == nil { gaps.append("自身用量未计价") }
        if let unpriced = adjustments?.unpricedEntryCount, unpriced > 0 { gaps.append("\(unpriced) 条调整未计价") }
        return gaps.isEmpty ? note : note + "（" + gaps.joined(separator: "，") + "）"
    }
    /// Rows needing a correction, and rows whose only reasons are proof gaps.
    var reasonClassCounts: (anomaly: Int, proofGapOnly: Int) {
        let rows = presentationRows
        return (rows.filter { $0.cost.hasAnomaly }.count,
                rows.filter { !$0.cost.hasAnomaly && !$0.cost.proofGapReasons.isEmpty }.count)
    }
}

/// The same query's declared business objects, not WI closures or file counts.
/// Display supplied category/unit quantities; never build a grand total here.
struct CostStageProjection: Codable, Equatable, Sendable {
    let status: String
    let coverage: String
    let executionAuthority: Bool
    let unsupportedFilters: [String]
    let reason: String?
    let outputs: CostStageOutputs?
    let diagnostics: [String: [String]]?

    var statusText: String {
        switch status {
        case "recorded_subset": return "已记录部分 · Agent 判断，尚非核验或人类验收"
        case "conflict": return "记录存在冲突 · 数量及归属待核验"
        case "unsupported_filter": return "该筛选范围尚不支持产出统计"
        default:
            if reason == "stage_projection_failed" { return "产出视图暂不可用，用量仍按已验证事实显示" }
            return reason == "stage_payload_too_large" ? "产出明细超出展示上限，须按同一查询水位读取" : "尚无可用产出记录，不等于零"
        }
    }

    func isValid(in query: CostQueryScope, recordedAt: Date?) -> Bool {
        guard coverage == "recorded_declarations_only", !executionAuthority,
              ["recorded_subset", "conflict", "unavailable", "unsupported_filter"].contains(status) else { return false }
        if reason == "stage_payload_too_large" || reason == "stage_projection_failed" {
            return status == "unavailable" && outputs == nil && diagnostics == nil
        }
        guard reason == nil, let outputs, let diagnostics,
              Set(diagnostics.keys) == Set(["membership_conflicts", "revision_conflicts", "shared_refs", "unknown_refs", "unresolved_link_targets", "time_unallocated_refs"]),
              diagnostics.values.allSatisfy({ CostStageOutput.validRefs($0) }),
              unsupportedFilters.isEmpty || status == "unsupported_filter",
              Set(outputs.items.map(\.factRef)).count == outputs.items.count,
              Set(outputs.items.map(\.entityRef)).count == outputs.items.count,
              outputs.cumulative.objectCount == Set(outputs.items.map(\.objectRef)).count else { return false }
        if !(diagnostics["membership_conflicts"] ?? []).isEmpty || !(diagnostics["revision_conflicts"] ?? []).isEmpty {
            guard ["conflict", "unsupported_filter"].contains(status) else { return false }
        }
        if ["unavailable", "unsupported_filter"].contains(status) && !outputs.items.isEmpty { return false }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: query.timezone); formatter.dateFormat = "yyyy-MM-dd"
        guard let start = formatter.date(from: query.fromDate), let end = formatter.date(from: query.toDate) else { return false }
        for item in outputs.items {
            guard item.isValid, let recordedAt, item.observedAt <= recordedAt else { return false }
            if let at = item.occurredAt { guard at < end else { return false } }
            else if !(diagnostics["time_unallocated_refs"] ?? []).contains(item.factRef) { return false }
        }
        let formed = outputs.items.filter { $0.occurredAt.map { start <= $0 && $0 < end } ?? false }
        return CostStageGroup.validPartition(outputs.formed, items: formed)
            && CostStageGroup.validPartition(outputs.cumulative.groups, items: outputs.items)
            && outputs.accepted.isValid(outputs: outputs.items, recordedAt: recordedAt)
    }
}

struct CostStageOutputs: Codable, Equatable, Sendable {
    let items: [CostStageOutput]
    let formed: [CostStageGroup]
    let cumulative: CostStageCumulative
    let accepted: CostStageAccepted
}

/// An observed final decision for the exact displayed version, never a grant.
struct CostStageAccepted: Codable, Equatable, Sendable {
    let status: String
    let reason: String?
    let objectCount: Int?
    let groups: [CostStageGroup]?
    let items: [CostStageAcceptance]?

    func isValid(outputs: [CostStageOutput], recordedAt: Date?) -> Bool {
        if status == "unavailable" {
            return ["native_exact_version_acceptance_not_joined", "native_assessment_conflict"].contains(reason)
                && objectCount == nil && groups == nil && items == nil
        }
        guard status == "recorded_subset", reason == nil, let groups, let items,
              !items.isEmpty, let recordedAt,
              Set(items.map(\.outputFactRef)).count == items.count else { return false }
        var selected: [CostStageOutput] = []
        for item in items {
            guard let output = outputs.first(where: { $0.factRef == item.outputFactRef }),
                  output.objectRef == item.objectRef, output.versionRef == item.versionRef,
                  item.scope == "final", item.acceptedAt <= recordedAt,
                  (item.verificationRef == nil) != (item.waiverRef == nil),
                  CostStageOutput.validRefs([item.candidateRef, item.humanReviewRef]),
                  CostStageOutput.validRefs([item.verificationRef, item.waiverRef].compactMap { $0 }) else { return false }
            selected.append(output)
        }
        return objectCount == Set(selected.map(\.objectRef)).count
            && CostStageGroup.validPartition(groups, items: selected)
    }
}

struct CostStageAcceptance: Codable, Equatable, Sendable {
    let outputFactRef: String
    let objectRef: String
    let versionRef: String
    let candidateRef: String
    let verificationRef: String?
    let waiverRef: String?
    let humanReviewRef: String
    let acceptedAt: Date
    let scope: String
}

struct CostStageCumulative: Codable, Equatable, Sendable {
    let objectCount: Int
    let groups: [CostStageGroup]
}

struct CostStageGroup: Codable, Equatable, Sendable, Identifiable {
    let category: String
    let unit: String
    let comparisonBasis: String
    let contribution: String
    let objectCount: Int
    let objectRefs: [String]
    let quantity: String?
    let knownQuantity: String?
    let unresolvedObjectRefs: [String]
    let epistemic: String
    let factRefs: [String]
    var id: [String] { [category, unit, comparisonBasis, contribution] }
    var contributionText: String { CostStageOutput.contributionText(contribution) }
    var quantityText: String {
        if let quantity { return quantity + " " + unit }
        if let knownQuantity { return "已知 " + knownQuantity + " " + unit + " · 总量未知" }
        return "数量未知 · " + unit
    }

    static func validPartition(_ groups: [Self], items: [CostStageOutput]) -> Bool {
        let expected = Dictionary(grouping: items, by: \.groupKey)
        guard Set(groups.map(\.id)).count == groups.count, Set(groups.map(\.id)) == Set(expected.keys) else { return false }
        for group in groups {
            guard let rows = expected[group.id], group.epistemic == "calculated_from_agent_judgments",
                  CostStageOutput.validRefs(group.objectRefs), CostStageOutput.validRefs(group.factRefs),
                  CostStageOutput.validRefs(group.unresolvedObjectRefs),
                  group.objectCount == group.objectRefs.count,
                  Set(group.objectRefs) == Set(rows.map(\.objectRef)), Set(group.factRefs) == Set(rows.map(\.factRef)),
                  Set(group.unresolvedObjectRefs).isSubset(of: Set(group.objectRefs)),
                  CostStageOutput.validQuantity(group.quantity), CostStageOutput.validQuantity(group.knownQuantity),
                  Set(rows.filter { $0.quantity == nil }.map(\.objectRef)).isSubset(of: Set(group.unresolvedObjectRefs)) else { return false }
            if group.unresolvedObjectRefs.isEmpty {
                guard group.quantity != nil && group.knownQuantity != nil else { return false }
            } else if group.quantity != nil { return false }
        }
        return true
    }
}

struct CostStageOutput: Codable, Equatable, Sendable, Identifiable {
    let wi: String?
    let contextRef: String
    let activityRefs: [String]
    let epistemic: String
    let objectRef: String
    let versionRef: String
    let name: String
    let category: String
    let unit: String
    let comparisonBasis: String
    let contribution: String
    let extent: String
    let quantity: String?
    let entityRef: String
    let factRef: String
    let occurredAt: Date?
    let observedAt: Date
    let producer: String
    let evidence: [String]
    var id: String { factRef }
    var groupKey: [String] { [category, unit, comparisonBasis, contribution] }
    var contributionText: String { Self.contributionText(contribution) }
    var quantityText: String { quantity.map { $0 + " " + unit } ?? "数量未知 · " + unit }
    var wiText: String { wi == "ambiguous" ? "归属冲突" : wi ?? "尚未关联 WI" }
    static func contributionText(_ value: String) -> String {
        ["new": "新增", "improved": "改进", "reused": "复用（不计新增）"][value] ?? "未知贡献"
    }
    static func validRefs(_ refs: [String]) -> Bool {
        Set(refs).count == refs.count && refs.allSatisfy { $0.range(of: "^ccf1_[a-f0-9]{64}$", options: .regularExpression) != nil }
    }
    static func validQuantity(_ value: String?) -> Bool {
        guard let value else { return true }
        guard value.range(of: #"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"#, options: .regularExpression) != nil,
              let number = Decimal(string: value, locale: Locale(identifier: "en_US_POSIX")), !number.isNaN else { return false }
        return number >= 0
    }
    var isValid: Bool {
        epistemic == "agent_judgment" && ["new", "improved", "reused"].contains(contribution)
            && validReferenceFields && !activityRefs.isEmpty && !evidence.isEmpty
            && Self.validRefs(activityRefs) && Self.validRefs(evidence) && Self.validQuantity(quantity)
            && (wi == nil || wi == "ambiguous" || wi!.range(of: "^wi_[A-Za-z0-9_]+$", options: .regularExpression) != nil)
            && [name, category, unit, comparisonBasis, extent, producer].allSatisfy { !$0.isEmpty && !$0.contains("\n") }
    }
    private var validReferenceFields: Bool {
        [contextRef, objectRef, versionRef, entityRef, factRef].allSatisfy { Self.validRefs([$0]) }
    }
}

struct CostQueryScope: Codable, Equatable, Sendable {
    let fromDate: String
    let toDate: String
    let timezone: String
    let scope: String
    let groupBy: [String]
    let watermark: Int
}

struct CostQuerySummary: Codable, Equatable, Sendable {
    let cost: QueryMachineCost
    let usage: CostQueryUsage
}

struct CostQueryUsage: Codable, Equatable, Sendable {
    let knownTotalTokens: Int?
    let eventCount: Int
    let sourceCoverage: String
    let wiInventoryCoverage: String
    var lastUsageAt: Date? = nil
    var unassignedEventCount: Int? = nil
    var unassignedKnownTotalTokens: Int? = nil
    var agentUnassignedEventCount: Int? = nil
    var agentUnassignedPricedUsd: String? = nil
    var wiUnassignedEventCount: Int? = nil
    var wiUnassignedPricedUsd: String? = nil
    var sourceDetails: [CostSourceDetail]? = nil
    var sourceDetailsTitle: String {
        guard let sourceDetails, !sourceDetails.isEmpty else { return "数据源 · 明细未提供" }
        return "数据源 · \(sourceDetails.count) 项"
    }
    var conflictedEventCount: Int? = nil
    var timeUnallocatedEventCount: Int? = nil
    // Optional for old snapshots only. Never synthesize a missing token class.
    var tokens: [String: CostTokenQuantity]? = nil
}

/// Preserve both the known quantity and its independent missing-event count.
/// Encoding nil explicitly prevents an unknown class from disappearing on cache save.
struct CostTokenQuantity: Codable, Equatable, Sendable {
    let known: Int?
    let unknownEventCount: Int

    enum CodingKeys: String, CodingKey { case known, unknownEventCount }

    init(known: Int?, unknownEventCount: Int) {
        self.known = known
        self.unknownEventCount = unknownEventCount
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        known = try values.decodeIfPresent(Int.self, forKey: .known)
        unknownEventCount = try values.decode(Int.self, forKey: .unknownEventCount)
        guard (known == nil || known! >= 0), unknownEventCount >= 0 else {
            throw DecodingError.dataCorruptedError(forKey: .known, in: values, debugDescription: "Negative token quantity")
        }
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(known, forKey: .known)
        try values.encode(unknownEventCount, forKey: .unknownEventCount)
    }
}

struct CostGroupUsage: Codable, Equatable, Sendable {
    let tokens: [String: CostTokenQuantity]
    let eventCount: Int
    let knownTotalTokens: Int?
}

struct CostSourceDetail: Codable, Equatable, Sendable {
    let label: String
    let kind: String
    let status: String
    let reason: String?
    let filesSeen: Int
    let filesRead: Int
    let filesInvalid: Int
    let excludedDirectories: Int
    let latestUsageAt: Date?
    let errors: [String: Int]
    let sourceRef: String
    var displayName: String {
        if kind == "receipts", label.hasPrefix("stats-transport-") {
            return ["stats-transport-tcloud":"TCloud 用量同步"][label] ?? "远端用量同步"
        }
        return label
    }
    /// Labels name their node as `<node>.<source>` or `stats-transport-<node>`.
    var node: String? {
        if label.hasPrefix("stats-transport-") { return String(label.dropFirst("stats-transport-".count)) }
        guard let dot = label.firstIndex(of: ".") else { return nil }
        return String(label[..<dot])
    }
    /// A retired node keeps its historical receipts but is no longer read.
    var isRetired: Bool { node.map(CostNode.retired.contains) ?? false }
    /// Read without failure; native logs only cannot prove the execution inventory.
    var hasInventoryProofGap: Bool {
        status == "unknown" && reason == "execution_inventory_not_observed" && !hasReadFailure
    }
    /// A current source whose reading failed or is unconfirmed.
    var needsAttention: Bool { !isRetired && (hasReadFailure || (status == "unknown" && !hasInventoryProofGap)) }
    var failureImpact: String {
        if !hasReadFailure { return "该来源本期读取情况尚未确认，当前成本可能不完整。" }
        return kind == "receipts" ? "本轮用量同步未完成，最新用量可能尚未计入；已入账历史保留。这不是节点宕机的判据。"
            : "部分用量记录未能读取或解析，当前成本可能不完整；不代表任务本身执行失败。"
    }
    var hasPendingUsage: Bool {
        kind == "relay_host" && status == "partial" && reason == "host_usage_pending"
            && filesInvalid == 0 && errors.isEmpty
    }
    var hasReadFailure: Bool {
        filesInvalid > 0 || !errors.isEmpty || status == "unreachable" || (status == "partial" && !hasPendingUsage)
    }
    var readStatusText: String {
        if isRetired { return "节点已退役 · 历史记录保留" }
        if hasReadFailure { return "读取异常" }
        if hasPendingUsage { return "用量待回报" }
        if hasInventoryProofGap { return "已读取 · 执行清单未证明完整" }
        return ["complete", "available", "ok"].contains(status) ? "已读取" : "读取情况待核验"
    }
}

/// What a person reads about a WI. Never an attribution input.
struct CostWorkItem: Codable, Equatable, Sendable {
    let id: String
    let title: String
    let summary: String?
    let status: String?
    /// The kernel-rendered project page (absolute local path), when the publisher found one.
    var projectPage: String? = nil
    /// A child WI's parent; nil for every other WI.
    var parent: String? = nil
    /// Only this WI's own `.html` page under an absolute path opens; anything else stays text.
    var projectPageURL: URL? {
        guard let path = projectPage, path.hasPrefix("/"), !path.contains("/../"),
              (path as NSString).lastPathComponent == id + ".html" else { return nil }
        return URL(fileURLWithPath: path)
    }
    var statusText: String {
        ["draft": "草稿", "active": "进行中", "stopped": "已暂停", "transferring": "交接中",
         "completed": "已完成", "failed": "未通过", "cancelled": "已取消", "terminated": "已终止",
         "superseded": "已被取代", "migrated": "已迁移"][status ?? ""] ?? (status ?? "状态未知")
    }
}

/// How a row's usage got its WI. Keys: declared, holder_supplement,
/// scheduled_job, classified (reviewed non-WI), undeclared (a human request
/// that declared no WI), unattributed.
struct CostAttribution: Codable, Equatable, Sendable {
    let sources: [String: CostAttributionSource]
    var job: CostScheduledJob? = nil
    static let labels: [(key: String, text: String)] = [
        ("declared", "声明"), ("holder_supplement", "持有补充"), ("exploration", "探索"), ("scheduled_job", "自动任务"),
        ("classified", "已审非 WI"), ("undeclared", "未声明 WI"), ("unattributed", "未关联")]
    var summary: String {
        Self.labels.compactMap { item in sources[item.key].map { "\(item.text) \($0.eventCount) 条" } }
            .joined(separator: " · ")
    }
}

struct CostAttributionSource: Codable, Equatable, Sendable {
    let eventCount: Int
    let pricedMachineCashUsd: String
}

struct CostScheduledJob: Codable, Equatable, Sendable {
    let id: String
    let name: String?
}

struct CostQueryGroup: Codable, Equatable, Sendable, Identifiable {
    let key: [String: String]
    let cost: QueryMachineCost
    var usage: CostGroupUsage? = nil
    var outcomes: WIOutcomeReferences? = nil
    var detail: CostActivityDetail? = nil
    var tenant: CostTenantIdentity? = nil
    var attribution: CostAttribution? = nil
    var adjustments: CostAdjustments? = nil
    var total: CostAdjustedTotal? = nil
    /// Display only; attached from the document, never part of a row's key.
    var workItem: CostWorkItem? = nil
    /// This row's WI's kernel-merged PRs in the view's period; attached, never decoded.
    var pullRequests: CostRowPullRequests? = nil
    /// A child WI's parent, as published; attached, never decoded.
    var parentWorkItem: CostWorkItem? = nil
    /// A parent WI's total with its children; attached, never decoded.
    var family: CostWorkItemFamily? = nil
    /// A scheduler job without a WI: its key is `cron:<job ID>`.
    var scheduledJobID: String? {
        guard let wi = key["wi"], wi.hasPrefix("cron:") else { return nil }
        return String(wi.dropFirst("cron:".count))
    }
    /// Confirmed non-WI work: reviewed cron/none or a scheduler job.
    /// Usage outside any WI: scheduled jobs, declared non-WI work, and human requests that declared no WI.
    var isNonWI: Bool { ["cron", "none", "undeclared"].contains(key["wi"] ?? "") || scheduledJobID != nil }
    /// "自动任务：<任务名>" for a scheduler job row.
    var scheduledJobText: String? {
        guard let id = scheduledJobID else { return nil }
        let name = attribution?.job?.id == id ? attribution?.job?.name : nil
        return "自动任务：" + (name ?? id)
    }
    /// Part of this row's WI came from the session's own WI writes, not a declaration.
    var hasHolderSupplement: Bool { attribution?.sources["holder_supplement"] != nil }
    var holderSupplementCount: Int { attribution?.sources["holder_supplement"]?.eventCount ?? 0 }
    var tenantText: String {
        guard let id = key["tenant"], !["unassigned", "ambiguous", "not_applicable"].contains(id) else {
            return key["tenant"] == "ambiguous" ? "租户待核验" : "租户未知"
        }
        guard let tenant, tenant.isValid(for: id) else { return "租户待核验" }
        return (tenant.name ?? id) + ((tenant.registryConflicts ?? []).isEmpty ? "" : "（与登记冲突）")
    }
    var tenantSourceText: String? {
        guard let tenant, let sources = tenant.sources else { return nil }
        let text = sources.map { $0 == "registry" ? "Agent 登记" : "会话绑定" }.joined(separator: "、")
        let conflicts = tenant.registryConflicts ?? []
        return conflicts.isEmpty ? text : text + "；登记为 " + conflicts.joined(separator: "、")
    }
    var subcallLabel: String? {
        guard let calls = detail?.subcalls else { return nil }
        guard calls.isValid(eventCount: usage?.eventCount ?? 0, runtime: key["runtime"]) else { return "子调用待核验" }
        return calls.eventCount == usage?.eventCount ? "子调用" : "含子调用"
    }
    var hasActivity: Bool {
        (usage?.eventCount ?? 0) > 0 || (outcomes?.knownCount ?? 0) > 0 || (outputCount ?? 0) > 0
    }
    /// The output column: PRs the core merged for this WI; nil is unknown, never zero.
    var outputCount: Int? { pullRequests?.count }
    var outputText: String {
        if isNonWI { return "—" }
        if ["unassigned", "ambiguous"].contains(key["wi"] ?? "unassigned") { return "未关联" }
        return outputCount.map { "\($0) 个 PR" } ?? "未提供"
    }
    /// Accepted WI Outcomes, shown beside the PR output.
    var outcomeCount: Int? { outcomes?.isValid == true ? outcomes?.count : nil }
    var outcomeText: String {
        if isNonWI { return "—" }
        if ["unassigned", "ambiguous"].contains(key["wi"] ?? "unassigned") { return "未关联" }
        return outcomeCount.map { "\($0) 项" } ?? "未提供"
    }
    /// Relay channels, scheduled runs, and the native session entrypoints the meter extracts.
    static func entrypointTitle(_ name: String) -> String {
        if name.hasPrefix("codex:"), name.count > "codex:".count { return "Codex（\(name.dropFirst("codex:".count))）" }
        return ["codex_app": "Codex App", "feishu": "飞书", "telegram": "Telegram", "cli": "命令行", "automation": "无",
                "sdk": "SDK", "codex_exec": "Codex 命令行", "pi_cli": "Pi 命令行", "relay": "Relay"][name] ?? name
    }
    var id: String { key.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: "|") }
    var name: String { key.values.first ?? "未知分组" }
    var title: String {
        if name == "ambiguous" {
            return key.keys.first == "wi" ? "WI 归属待核验" : "归属冲突"
        }
        if name == "undeclared" && key.keys.first == "wi" { return "未声明 WI" }
        // The meter publisher names a missing model or entrypoint "unknown"; older ones "unassigned".
        if name == "unassigned" || (name == "unknown" && key.keys.first != "wi") {
            switch key.keys.first {
            case "agent": return "Agent 未识别"
            case "wi": return "WI 未关联"
            case "node": return "节点未知"
            case "entrypoint": return "交互入口未知"
            case "runtime": return "Runtime 未知"
            case "model": return "模型未知"
            default: return "未知"
            }
        }
        switch key.keys.first {
        case "agent": return ["pulse": "Pulse", "aki": "Aki", "hestia": "Hestia", "duke": "Duke"][name] ?? name
        // Historical receipts keep the node that measured them.
        case "node": return CostNode.title(name)
        case "runtime": return ["codex": "Codex", "pi": "Pi", "cc": "Claude Code", "cc_sdk": "Claude Code SDK"][name] ?? name
        case "entrypoint": return Self.entrypointTitle(name)
        default: return name
        }
    }
}

enum CostNode {
    /// Nodes that no longer run. Their measured history stays queryable.
    static let retired: Set<String> = ["vultr"]
    static func title(_ name: String) -> String {
        let title = ["tcloud": "TCloud", "vultr": "Vultr"][name] ?? name
        return retired.contains(name) ? title + "（已退役）" : title
    }
}

struct CostTenantIdentity: Codable, Equatable, Sendable {
    let id: String
    let name: String?
    let bindingRefs: [String]
    /// "session": Relay input or declared membership; "registry": the Agent's registered tenant.
    var sources: [String]? = nil
    /// Registry tenants that differ from the session tenant shown in `id`.
    var registryConflicts: [String]? = nil
    func isValid(for key: String) -> Bool {
        guard id == key, id.range(of: #"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"#, options: .regularExpression) != nil,
              let name, !name.isEmpty, name.count <= 64, name == name.trimmingCharacters(in: .whitespacesAndNewlines),
              !name.contains(where: { $0.isNewline || "/\\:@".contains($0) }),
              !bindingRefs.isEmpty, Set(bindingRefs).count == bindingRefs.count else { return false }
        return bindingRefs.allSatisfy { $0.range(of: #"^ccf1_[a-f0-9]{64}$"#, options: .regularExpression) != nil }
    }
}

struct CostUnpriced: Codable, Equatable, Sendable {
    let factRef: String
    let reason: String
}

struct QueryMachineCost: Codable, Equatable, Sendable {
    let machineCashUsd: String?
    let partialMachineCashUsd: String?
    let cashBasis: String
    let reasons: [String]
    let unpriced: [CostUnpriced]
    var unpricedCount: Int? = nil
    var unpricedReasonCounts: [String: Int]? = nil
    var unpricedSha256: String? = nil
    var pricedMachineCashUsd: String? = nil
    var pricedEventCount: Int? = nil
    /// Presentation classes of `reasons`. Older publications carry neither,
    /// and then every reason stays an anomaly.
    var anomalies: [String]? = nil
    var proofGaps: [String]? = nil
    var unpricedEventCount: Int { unpricedCount ?? unpriced.count }
    var anomalyReasons: [String] { anomalies ?? reasons }
    var proofGapReasons: [String] { anomalies == nil ? [] : proofGaps ?? [] }
    var hasAnomaly: Bool { !anomalyReasons.isEmpty }

    /// The priced total covers exactly the priced rows of the same query.
    func pricedTotalIsValid(eventCount: Int) -> Bool {
        guard isValid, let count = pricedEventCount, count >= 0,
              (pricedMachineCashUsd == nil) == (count == 0),
              pricedMachineCashUsd == nil || CostText.decimal(pricedMachineCashUsd) != nil else { return false }
        let (rows, overflow) = count.addingReportingOverflow(unpricedEventCount)
        guard !overflow, rows == eventCount else { return false }
        if let known = machineCashUsd ?? partialMachineCashUsd { return pricedMachineCashUsd == known }
        return true
    }

    private var diagnosticSummaryIsValid: Bool {
        if unpricedCount == nil && unpricedReasonCounts == nil && unpricedSha256 == nil { return true }
        guard let count = unpricedCount, count > unpriced.count,
              let counts = unpricedReasonCounts, !counts.isEmpty,
              let fingerprint = unpricedSha256,
              fingerprint.range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil else { return false }
        var total = 0
        for (reason, number) in counts {
            guard number > 0,
                  reason.range(of: #"^[a-z][a-z0-9_]{0,100}$"#, options: .regularExpression) != nil else { return false }
            let (sum, overflow) = total.addingReportingOverflow(number)
            guard !overflow else { return false }
            total = sum
        }
        let samples = Dictionary(grouping: unpriced, by: \.reason)
        return total == count && Set(samples.keys) == Set(counts.keys)
            && samples.allSatisfy { key, values in values.count <= (counts[key] ?? 0) }
    }


    private var reasonClassesAreValid: Bool {
        guard let anomalies, let proofGaps else { return anomalies == nil && proofGaps == nil }
        return anomalies == Array(Set(anomalies)).sorted() && proofGaps == Array(Set(proofGaps)).sorted()
            && Set(anomalies).isDisjoint(with: proofGaps) && Set(anomalies + proofGaps) == Set(reasons)
    }

    var isValid: Bool {
        guard diagnosticSummaryIsValid, reasonClassesAreValid, cashBasis == "openrouter_standard_token_cash",
              machineCashUsd == nil || CostText.decimal(machineCashUsd) != nil,
              partialMachineCashUsd == nil || CostText.decimal(partialMachineCashUsd) != nil else { return false }
        if machineCashUsd != nil && (partialMachineCashUsd != nil || !reasons.isEmpty) { return false }
        if partialMachineCashUsd != nil && (reasons.isEmpty || reasons.contains("join_conflict")) { return false }
        return true
    }
    var amount: Decimal? { isValid ? CostText.decimal(machineCashUsd ?? partialMachineCashUsd) : nil }
    var exactAmount: String? { isValid ? machineCashUsd ?? partialMachineCashUsd : nil }
    var isPartial: Bool { isValid && partialMachineCashUsd != nil }
    /// Anomalies only: problems that need a correction.
    var note: String {
        guard isValid else { return "成本数据冲突，未采用金额" }
        return Self.text(anomalyReasons)
    }
    /// Completeness the sources cannot prove; not a data error.
    var proofGapNote: String { isValid ? Self.text(proofGapReasons) : "" }
    private static func text(_ codes: [String]) -> String {
        Array(Set(codes.map(CostText.reason))).sorted().joined(separator: "；")
    }
}

struct CostSubscription: Codable, Equatable, Sendable, Identifiable {
    let id: String
    let name: String
    let status: String
    let monthlyAmountUsd: String?
    let billingDay: Int?
    let confirmedOn: String
    var active: Bool { status == "active_user_confirmed" }
}

struct CostProviderActivity: Codable, Equatable, Sendable {
    let status: String
    let reason: String
    let observedAt: Date?
    let monthUtc: String
    let coverage: String
    let usageUsd: String?
    let requests: Int?
    let firstReturnedDay: String?
    let lastReturnedDay: String?

    func amount(at now: Date) -> Decimal? {
        guard status == "available", coverage == "returned_rows_only",
              let observedAt, observedAt <= now, now.timeIntervalSince(observedAt) < 86400,
              let requests, requests >= 0, firstReturnedDay != nil, lastReturnedDay != nil else { return nil }
        let month = DateFormatter()
        month.locale = Locale(identifier: "en_US_POSIX")
        month.timeZone = TimeZone(secondsFromGMT: 0)
        month.dateFormat = "yyyy-MM"
        guard month.string(from: now) == monthUtc else { return nil }
        return CostText.decimal(usageUsd)
    }
}

enum CostText {
    /// A published signed amount (adjustments may be negative).
    static func signedDecimal(_ value: String?) -> Decimal? {
        guard let value, value.hasPrefix("-") else { return decimal(value) }
        return decimal(String(value.dropFirst())).map { -$0 }
    }
    static func decimal(_ value: String?) -> Decimal? {
        guard let value, value.count <= 38,
              value.range(of: #"^[0-9]+(?:\.[0-9]+)?$"#, options: .regularExpression) != nil,
              let amount = Decimal(string: value, locale: Locale(identifier: "en_US_POSIX")),
              !amount.isNaN, amount >= 0 else { return nil }
        return amount
    }
    static func refreshReason(_ code: String?) -> String {
        switch code {
        case "production_ledger_unavailable": return "生产账本暂不可读写（GitHub）"
        case "production_ledger_changed": return "生产账本正被另一端写入，下轮重读"
        case "peer_unavailable": return "TCloud 来源暂不可用"
        case "cycle_interrupted": return "刷新进程中断"
        case "timeout": return "刷新超时"
        case "peer_busy": return "远端上一轮采集仍在进行"
        case "peer_ssh_failed": return "SSH 连接失败"
        case "peer_command_failed": return "远端采集失败"
        case "peer_production_ledger_unavailable": return "远端生产账本暂不可读写（GitHub）"
        case "peer_timeout": return "远端采集超时"
        case "peer_receipt_invalid": return "远端回执无效"
        case "paused_by_decision": return "按决定暂停采集"
        case "observation_backlog": return "观察包积压，分轮读取中"
        case "resolved_view_mismatch": return "增量视图与现状表对账不符，本轮未发布"
        default: return "发布失败"
        }
    }

    static func reason(_ code: String) -> String {
        switch code {
        case "host_usage_pending": return "执行尚未结束，完整用量待回报"
        case "source_not_configured": return "协作成本来源尚未接入"
        case "query_source_unavailable": return "协作成本来源暂不可读"
        case "invalid_cost_snapshot": return "成本快照校验失败"
        case "snapshot_stale": return "成本数据已过期，等待刷新"
        case "snapshot_from_future": return "成本快照时间异常"
        case "query_payload_too_large": return "成本明细超过传输范围"
        case "source_coverage_unknown", "wi_execution_inventory_unknown": return "采集范围尚未完整证明"
        case "execution_link_unknown": return "部分用量尚未关联执行记录"
        case "attribution_unknown": return "部分用量尚未关联 WI"
        case "actor_attribution_conflict": return "Agent 或入口证据冲突，相关维度单列"
        case "join_conflict": return "来源存在冲突，不能汇总金额"
        case "execution_evidence_conflict": return "执行记录存在冲突，相关成本待核验"
        case "context_inventory_unknown": return "所选事务范围的用量清单尚未证明完整"
        case "context_linked_usage_unavailable": return "部分关联用量暂不可读"
        case "source_unreadable_or_invalid": return "来源读取失败或内容无效"
        case "execution_inventory_not_observed": return "尚未观测到该来源的执行清单"
        case "authority_binding_unavailable": return "来源与登记身份尚未绑定"
        case "native_attribution_boundary_invalid": return "原生归属边界无效，相关用量未归属"
        case "native_mcp_result_unavailable": return "WI 声明结果暂不可读"
        case "cc_request_declaration_boundary_unproved": return "CC 请求的 WI 声明边界尚未证明"
        case "host_stream_invalid": return "Host 记录流含无效记录"
        case "host_capture_gap": return "Host 采集存在缺口"
        case "time_unallocated": return "部分用量无法归入确切日期"
        case "incomplete_execution": return "部分执行未收到预期用量"
        case "execution_in_progress": return "会话仍在进行，总额尚未定稿"
        case "provisional_usage": return "部分用量尚未定稿，按已知下限计"
        case "expected_measurement_missing", "expected_measurement_unjoined": return "部分执行凭据尚未接齐"
        case "incomplete_price_or_quantity": return "部分用量或价格缺失"
        case "no_usage_observations": return "未收到可用的用量凭据"
        case "price_timestamp_unknown": return "价格目录时间未知"
        case "provider_not_connected": return "OpenRouter 账务尚未接入"
        case "provider_read_failed": return "OpenRouter 账务读取失败"
        case "invalid_provider_snapshot": return "OpenRouter 账务校验失败"
        case "provider_snapshot_stale": return "OpenRouter 记录已过期"
        case "no_returned_rows_in_month": return "本月未返回账务记录，不能当作零"
        default: return code
        }
    }
}


/// Optional extension: old cost snapshots still decode without Outcome data.
struct WIOutcomeResult: Codable, Equatable, Sendable {
    let status: String
    let count: Int?
    let knownCount: Int
    let metricScope: String
    let items: [WIOutcomeItem]
    let unallocatedRefs: [String]
    let conflictRefs: [String]
    let coverage: String
    var isValid: Bool {
        metricScope == "wi_outcomes_in_period" && knownCount >= 0
            && ["complete","observed","partial","unavailable","conflict"].contains(status)
            && Set(items.map(\.outcomeRef)).count == items.count
            && items.filter(\.counted).count == knownCount
            && items.allSatisfy { $0.counted == ($0.status != "unknown") }
            && (count == nil || count == knownCount)
            && (!["unavailable","conflict"].contains(status) || count == nil)
            && (count != 0 || status == "complete")
    }
    var note: String {
        switch status {
        case "complete": return "所选期间已核验"
        case "observed", "partial": return "已记录成果，完整范围尚未确认"
        case "conflict": return "成果证据冲突，未采用总数"
        default: return "成果未提供，不等于0项"
        }
    }
}

struct WIOutcomeItem: Codable, Equatable, Sendable, Identifiable {
    let outcomeRef: String
    let factRef: String
    let wi: String
    let responsible: String?
    let status: String
    let counted: Bool
    let occurredAt: Date?
    let candidateRef: String
    let artifactRef: String
    let acceptanceRef: String
    let reviewDecisions: [String]
    let reviewRefs: [String]
    let evidenceRefs: [String]
    let title: String?
    let summary: String?
    /// Who accepted it; newer queries only. `self_with_controls` is an Agent review.
    var verificationLevel: String? = nil
    var id: String { outcomeRef }
    var statusText: String {
        switch status {
        case "accepted" where verificationLevel == "self_with_controls": return "主会话审核通过（非人类验收）"
        case "accepted": return "已接受"
        case "submitted": return "已提交"
        case "partial": return "部分接受"
        case "rejected": return "未通过"
        case "blocked": return "受阻"
        default: return "仅有审核，Outcome 未提供"
        }
    }
}

struct WIOutcomeReferences: Codable, Equatable, Sendable {
    let status: String
    let count: Int?
    let knownCount: Int
    let refs: [String]
    let metricScope: String
    var isValid: Bool {
        metricScope == "wi_reference_not_runtime_allocation" && knownCount >= 0
            && knownCount <= refs.count && Set(refs).count == refs.count
            && ["complete","observed","partial","unavailable","conflict"].contains(status)
            && (count == nil || count == knownCount)
            && (!["unavailable","conflict"].contains(status) || count == nil)
            && (count != 0 || status == "complete")
    }
}

/// A parent WI with children in the view: its own amount and PRs plus theirs.
/// The parent's own row keeps its own values; the view total never adds this.
struct CostWorkItemFamily: Codable, Equatable, Sendable {
    let wi: String
    let children: [String]
    let costUsd: String?
    let costComplete: Bool
    let pullRequestCount: Int?
    var isValid: Bool {
        !children.isEmpty && Set(children).count == children.count && !children.contains(wi)
            && (pullRequestCount ?? 0) >= 0 && (!costComplete || costUsd != nil)
    }
    var text: String {
        let amount = CostText.decimal(costUsd).map { StatsFormat.usdDecimal($0) + (costComplete ? "" : "（已计价部分）") }
            ?? "金额未提供"
        let prs = pullRequestCount.map { "\($0) 个 PR" } ?? "PR 未提供"
        return "含子 WI 合计（本 WI 与 \(children.count) 个子 WI）：\(amount) · \(prs)"
    }
}

/// Optional extension: PRs the native core reviewed, merged and read back, by WI.
struct WIPullRequestOutputs: Codable, Equatable, Sendable {
    let status: String
    let reason: String?
    let metricScope: String
    let count: Int?
    let knownCount: Int
    let workItems: [WIPullRequestWorkItem]
    let unreadableWorkItems: [String]
    let timeUnknownCount: Int
    var isValid: Bool {
        guard metricScope == "wi_kernel_merged_pull_requests_in_period",
              ["complete", "partial", "unavailable"].contains(status),
              (status == "complete") == (count != nil), count == nil || count == knownCount,
              status != "unavailable" || workItems.isEmpty,
              Set(workItems.map(\.wi)).count == workItems.count,
              workItems.allSatisfy({ $0.count > 0 && $0.count == $0.pullRequests.count
                  && !unreadableWorkItems.contains($0.wi) }) else { return false }
        return Set(workItems.flatMap { $0.pullRequests.map(\.id) }).count == knownCount
    }
    /// One WI's PRs: a WI not listed merged none, unless its records were unreadable.
    func summary(for wi: String) -> CostRowPullRequests {
        guard status != "unavailable", !unreadableWorkItems.contains(wi) else {
            return CostRowPullRequests(count: nil, item: nil)
        }
        let item = workItems.first { $0.wi == wi }
        return CostRowPullRequests(count: item?.count ?? 0, item: item)
    }
    var note: String {
        switch status {
        case "complete": return "内核合并记录已全部读取"
        case "partial": return "部分 WI 的合并记录未读到，合计未采用"
        default: return "合并 PR 记录未提供，不等于 0 个"
        }
    }
}

struct WIPullRequestWorkItem: Codable, Equatable, Sendable {
    let wi: String
    let count: Int
    /// The WI's amount in this view: complete, or its priced part.
    let costUsd: String?
    let costComplete: Bool
    let costPerPullRequestUsd: String?
    let pullRequests: [WIPullRequest]
    var costText: String {
        guard let cost = CostText.decimal(costUsd), let per = CostText.decimal(costPerPullRequestUsd) else {
            return "本期无用量记录，成本未提供"
        }
        let qualifier = costComplete ? "" : "（已计价部分，完整成本未知）"
        return "WI 本期 \(StatsFormat.usdDecimal(cost)) · 每 PR \(StatsFormat.usdDecimal(per))" + qualifier
    }
}

struct WIPullRequest: Codable, Equatable, Sendable, Identifiable {
    let repository: String
    let number: Int
    let mergeCommit: String?
    let mergedAt: Date?
    let score: Int?
    let reviewedRounds: Int
    let decision: String
    var id: String { "\(repository)#\(number)" }
    var title: String { (repository.split(separator: "/").last.map(String.init) ?? repository) + " #\(number)" }
    var reviewText: String {
        let review = score.map { "审核 \($0) 分" } ?? "无审核分"
        let gate = decision == "human" ? " · 人类确认合并" : ""
        return review + " · \(reviewedRounds) 轮" + gate
    }
}

/// A row's view of its WI's PRs; `count` nil means unknown, not zero.
struct CostRowPullRequests: Codable, Equatable, Sendable {
    let count: Int?
    let item: WIPullRequestWorkItem?
}

/// GitHub merges the core has no record of, and direct pushes to main.
/// Decoding never fails the cost document; malformed parts read as unverified.
struct PullRequestAnomalies: Codable, Equatable, Sendable {
    struct Merge: Codable, Equatable, Sendable, Identifiable {
        let repository: String
        let number: Int
        let mergedAt: Date?
        let title: String?
        var id: String { "\(repository)#\(number)" }
        func line(_ label: String) -> String {
            let name = repository.split(separator: "/").last.map(String.init) ?? repository
            return "\(label) · \(name) #\(number)" + (title.map { " · " + $0 } ?? "")
        }
    }
    struct Commit: Codable, Equatable, Sendable, Identifiable {
        let repository: String
        let sha: String
        let committedAt: Date?
        let title: String?
        let author: String?
        var id: String { repository + "@" + sha }
        var line: String {
            let name = repository.split(separator: "/").last.map(String.init) ?? repository
            let parts = [title, author].compactMap { $0 }
            return "直接提交 main · \(name) " + String(sha.prefix(10)) + parts.map { " · " + $0 }.joined()
        }
    }
    struct List<Item: Codable & Equatable & Sendable>: Codable, Equatable, Sendable {
        let count: Int?
        let items: [Item]
    }
    let status: String
    let reason: String?
    let observedAt: Date?
    let windowDays: Int?
    /// The Beijing day from which a GitHub merge without a kernel record is an anomaly.
    let enforcedSince: String?
    let unrecordedMerges: List<Merge>?
    /// GitHub merges before enforcement (or with none configured): reference only.
    let unenforcedMerges: List<Merge>?
    let directCommits: List<Commit>?
    /// Per period, merged PRs without a kernel merge record; hints only, never output.
    let unregisteredPullRequests: [String: Unregistered]?

    /// Merged PRs of one period that no WI's kernel merge record names. The Agent is
    /// a branch-prefix hint (`<agent>/`), unverified; none of it is output or cost.
    struct Unregistered: Codable, Equatable, Sendable {
        struct AgentCount: Codable, Equatable, Sendable { let agent: String; let count: Int }
        struct Item: Codable, Equatable, Sendable, Identifiable {
            let repository: String
            let number: Int
            let mergedAt: Date?
            let title: String?
            let headRef: String?
            let agentHint: String?
            var id: String { "\(repository)#\(number)" }
            var line: String {
                let name = repository.split(separator: "/").last.map(String.init) ?? repository
                let parts = [headRef, agentHint.map { "提示 " + $0 }, title].compactMap { $0 }
                return "未登记 · \(name) #\(number)" + parts.map { " · " + $0 }.joined()
            }
        }
        let fromDate: String
        let toDate: String
        let status: String
        let reason: String?
        let count: Int?
        let agents: [AgentCount]
        let unhintedCount: Int?
        let items: [Item]
        /// The count, only when verified and its hints re-add.
        var knownCount: Int? {
            guard status == "verified", let count, let unhintedCount,
                  agents.reduce(0, { $0 + $1.count }) + unhintedCount == count, items.count <= count else { return nil }
            return count
        }
        /// Beside the PR tile: "另 N 个未登记" when there are any.
        var tileText: String? { knownCount.flatMap { $0 > 0 ? "另 \($0) 个未登记" : nil } }
        func headline(_ period: String) -> String? {
            guard let count = knownCount else {
                return "\(period)未经内核登记的已合并 PR 未核验：" + PullRequestAnomalies.reasonText(reason)
            }
            guard count > 0 else { return nil }
            let hints = agents.map { "\($0.agent) \($0.count)" } + ((unhintedCount ?? 0) > 0 ? ["无提示 \(unhintedCount ?? 0)"] : [])
            return "\(period)另有 \(count) 个已合并 PR 未经内核登记 · 分支前缀提示（未核实）：" + hints.joined(separator: " · ")
        }
    }
    /// The entry for exactly this query period (matched by its dates, not its name).
    func unregistered(from: String, to: String) -> Unregistered? {
        unregisteredPullRequests?.values.first { $0.fromDate == from && $0.toDate == to }
    }

    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        status = (try? c?.decode(String.self, forKey: .status)) ?? "unverified"
        reason = (try? c?.decodeIfPresent(String.self, forKey: .reason)) ?? (c == nil ? "audit_invalid" : nil)
        observedAt = try? c?.decodeIfPresent(Date.self, forKey: .observedAt)
        windowDays = try? c?.decodeIfPresent(Int.self, forKey: .windowDays)
        enforcedSince = try? c?.decodeIfPresent(String.self, forKey: .enforcedSince)
        unrecordedMerges = try? c?.decodeIfPresent(List<Merge>.self, forKey: .unrecordedMerges)
        unenforcedMerges = try? c?.decodeIfPresent(List<Merge>.self, forKey: .unenforcedMerges)
        directCommits = try? c?.decodeIfPresent(List<Commit>.self, forKey: .directCommits)
        unregisteredPullRequests = try? c?.decodeIfPresent([String: Unregistered].self, forKey: .unregisteredPullRequests)
    }

    /// Reference count, never an anomaly: "GitHub 直接合并 N 个（关卡尚未强制）".
    var referenceLine: String? {
        guard status != "unverified", let days = windowDays else { return nil }
        guard let count = unenforcedMerges?.count else {
            return enforcedSince == nil ? "近 \(days) 天 GitHub 直接合并 未核验（关卡尚未强制）" : nil
        }
        return count > 0 || enforcedSince == nil ? "近 \(days) 天 GitHub 直接合并 \(count) 个（关卡尚未强制）" : nil
    }
    /// Without an enforcement date no GitHub merge is an anomaly.
    var mergeCount: Int? { status == "unverified" || enforcedSince == nil ? nil : unrecordedMerges?.count }
    var commitCount: Int? { status == "unverified" ? nil : directCommits?.count }
    var needsAttention: Bool { (mergeCount ?? 0) > 0 || (commitCount ?? 0) > 0 }
    /// Known anomalies beyond the listed samples.
    var unlistedCount: Int {
        let listed = (unrecordedMerges?.items.count ?? 0) + (directCommits?.items.count ?? 0)
        return max(0, (mergeCount ?? 0) + (commitCount ?? 0) - listed)
    }
    var headline: String {
        guard status != "unverified", let days = windowDays else {
            return "PR 产出异常未核验：" + Self.reasonText(reason)
        }
        let commits = commitCount.map { "直接提交 main \($0) 个" } ?? "直接提交 main 未核验"
        guard enforcedSince != nil else { return "近 \(days) 天 \(commits)" }
        let merges = mergeCount.map { "未经内核合并 \($0) 个" } ?? "未经内核合并 未核验"
        return "近 \(days) 天 \(merges) · \(commits)"
    }
    static func reasonText(_ reason: String?) -> String {
        switch reason {
        case "audit_not_collected": return "尚未读取 GitHub"
        case "audit_stale": return "GitHub 读取已过期"
        case "core_pull_requests_unverified": return "内核合并记录未读到"
        case "github_partially_read": return "部分仓库未读到"
        case "enforcement_date_invalid": return "强制生效日配置无效"
        case "audit_window_short": return "GitHub 读取未覆盖本期起点"
        default: return "读取结果无法核对"
        }
    }
}

struct CostActivityDetail: Codable, Equatable, Sendable {
    let firstUsageAt: Date?
    let lastUsageAt: Date?
    let factRefs: [String]
    let factRefCount: Int
    let attributionRefs: [String]
    let attributionRefCount: Int
    var subcalls: CostRuntimeSubcalls? = nil
}

struct CostRuntimeSubcall: Codable, Equatable, Sendable, Identifiable {
    let parentScopeRef: String
    let childScopeRef: String
    let callRef: String
    let parentRuntime: String
    let childRuntime: String
    let basis: String
    let eventCount: Int
    var label: String? = nil
    var id: String { callRef + ":" + childScopeRef }
    static func runtimeName(_ value: String) -> String {
        ["codex":"Codex", "pi":"Pi", "cc":"Claude Code", "cc_sdk":"Claude Code SDK"][value] ?? value
    }
    var chain: String { Self.runtimeName(parentRuntime) + " → " + Self.runtimeName(childRuntime) }
    var isValid: Bool {
        let runtimes = ["codex", "pi", "cc", "cc_sdk"]
        return eventCount > 0 && parentScopeRef != childScopeRef
            && [parentScopeRef, childScopeRef, callRef].allSatisfy { $0.range(of: #"^ccf1_[0-9a-f]{64}$"#, options: .regularExpression) != nil }
            && runtimes.contains(parentRuntime) && runtimes.contains(childRuntime)
            && ["native_tool_result", "reviewed_launch"].contains(basis)
            && (label == nil || (!(label ?? "").isEmpty && (label?.count ?? 0) <= 160
                && label?.rangeOfCharacter(from: .controlCharacters) == nil))
    }
}

struct CostRuntimeSubcalls: Codable, Equatable, Sendable {
    let eventCount: Int
    let itemCount: Int
    let items: [CostRuntimeSubcall]
    let sha256: String
    func isValid(eventCount total: Int, runtime: String?) -> Bool {
        guard eventCount > 0, eventCount <= total, itemCount > 0, itemCount >= items.count,
              !items.isEmpty, items.count <= 5, Set(items.map(\.id)).count == items.count,
              sha256.range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil,
              items.allSatisfy({ $0.isValid && $0.childRuntime == runtime }) else { return false }
        var measured = 0
        for item in items {
            let (sum, overflow) = measured.addingReportingOverflow(item.eventCount)
            guard !overflow else { return false }
            measured = sum
        }
        return itemCount == items.count ? measured == eventCount : measured < eventCount
    }
}

struct DashboardStatusContext {
    let authority: GlobalAuthorityViewModel
    let relay: RelayViewModel
    let harness: HarnessViewModel
    let health: RuntimeHealth
    let incidents: [Incident]
    let observedAt: Date?

    /// Current measured operation, never proof of an entire reporting period.
    /// Cross-endpoint collaboration verification is a separate card and gate.
    func currentStatus(now: Date, resources: OikosPlatformViewModel? = nil) -> (value: String, note: String, lamp: StatusLamp) {
        guard let observedAt, observedAt <= now, now.timeIntervalSince(observedAt) < 2700,
              health != .stale else { return ("待更新", "运行读数过期", .gray) }
        let open = incidents.filter { $0.state == .open && $0.openedAt <= now }
        if health == .critical || open.contains(where: { $0.severity == .critical }) {
            return ("有异常", "当前运行异常", .red)
        }
        if health == .warning || !open.isEmpty {
            return ("有异常", "当前运行异常", .amber)
        }
        if let resources, resources.available {
            if resources.confirmedCount > 0 { return ("有异常", "当前资源异常", .red) }
            if resources.lamp == .amber { return ("需关注", "资源需关注", .amber) }
            if resources.unknownCount > 0 { return ("无法确认", "资源读数不全", .gray) }
        }
        guard health == .ok else { return ("无法确认", "缺少运行证据", .gray) }
        if let resources, !resources.available { return ("待更新", "资源读数过期", .gray) }
        return ("正常", "已监测项均正常", .green)
    }

    /// WI created and closed in the selected period, from the same snapshot.
    /// A reading from another Asia/Shanghai day or an old snapshot is unknown.
    func workItemCounts(period: CostPeriod, now: Date) -> (created: Int?, closed: Int?) {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        guard let observedAt, observedAt <= now, now.timeIntervalSince(observedAt) < 2700,
              calendar.isDate(observedAt, inSameDayAs: now) else { return (nil, nil) }
        switch period {
        case .today: return (harness.dayCreated, harness.dayClosed)
        case .week: return (harness.isoWeekCreated, harness.isoWeekClosed)
        case .month: return (harness.monthCreated, harness.monthClosed)
        }
    }
}


// A broken optional Outcome/detail carrier must not discard independently valid
// cost, usage or the three lower cards. Semantic checks still gate Outcome use.
extension CostQueryView {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        period = try c.decode(String.self,forKey:.period)
        dimension = try c.decode(String.self,forKey:.dimension)
        sourceSchema = try c.decode(String.self,forKey:.sourceSchema)
        query = try c.decode(CostQueryScope.self,forKey:.query)
        watermark = try c.decode(Int.self,forKey:.watermark)
        recordedAt = try c.decodeIfPresent(Date.self,forKey:.recordedAt)
        catalogSha256 = try c.decodeIfPresent(String.self,forKey:.catalogSha256)
        catalogObservedAt = try c.decodeIfPresent(Date.self,forKey:.catalogObservedAt)
        reportSha256 = try c.decode(String.self,forKey:.reportSha256)
        summary = try c.decode(CostQuerySummary.self,forKey:.summary)
        groups = try c.decode([CostQueryGroup].self,forKey:.groups)
        outcomes = try? c.decodeIfPresent(WIOutcomeResult.self,forKey:.outcomes)
        outcomeOnlyGroups = try? c.decodeIfPresent([CostQueryGroup].self,forKey:.outcomeOnlyGroups)
        pullRequestOutputs = try? c.decodeIfPresent(WIPullRequestOutputs.self,forKey:.pullRequestOutputs)
        pullRequestOnlyGroups = try? c.decodeIfPresent([CostQueryGroup].self,forKey:.pullRequestOnlyGroups)
        workItemFamilies = try? c.decodeIfPresent([CostWorkItemFamily].self,forKey:.workItemFamilies)
        activeAgents = try? c.decodeIfPresent([String].self,forKey:.activeAgents)
        populationScope = try? c.decodeIfPresent(String.self,forKey:.populationScope)
        stages = try? c.decodeIfPresent(CostStageProjection.self,forKey:.stages)
        adjustments = try? c.decodeIfPresent(CostAdjustments.self,forKey:.adjustments)
        total = try? c.decodeIfPresent(CostAdjustedTotal.self,forKey:.total)
        adjustmentOnlyGroups = try? c.decodeIfPresent([CostQueryGroup].self,forKey:.adjustmentOnlyGroups)
        closedWeek = try? c.decodeIfPresent(CostClosedWeek.self,forKey:.closedWeek)
    }
}

extension CostQueryGroup {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode([String:String].self,forKey:.key)
        cost = try c.decode(QueryMachineCost.self,forKey:.cost)
        usage = try c.decodeIfPresent(CostGroupUsage.self,forKey:.usage)
        outcomes = try? c.decodeIfPresent(WIOutcomeReferences.self,forKey:.outcomes)
        detail = try? c.decodeIfPresent(CostActivityDetail.self,forKey:.detail)
        tenant = try? c.decodeIfPresent(CostTenantIdentity.self,forKey:.tenant)
        attribution = try? c.decodeIfPresent(CostAttribution.self,forKey:.attribution)
        adjustments = try? c.decodeIfPresent(CostAdjustments.self,forKey:.adjustments)
        total = try? c.decodeIfPresent(CostAdjustedTotal.self,forKey:.total)
    }
}

/// Recorded usage of one runtime inside an activity view. A token class missing
/// from a row counts that row's events as unknown; nothing is synthesized.
struct CostRuntimeUsage: Equatable, Sendable {
    let eventCount: Int
    let tokens: [String: CostTokenQuantity]
    /// nil when any contributing row lacks its last usage time.
    let lastUsageAt: Date?
}

extension CostQueryView {
    /// The producer marks coverage complete only when the source denominator is proven.
    var isUsageSourceComplete: Bool { summary.usage.sourceCoverage == "complete" }

    /// nil when no row of this view belongs to `runtime`.
    func runtimeUsage(_ runtime: String) -> CostRuntimeUsage? {
        let rows = groups.compactMap { row -> (CostGroupUsage, Date?)? in
            guard row.key["runtime"] == runtime, let usage = row.usage else { return nil }
            return (usage, row.detail?.lastUsageAt)
        }
        guard !rows.isEmpty else { return nil }
        let classes = Set(rows.flatMap { $0.0.tokens.keys })
        var tokens: [String: CostTokenQuantity] = [:]
        for name in classes {
            var known: Int?
            var unknown = 0
            for (usage, _) in rows {
                if let quantity = usage.tokens[name] {
                    if let value = quantity.known { known = (known ?? 0) + value }
                    unknown += quantity.unknownEventCount
                } else {
                    unknown += usage.eventCount
                }
            }
            tokens[name] = CostTokenQuantity(known: known, unknownEventCount: unknown)
        }
        let times = rows.map(\.1)
        let last = times.contains(where: { $0 == nil }) ? nil : times.compactMap { $0 }.max()
        return CostRuntimeUsage(eventCount: rows.reduce(0) { $0 + $1.0.eventCount }, tokens: tokens, lastUsageAt: last)
    }
}

/// The snapshot's cost carrier: the expanded publication, or the gzip form the
/// exporter sends when the expanded one would not fit the 1 MiB snapshot. The
/// exporter has verified the publication digest; the App checks the hash of the
/// gzip bytes, expands at most `maxExpandedBytes` and requires the expanded
/// document to restate the outer metadata.
struct CostDashboardCarrier: Decodable {
    /// ComputeCost's in-memory expansion bound (stats_dashboard_wire.MAX_EXPANDED_BYTES).
    static let maxExpandedBytes = 4 * 1024 * 1024

    enum TransportError: Error, Equatable {
        case encoding, digest, stream, tooLarge, metadata
    }

    let dashboard: CostDashboard

    private enum TransportKeys: String, CodingKey {
        case schemaVersion, generatedAt, expiresAt, sourceRevision, status, sha256
        case compressedDashboard, compressedDashboardSha256
    }

    init(from decoder: Decoder) throws {
        let keys = try decoder.container(keyedBy: TransportKeys.self)
        guard keys.contains(.compressedDashboard) else {
            dashboard = try CostDashboard(from: decoder)
            return
        }
        let encoded = try keys.decode(String.self, forKey: .compressedDashboard)
        let digest = try keys.decode(String.self, forKey: .compressedDashboardSha256)
        guard let compressed = Data(base64Encoded: encoded) else { throw TransportError.encoding }
        guard Self.sha256Hex(compressed) == digest else { throw TransportError.digest }
        let expanded = try Self.gunzip(compressed, limit: Self.maxExpandedBytes)
        let value = try SnapshotDecoder.makeDecoder().decode(CostDashboard.self, from: expanded)
        guard value.schemaVersion == (try keys.decode(String.self, forKey: .schemaVersion)),
              value.generatedAt == (try keys.decode(Date.self, forKey: .generatedAt)),
              value.expiresAt == (try keys.decode(Date.self, forKey: .expiresAt)),
              value.sourceRevision == (try keys.decodeIfPresent(String.self, forKey: .sourceRevision)),
              value.status == (try keys.decode(String.self, forKey: .status)),
              value.sha256 == (try keys.decodeIfPresent(String.self, forKey: .sha256)) else {
            throw TransportError.metadata
        }
        dashboard = value
    }

    static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    /// Exactly one gzip member: zlib checks the header, CRC-32 and length, and
    /// any byte after the member, a truncated stream or excess output is refused.
    static func gunzip(_ data: Data, limit: Int) throws -> Data {
        var stream = z_stream()
        guard inflateInit2_(&stream, 16 + MAX_WBITS, zlibVersion(), Int32(MemoryLayout<z_stream>.size)) == Z_OK else {
            throw TransportError.stream
        }
        defer { inflateEnd(&stream) }
        var output = Data()
        var input = [UInt8](data)
        let chunk = 64 * 1024
        var buffer = [UInt8](repeating: 0, count: chunk)
        let status: Int32 = input.withUnsafeMutableBufferPointer { source in
            stream.next_in = source.baseAddress
            stream.avail_in = uInt(source.count)
            var status = Z_OK
            while status == Z_OK && output.count <= limit {
                status = buffer.withUnsafeMutableBufferPointer { target in
                    stream.next_out = target.baseAddress
                    stream.avail_out = uInt(chunk)
                    let result = inflate(&stream, Z_NO_FLUSH)
                    output.append(target.baseAddress!, count: chunk - Int(stream.avail_out))
                    return result
                }
            }
            return status
        }
        guard output.count <= limit else { throw TransportError.tooLarge }
        guard status == Z_STREAM_END, stream.avail_in == 0 else { throw TransportError.stream }
        return output
    }
}
