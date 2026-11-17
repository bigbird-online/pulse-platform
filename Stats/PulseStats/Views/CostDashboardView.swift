import AppKit
import SwiftUI

#if DEBUG
private struct CostRenderAnchor: NSViewRepresentable {
    let value: String
    func makeNSView(context: Context) -> NSView {
        let view = NSView(frame: .zero)
        view.identifier = NSUserInterfaceItemIdentifier(value)
        return view
    }
    func updateNSView(_ view: NSView, context: Context) { view.identifier = NSUserInterfaceItemIdentifier(value) }
}
#endif

private extension View {
    @ViewBuilder func costAnchor(_ value: String) -> some View {
        #if DEBUG
        background(CostRenderAnchor(value: value))
        #else
        self
        #endif
    }
}

struct CostDashboardView: View {
    let document: CostDashboard?
    let now: Date
    private let refresh: CostRefreshStatus?
    private let tablePreferences: UserDefaults?
    @State private var tab: CostTab
    @State private var browser: CostBrowserState
    private let onOpenDetails: (() -> Void)?
    @State private var evidenceExpanded: Bool
    @State private var quotaExpanded: Bool
    @State private var ccQuotaExpanded: Bool
    @State private var grokQuotaExpanded: Bool
    private let statusContext: DashboardStatusContext?
    private let platform: OikosPlatformViewModel?
    @State private var oikosExpanded: Bool
    /// Cards placed after the cost card; they share the panel's reading order.
    private let trailing: AnyView?

    init(document: CostDashboard?, now: Date, refresh: CostRefreshStatus? = nil, initialTab: CostTab = .collaboration,
         initialPeriod: CostPeriod = .today, initialEvidenceExpanded: Bool = false, initialQuotaExpanded: Bool = false,
         initialCCQuotaExpanded: Bool = false, initialGrokQuotaExpanded: Bool = false,
         tablePreferences: UserDefaults? = .standard, platform: OikosPlatformViewModel? = nil,
         initialOikosExpanded: Bool = false, statusContext: DashboardStatusContext? = nil,
         browser: CostBrowserState? = nil, onOpenDetails: (() -> Void)? = nil, trailing: AnyView? = nil) {
        self.document = document
        self.refresh = refresh
        self.trailing = trailing
        self.now = now
        self.tablePreferences = tablePreferences
        self.statusContext = statusContext
        self.platform = platform
        _oikosExpanded = State(initialValue: initialOikosExpanded)
        _tab = State(initialValue: initialTab)
        _browser = State(initialValue: browser ?? CostBrowserState(period: initialPeriod))
        self.onOpenDetails = onOpenDetails
        _evidenceExpanded = State(initialValue: initialEvidenceExpanded || initialTab == .procurement)
        _quotaExpanded = State(initialValue: initialQuotaExpanded)
        _ccQuotaExpanded = State(initialValue: initialCCQuotaExpanded)
        _grokQuotaExpanded = State(initialValue: initialGrokQuotaExpanded)
    }

    private var period: CostPeriod { browser.period }
    private var activity: CostQueryView? { document?.activityView(period: period, at: now) }
    private var query: CostQueryView? { activity ?? document?.view(period: period, dimension: .agent, at: now) }
    /// Set while the figures shown are a retained, no longer current publication.
    private var notice: CostDataNotice? { document?.notice(at: now, refresh: refresh) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            StatsCard {
                HStack {
                    StatsSectionHeading(lamp: .gray, title: "仪表盘")
                    Picker("统计期间", selection: $browser.period) {
                        ForEach(CostPeriod.allCases) { Text($0.title).tag($0) }
                    }.pickerStyle(.segmented).labelsHidden().frame(width: 142)
                }.costAnchor("dashboard-section-heading")
                summaryMetrics
                statusRows
            }.costAnchor("stats-card-dashboard")
            StatsCard {
                HStack {
                    StatsSectionHeading(lamp: .gray, title: "产出与成本")
                    Button { browser.filtersVisible.toggle() } label: {
                        Label(browser.filtersVisible ? "收起筛选" : "筛选", systemImage: "line.3.horizontal.decrease")
                    }.buttonStyle(.plain).font(.caption).foregroundStyle(.secondary)
                }.costAnchor("outcome-cost-section-heading")
                collaboration
            }.costAnchor("stats-card-cost")
            if let trailing { trailing }
        }
        .disclosureGroupStyle(StatsDisclosureStyle())
        .multilineTextAlignment(.leading)
        .costAnchor("cost-dashboard")

    }

    private var summaryMetrics: some View {
        let health = statusContext?.currentStatus(now: now, resources: platform) ?? (value: "无法确认", note: "缺少运行证据", lamp: StatusLamp.gray)
        let workItems = statusContext?.workItemCounts(period: period, now: now) ?? (created: nil, closed: nil)
        return HStack(spacing: 0) {
            metric("平台运行", value: health.value, note: health.note, anchor: "dashboard-platform-state", lamp: health.lamp) { oikosExpanded.toggle() }
            Divider().frame(height: 33)
            metric("新增 WI", value: workItems.created.map(String.init) ?? "—", note: "\(period.title)新建", anchor: "dashboard-wi-created")
            Divider().frame(height: 33)
            metric("PR 数", value: query?.mergedPullRequestText ?? "—", note: retainedNote ?? "\(period.title)内核合并", anchor: "dashboard-merged-prs",
                   extra: unregisteredPullRequests?.tileText, retained: notice != nil)
            Divider().frame(height: 33)
            metric("总成本", value: query?.totalCostAmount.map { StatsFormat.usdDecimal($0) } ?? "—", note: retainedNote ?? "标准价", anchor: "collaboration-cost-amount",
                   retained: notice != nil) { evidenceExpanded.toggle() }
        }
        .padding(.vertical, 9)
        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.18)))
    }

    /// Retained tiles name their own time instead of the period caption.
    private var retainedNote: String? {
        guard notice != nil, let document else { return nil }
        return "截至 " + StatsFormat.clockBeijing(document.generatedAt)
    }

    @ViewBuilder private func metric(_ label: String, value: String, note: String, anchor: String, lamp: StatusLamp? = nil,
                                     extra: String? = nil, retained: Bool = false, action: (() -> Void)? = nil) -> some View {
        let content = VStack(alignment: .leading, spacing: 5) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(size: 19, weight: .semibold)).monospacedDigit()
                .foregroundStyle(retained ? Color.secondary : lamp?.color ?? Color.primary)
                .lineLimit(1).minimumScaleFactor(0.85).costAnchor(anchor)
            Text(note).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            if let extra {
                Text(extra).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                    .minimumScaleFactor(0.8).costAnchor(anchor + "-unregistered")
            }
        }.frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 7)
        if let action {
            Button(action: action) { content }.buttonStyle(.plain).help("展开\(label)依据")
        } else {
            content
        }
    }

    private var statusRows: some View {
        VStack(alignment: .leading, spacing: 8) {
            OikosOverview(model: platform, expansion: $oikosExpanded)
            DisclosureGroup {
                Text(statusContext?.authority.detail ?? "尚未取得 WI 账本核验")
            } label: {
                HStack(spacing: 6) {
                    LampDot(lamp: authorityFresh ? (statusContext?.authority.lamp ?? .gray) : .gray, size: 7)
                    Text(authorityFresh && statusContext?.authority.lamp == .green ? "WI 账本已核验" : "WI 账本待核验")
                        .costAnchor("global-authority-status")
                    Spacer()
                }
            }.font(.caption)
            DisclosureGroup(isExpanded: $quotaExpanded) {
                quotaDetails
            } label: {
                HStack(spacing: 6) {
                    LampDot(lamp: statusContext?.relay.codexLamp ?? .gray, size: 7)
                    Text("Codex 7 天额度")
                    Spacer()
                    Text(statusContext?.relay.codexUsedPercent.map { "已用 " + StatsFormat.percent($0) } ?? "待更新").monospacedDigit()
                }
            }.font(.caption).costAnchor("dashboard-codex-quota")
            DisclosureGroup(isExpanded: $ccQuotaExpanded) {
                ccQuotaDetails
            } label: {
                HStack(spacing: 6) {
                    LampDot(lamp: statusContext?.relay.ccLamp ?? .gray, size: 7)
                    Text("CC 7 天额度")
                    Spacer()
                    Text(ccQuotaSummary).monospacedDigit()
                }
            }.font(.caption).costAnchor("dashboard-cc-quota")
            DisclosureGroup(isExpanded: $grokQuotaExpanded) {
                grokQuotaDetails
            } label: {
                HStack(spacing: 6) {
                    LampDot(lamp: statusContext?.relay.grokLamp ?? .gray, size: 7)
                    Text("GROK 周额度")
                    Spacer()
                    Text(statusContext?.relay.grokQuotaSummary ?? "待更新").monospacedDigit()
                }
            }.font(.caption).costAnchor("dashboard-grok-quota")
        }
    }

    private var authorityFresh: Bool {
        guard let at = statusContext?.observedAt else { return false }
        return at <= now && now.timeIntervalSince(at) < 2700
    }

    /// The 7-day share, plus the 5-hour window when Relay has a current reading.
    private var ccQuotaSummary: String {
        guard let used = statusContext?.relay.ccUsedPercent else { return "待更新" }
        let weekly = "已用 " + StatsFormat.percent(used)
        guard let fiveHour = statusContext?.relay.ccFiveHourUsedPercent else { return weekly }
        return weekly + " · 5 小时 " + StatsFormat.percent(fiveHour)
    }

    private var todayUsage: CostQueryUsage? {
        guard let document, CostDashboard.beijing.isDate(document.generatedAt, inSameDayAs: now) else { return nil }
        return (document.activityView(period: .today, at: now)
            ?? document.view(period: .today, dimension: .agent, at: now))?.summary.usage
    }

    private var quotaDetails: some View {
        VStack(alignment: .leading, spacing: 7) {
            detailLine("重置时间", CostDetailContent.dateText(statusContext?.relay.codexResetsAt))
                .costAnchor("codex-quota-reset-detail")
            if let at = statusContext?.relay.codexQuotaObservedAt {
                detailLine("额度读取", CostDetailContent.dateText(at))
            }
            if statusContext?.relay.codexUsedPercent == nil {
                Text(statusContext?.relay.codexQuotaReason ?? "额度暂未更新")
            }
            Divider()
            Text("今日已记录用量 · 全部 Runtime").font(.caption.weight(.medium))
            if let usage = todayUsage {
                CostDetailContent.tokenDetails(usage.tokens)
                    .costAnchor("codex-quota-today-tokens")
                detailLine("用量截至", CostDetailContent.dateText(usage.lastUsageAt))
            } else {
                Text("今日用量尚未更新").costAnchor("codex-quota-today-unavailable")
            }
        }.padding(.top, 5).font(.caption2)
    }

    /// Relay records CC quota on real CC turns, so the read time shows how
    /// recent the figure inside the current weekly window is.
    private var ccQuotaDetails: some View {
        VStack(alignment: .leading, spacing: 7) {
            detailLine("重置时间", CostDetailContent.dateText(statusContext?.relay.ccResetsAt))
                .costAnchor("cc-quota-reset-detail")
            if let at = statusContext?.relay.ccQuotaObservedAt {
                detailLine("额度读取", CostDetailContent.dateText(at))
                    .costAnchor("cc-quota-observed-detail")
            }
            if statusContext?.relay.ccUsedPercent == nil {
                Text(statusContext?.relay.ccQuotaReason ?? "额度暂未更新")
                    .costAnchor("cc-quota-unavailable")
            }
            if let fiveHour = statusContext?.relay.ccFiveHourUsedPercent {
                detailLine("近 5 小时窗口", "已用 " + StatsFormat.percent(fiveHour))
                    .costAnchor("cc-quota-5h-detail")
            }
            Divider()
            Text("今日已记录用量 · CC").font(.caption.weight(.medium))
            if let view = document?.todayActivityView(at: now) {
                if let usage = view.runtimeUsage("cc") {
                    CostDetailContent.tokenDetails(usage.tokens)
                        .costAnchor("cc-quota-today-tokens")
                    detailLine("用量截至", CostDetailContent.dateText(usage.lastUsageAt))
                } else if view.isUsageSourceComplete {
                    Text("今日 CC 已记录用量为 0（采集范围已核验）").costAnchor("cc-quota-today-zero")
                } else {
                    Text("今日暂无 CC 用量记录；采集范围尚未完整证明，不等于零用量")
                        .costAnchor("cc-quota-today-none")
                }
            } else {
                Text("今日用量尚未更新").costAnchor("cc-quota-today-unavailable")
            }
        }.padding(.top, 5).font(.caption2)
    }

    /// The Grok account's weekly pool; it is not a per-bot cost and joins no total.
    private var grokQuotaDetails: some View {
        VStack(alignment: .leading, spacing: 7) {
            detailLine("重置时间", CostDetailContent.dateText(statusContext?.relay.grokResetsAt))
                .costAnchor("grok-quota-reset-detail")
            detailLine("额度读取", CostDetailContent.dateText(statusContext?.relay.grokQuotaObservedAt))
                .costAnchor("grok-quota-observed-detail")
            if let plan = statusContext?.relay.grokPlanLabel {
                detailLine("套餐", plan)
            }
            if statusContext?.relay.grokUsedPercent == nil {
                Text(statusContext?.relay.grokQuotaReason ?? "额度暂未更新")
                    .costAnchor("grok-quota-unavailable")
            }
            Text("账号级周额度，不是单个机器人的成本，不计入成本合计").foregroundStyle(.secondary)
        }.padding(.top, 5).font(.caption2)
    }

    private func detailLine(_ label: String, _ value: String) -> some View {
        StatsDetailRow(label, value)
    }

    private var collaboration: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let notice, query != nil {
                Text(notice.text).font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true).costAnchor("cost-data-notice")
            }
            collaborationContent.opacity(notice != nil && query != nil ? 0.6 : 1)
        }
    }

    private var collaborationContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("\(period.title) · \(query?.query.fromDate ?? "—")")
                Spacer()
                Text(activity?.populationScopeText ?? "WI 成果 · USD")
            }.font(.caption2).foregroundStyle(.secondary)
            if let query {
                if let activity {
                    stageOutputs(activity)
                    CostCompactPreview(rows: activity.presentationRows, browser: browser, onOpenDetails: onOpenDetails)
                        .costAnchor("cost-joint-activity")
                    if let selected = browser.selection.rows(activity.presentationRows).first(where: { $0.id == browser.selectedRowID }) {
                        CostRecordDetail(row: selected, view: activity, onClose: { browser.selectedRowID = nil })
                            .costAnchor("outcome-row-detail")
                    }
                } else {
                    Text("明细尚未更新，正在等待数据发布")
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, minHeight: 50, alignment: .leading)
                        .costAnchor("cost-activity-awaiting-source")
                }
                pullRequestAnomalies
                usageSourceNotes(query.summary.usage)
                DisclosureGroup("统计详情", isExpanded: $evidenceExpanded) {
                    evidence(query).padding(.top, 6)
                }.font(.caption2).foregroundStyle(.secondary).costAnchor("cost-evidence-disclosure")
            } else {
                Text(document?.unavailableReason(at: now) ?? "成本来源尚未接入或格式不兼容")
                    .font(.caption).foregroundStyle(.secondary).costAnchor("collaboration-cost-unavailable")
            }
        }
    }

    /// Output that bypassed the kernel gate needs handling, beside the output table.
    @ViewBuilder private var pullRequestAnomalies: some View {
        if let doc = document, doc.isDisplayable(at: now), let anomalies = doc.pullRequestAnomalies {
            DisclosureGroup {
                CostDetailContent.anomalyList(anomalies)
            } label: {
                Text(anomalies.headline)
                    .foregroundStyle(anomalies.needsAttention ? Color.orange : Color.secondary)
            }.font(.caption2).costAnchor("pull-request-anomalies")
            if let reference = anomalies.referenceLine {
                Text(reference).font(.caption2).foregroundStyle(.secondary)
                    .costAnchor("pull-request-unenforced-merges")
            }
        }
        // An unverified anomaly block already says GitHub was not read; no second line.
        if let unregistered = unregisteredPullRequests, let headline = unregistered.headline(period.title),
           unregistered.knownCount != nil || document?.pullRequestAnomalies?.status != "unverified" {
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 4) {
                    Text("没有任何 WI 的内核合并记录；Agent 按分支前缀提示，未核实，不计入产出与每 PR 成本。")
                        .foregroundStyle(.secondary)
                    ForEach(unregistered.items) { Text($0.line) }
                    if let count = unregistered.knownCount, count > unregistered.items.count {
                        Text("另有 \(count - unregistered.items.count) 个未列出").foregroundStyle(.secondary)
                    }
                }.font(.caption2).frame(maxWidth: .infinity, alignment: .leading)
            } label: {
                Text(headline).foregroundStyle(.secondary)
            }.font(.caption2).costAnchor("pull-request-unregistered")
        }
    }

    /// This period's merged PRs without a kernel record, from a fresh document.
    private var unregisteredPullRequests: PullRequestAnomalies.Unregistered? {
        guard let doc = document, doc.isDisplayable(at: now), let query else { return nil }
        return doc.pullRequestAnomalies?.unregistered(from: query.query.fromDate, to: query.query.toDate)
    }

    /// Usage-source gaps qualify the cost figures, so they sit beside the
    /// cost table rather than under the dashboard tiles.
    @ViewBuilder private func usageSourceNotes(_ usage: CostQueryUsage) -> some View {
        // A paused node's older rows are history, not a current read failure.
        let failures = usage.sourceDetails?.filter { $0.needsAttention && refresh?.pausedReason(node: $0.node) == nil } ?? []
        if !failures.isEmpty {
            DisclosureGroup {
                ForEach(failures, id: \.sourceRef) { source in
                    Text(source.displayName).fontWeight(.medium)
                    Text(source.failureImpact)
                    if let reason = source.reason { Text(CostText.reason(reason)) }
                    if let peer = refresh?.peerReason(node: source.node) {
                        Text("来源暂不可用（\(peer)）").costAnchor("cost-source-peer-reason")
                    }
                    Text("来源：" + source.label).foregroundStyle(.secondary)
                }
            } label: {
                Text("\(failures.count) 项成本用量来源未完整读取")
                    .foregroundStyle(.orange)
            }.font(.caption2).costAnchor("usage-source-failure-disclosure")
        }
        if let pending = usage.sourceDetails?.filter(\.hasPendingUsage).count, pending > 0 {
            Text("\(pending) 项来源有执行待回报").font(.caption2).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private func stageOutputs(_ view: CostQueryView) -> some View {
        DisclosureGroup("阶段产出 · \(period.title)") {
            if let stage = view.validStages {
                Text(stage.statusText).font(.caption2).foregroundStyle(.secondary)
                Text("当前查询范围；不随成本表行筛选变化。期间形成与截止累计不能相加。")
                    .font(.caption2).foregroundStyle(.secondary)
                if let outputs = stage.outputs {
                    stageGroups(outputs.formed, title: "期间形成（按发生时间）", items: outputs.items)
                    stageGroups(outputs.cumulative.groups, title: "截止期末累计（同一知识水位）", items: outputs.items)
                    if let groups = outputs.accepted.groups, let accepted = outputs.accepted.items {
                        let refs = Set(accepted.map(\.outputFactRef))
                        stageGroups(groups, title: "截止期末已接受版本（含明确豁免，已记录部分）", items: outputs.items.filter { refs.contains($0.factRef) })
                        Text("仅展示同一冻结版本的核验或明确豁免与人审记录；数量仍来自原产出声明，不授予执行权限。")
                            .font(.caption2).foregroundStyle(.secondary)
                    } else {
                        Text(outputs.accepted.reason == "native_assessment_conflict"
                             ? "验收记录冲突，待核对原始决定。"
                             : "验收证据尚未与准确版本关联，不推定为零或已验收。")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    if let diagnostics = stage.diagnostics {
                        ForEach(diagnostics.keys.sorted(), id: \.self) { key in
                            if let refs = diagnostics[key], !refs.isEmpty {
                                DisclosureGroup(stageDiagnosticTitle(key) + " · \(refs.count)") {
                                    ForEach(refs, id: \.self) { CostDetailContent.evidenceReference("引用", $0) }
                                }.font(.caption2)
                            }
                        }
                    }
                }
                CostDetailContent.evidenceReference("同源报告", view.reportSha256)
                detailLine("知识水位", String(view.watermark))
            } else {
                Text("阶段产出未接入或格式不兼容；已有 WI 成果记录不代表全部业务产出。")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }.font(.caption).costAnchor("stage-output-section")
    }

    private func stageDiagnosticTitle(_ key: String) -> String {
        ["membership_conflicts": "归属冲突", "revision_conflicts": "修订冲突", "shared_refs": "共享未分摊",
         "unknown_refs": "归属未知", "unresolved_link_targets": "关联目标未到", "time_unallocated_refs": "发生时间未知"][key] ?? key
    }

    private func stageGroups(_ groups: [CostStageGroup], title: String, items: [CostStageOutput]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.caption.weight(.medium))
            if groups.isEmpty {
                Text("此范围暂无已记录产出，不代表实际为零。")
            }
            ForEach(groups) { group in
                DisclosureGroup("\(group.category) · \(group.contributionText) · \(group.quantityText)") {
                    Text("可比口径：" + group.comparisonBasis)
                    Text("记录涉及 \(group.objectCount) 个去重对象；对象数不是跨类别的产出总量。")
                        .foregroundStyle(.secondary)
                    ForEach(items.filter { group.factRefs.contains($0.factRef) }) { item in
                        DisclosureGroup(item.name + " · " + item.contributionText + " · " + item.quantityText) {
                            detailLine("范围", item.extent)
                            detailLine("归属", item.wiText)
                            detailLine("发生时间", CostDetailContent.dateText(item.occurredAt))
                            detailLine("观察时间", CostDetailContent.dateText(item.observedAt))
                            detailLine("性质", "Agent 判断；形成、核验、接受分别成立")
                            CostDetailContent.evidenceReference("对象", item.objectRef)
                            CostDetailContent.evidenceReference("准确版本", item.versionRef)
                            CostDetailContent.evidenceReference("事实", item.factRef)
                            CostDetailContent.evidenceReference("事务", item.contextRef)
                            ForEach(item.evidence, id: \.self) { CostDetailContent.evidenceReference("证据", $0) }
                        }
                    }
                }
            }
        }.font(.caption2).frame(maxWidth: .infinity, alignment: .leading)
    }

    private func evidence(_ query: CostQueryView) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            detailLine("期间（结束日不含）", "\(query.query.fromDate) — \(query.query.toDate)")
            detailLine("标准成本", query.summary.cost.machineCashUsd.map { "USD " + $0 }
                ?? (query.summary.cost.hasAnomaly ? nil : query.totalCostExact.map { "USD \($0)（完整性未证明）" })
                ?? "完整成本未知")
            if query.summary.cost.machineCashUsd == nil, query.summary.cost.hasAnomaly, let priced = query.totalCostExact {
                detailLine("已计价部分", "USD " + priced).costAnchor("cost-partial-evidence")
            }
            if let counts = activity?.reasonClassCounts {
                Text("需处理 \(counts.anomaly) 组 · 仅完整性未证明 \(counts.proofGapOnly) 组")
                    .foregroundStyle(counts.anomaly > 0 ? .orange : .secondary).costAnchor("cost-reason-classes")
            }
            detailLine("用量记录", "\(query.summary.usage.eventCount) 条").costAnchor("cost-unassigned-scale")
            detailLine("用量截至", CostDetailContent.dateText(query.summary.usage.lastUsageAt)).costAnchor("cost-effective-usage-time")
            detailLine("活跃 Agent", activity.flatMap { view in
                view.activeAgentCount.map { "\($0) 个 · " + (view.activeAgents ?? []).joined(separator: "、") }
            } ?? "未提供").costAnchor("cost-active-agents")
            if let shared = activity?.multiTenantAgents, !shared.isEmpty {
                Text("在多个租户下活动，按一人计：" + shared.joined(separator: "、")).foregroundStyle(.secondary)
            }
            if let unknown = query.summary.usage.agentUnassignedEventCount, unknown > 0 {
                Text("Agent 未识别 \(unknown) 条").foregroundStyle(.orange).costAnchor("cost-agent-unassigned")
            }
            if let unlinked = query.summary.usage.wiUnassignedEventCount ?? query.summary.usage.unassignedEventCount, unlinked > 0 {
                Text("WI 未关联 \(unlinked) 条").foregroundStyle(.orange).costAnchor("cost-wi-unassigned")
            }
            if !query.summary.cost.unpriced.isEmpty { Text("未计价 \(query.summary.cost.unpricedEventCount) 条").foregroundStyle(.orange) }
            if let n = query.summary.usage.conflictedEventCount, n > 0 { Text("用量冲突 \(n) 条").foregroundStyle(.orange) }
            if let n = query.summary.usage.timeUnallocatedEventCount, n > 0 { Text("无法归期 \(n) 条").foregroundStyle(.orange) }
            DisclosureGroup("\(period.title)用量分项") { CostDetailContent.tokenDetails(query.summary.usage.tokens) }
            detailLine("合并 PR（内核）", activity.flatMap { view in
                view.validPullRequests.map { result in
                    result.status == "unavailable" ? result.note
                        : (result.count.map { "\($0) 个" } ?? "已读到 \(result.knownCount) 个") + " · " + result.note }
            } ?? "未提供").costAnchor("cost-merged-pull-requests")
            DisclosureGroup("原生 WI 成果记录（非阶段产出总量）") { CostDetailContent.outcomeList(activity?.validOutcomes?.items ?? [], result: activity?.validOutcomes) }
            DisclosureGroup("套餐与采购", isExpanded: Binding(get: { tab == .procurement }, set: { tab = $0 ? .procurement : .collaboration })) {
                procurement.padding(.top, 4)
            }
            DisclosureGroup(query.summary.usage.sourceDetailsTitle) {
                ForEach(refresh?.pausedPeers ?? [], id: \.id) { peer in
                    Text(CostNode.title(peer.id) + "：" + CostText.refreshReason(peer.reason) + "，本期覆盖不含该节点；已入账历史保留")
                        .foregroundStyle(.secondary).costAnchor("cost-source-paused-peer")
                }
                if let sources = query.summary.usage.sourceDetails, !sources.isEmpty {
                    ForEach(sources.sorted { $0.hasReadFailure && !$1.hasReadFailure }, id: \.sourceRef) { source in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(source.displayName).fontWeight(.medium)
                            let paused = refresh?.pausedReason(node: source.node)
                            Text(paused.map { $0 + " · 历史记录保留" } ?? source.readStatusText).foregroundStyle(.secondary)
                            if let reason = source.reason, paused == nil, source.needsAttention || source.hasPendingUsage { Text(CostText.reason(reason)) }
                            if source.filesInvalid > 0 { Text("异常文件 \(source.filesInvalid) 个") }
                        }.padding(.vertical, 3)
                    }
                } else {
                    Text("本次发布未包含来源明细，不能据此判断没有来源。")
                }
            }
            DisclosureGroup("技术凭据") {
                CostDetailContent.reasonLines(query.summary.cost)
                detailLine("账本水位", String(query.watermark))
                detailLine("凭据入账", CostDetailContent.dateText(query.recordedAt))
                detailLine("价格目录", CostDetailContent.dateText(query.catalogObservedAt))
                CostDetailContent.evidenceReference("报告", query.reportSha256)
                if let hash = query.catalogSha256 { CostDetailContent.evidenceReference("价表", hash) }
                if let doc = document {
                    detailLine("数据发布", CostDetailContent.dateText(doc.generatedAt))
                    CostDetailContent.evidenceReference("发布版本", doc.sourceRevision ?? "未提供")
                }
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private var procurement: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let doc = document, doc.isDisplayable(at: now), !doc.subscriptions.isEmpty {
                ForEach(doc.subscriptions.filter(\.active)) { subscription in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(subscription.name).font(.caption.weight(.medium))
                            Spacer()
                            Text("\(StatsFormat.usdDecimal(CostText.decimal(subscription.monthlyAmountUsd)))/月")
                                .font(.callout.monospacedDigit()).costAnchor("procurement-subscription")
                        }
                        if let day = subscription.billingDay, (1...31).contains(day) {
                            Text("账单日：每月 \(day) 日 · 用户于 \(subscription.confirmedOn) 确认")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
                Divider()
                if let provider = doc.openrouter {
                    HStack {
                        Text("OpenRouter API").font(.caption.weight(.medium))
                        Spacer()
                        Text(StatsFormat.usdDecimal(provider.amount(at: now)))
                            .font(.callout.monospacedDigit()).costAnchor("procurement-openrouter")
                    }
                    if provider.amount(at: now) != nil {
                        Text("\(provider.monthUtc) 已返回记录 · \(provider.requests.map { "\($0) 次请求" } ?? "请求数未提供")")
                            .font(.caption2).foregroundStyle(.secondary)
                        Text("\(provider.firstReturnedDay ?? "—") 至 \(provider.lastReturnedDay ?? "—") · UTC")
                            .font(.caption2).foregroundStyle(.secondary)
                        Text("仅含已返回记录")
                            .font(.caption2).foregroundStyle(.secondary)
                    } else {
                        Text(CostText.reason(provider.reason)).font(.caption2).foregroundStyle(.secondary)
                    }
                    Text("读取：\(CostDetailContent.dateText(provider.observedAt))").font(.caption2).foregroundStyle(.secondary)
                } else {
                    Text("OpenRouter 账务尚未接入").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(doc.subscriptions.filter { !$0.active }) { subscription in
                    Text("\(subscription.name) 已停订")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            } else {
                Text(document?.unavailableReason(at: now) ?? "采购事实尚未接入")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }


}
