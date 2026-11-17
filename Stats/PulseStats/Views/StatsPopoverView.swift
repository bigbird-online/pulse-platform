import AppKit
import SwiftUI

#if DEBUG
private struct RenderUATAnchor: NSViewRepresentable {
    let identifier: String

    func makeNSView(context: Context) -> NSView {
        let view = NSView(frame: .zero)
        view.identifier = NSUserInterfaceItemIdentifier(identifier)
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        nsView.identifier = NSUserInterfaceItemIdentifier(identifier)
    }
}

private extension View {
    func renderUATAnchor(_ identifier: String) -> some View {
        background(RenderUATAnchor(identifier: identifier))
    }
}
#else
private extension View {
    func renderUATAnchor(_ identifier: String) -> some View { self }
}
#endif

/// One read-only panel: overview, human requests, work board, automations and cost.
struct StatsPopoverView: View {
    let store: SnapshotStore
    var tablePreferences: UserDefaults? = .standard
    @State var costBrowser = CostBrowserState()
    var sourceNotice: String? = nil

    @State private var diagnosticsExpanded = false
    /// Survives refreshes: the section is rebuilt from each new snapshot.
    @State private var automationDisclosure = AutomationDisclosure()

    private var model: TriageViewModel {
        TriageViewModel(snapshot: store.snapshot, presentation: store.presentation)
    }

    private var relay: RelayViewModel {
        RelayViewModel(metrics: SnapshotMetrics(store.snapshot), hasSnapshot: store.snapshot != nil, at: store.presentationTime)
    }

    private var platform: OikosPlatformViewModel {
        OikosPlatformViewModel(metrics: SnapshotMetrics(store.snapshot), now: store.presentationTime)
    }

    private var harness: HarnessViewModel {
        HarnessViewModel(metrics: SnapshotMetrics(store.snapshot), hasSnapshot: store.snapshot != nil)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.horizontal, 14)
                .padding(.top, 12)
                .padding(.bottom, 10)
            Divider()
            if let sourceNotice {
                Text(sourceNotice + " · 独立查看，不写生产数据")
                    .font(.caption).foregroundStyle(.secondary)
                    .padding(.horizontal, 14).padding(.vertical, 6)
                    .accessibilityIdentifier("snapshot-source-notice")
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let failure = PanelStatusText.refreshFailure(store.transportState) {
                        Text(failure).font(.caption).foregroundStyle(.orange)
                            .accessibilityIdentifier("snapshot-refresh-failure")
                    }
                    dashboardSection
                }
                .padding(14)
            }
            Divider()
            footer
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
        }
        // Keep the original menu-bar footprint. Columns scroll inside their
        // own viewport; their total width must never enlarge the popover.
        .frame(width: 420, height: 580)
        .background(Color(nsColor: .windowBackgroundColor))
        .disclosureGroupStyle(StatsDisclosureStyle())
        .multilineTextAlignment(.leading)
        .textSelection(.enabled)
        // Read one published snapshot on open; never reprice or start business jobs.
        .task {
            await store.refresh()
        }
    }

    // MARK: 头部

    private var header: some View {
        HStack(spacing: 8) {
            Text("PULSE").font(.headline)
            LampDot(lamp: model.statusLamp, size: 7)
            if let statusText = model.statusText {
                Text(statusText).font(.caption).foregroundStyle(.secondary).lineLimit(1).help(statusText)
            }
            Spacer(minLength: 4)
            Text(model.hasSnapshot ? StatsFormat.age(model.generatedAt, now: store.presentationTime) : "待更新")
                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                .help("快照更新于 " + StatsFormat.clockBeijing(model.generatedAt) + "（北京）")
        }
    }

    private var snapshotCurrent: Bool {
        guard let snapshot = store.snapshot else { return false }
        return snapshot.generatedAt <= store.presentationTime && store.presentationTime < snapshot.expiresAt
    }
    private var actionSourceCurrent: Bool {
        snapshotCurrent && store.snapshot?.capabilities.first(where: { $0.id == "notify.action_package" })?.freshness.state == .fresh
    }

    // MARK: 自动任务

    private var automationSection: some View {
        AutomationTasksSection(model: AutomationTasksViewModel(
            metrics: SnapshotMetrics(store.snapshot), now: store.presentationTime),
            disclosure: $automationDisclosure)
    }

    // MARK: 要你出手

    private var actSection: some View {
        StatsCard {
            sectionHeader(
                lamp: !actionSourceCurrent ? .gray : model.act.isEmpty ? .green : .amber,
                title: "要你出手",
                count: model.hasSnapshot ? (actionSourceCurrent ? "\(model.actCount) 项" : "已读 \(model.act.count) 项") : nil,
                hint: "已登记需要你参与的事项；展开查看原请求"
            )
            if !model.hasSnapshot {
                emptyLine("还没拿到数据，说不好有没有事")
            } else if model.act.isEmpty {
                emptyLine(model.actCount > 0 ? "已登记 \(model.actCount) 项，明细待更新"
                    : actionSourceCurrent ? "没有待你参与的事项" : "请求数据待核验，暂不能确认是否有待办")
            } else {
                if !actionSourceCurrent {
                    Text(snapshotCurrent ? "部分请求来源待核验，以下保留可读记录" : "以下为上次记录，本次状态待核验").font(.caption).foregroundStyle(.orange)
                }
                if model.actCount != model.act.count {
                    Text("已收到 \(model.act.count) 条明细，与汇总数量尚未一致").font(.caption).foregroundStyle(.orange)
                }
                ForEach(model.act) { alert in
                    ActAlertCard(alert: alert)
                }
            }
        }
    }

    // MARK: 在办

    private var workBoardSection: some View {
        WorkBoardSection(
            model: WorkBoardViewModel(metrics: SnapshotMetrics(store.snapshot),
                                      actWorkItemIDs: Set(model.act.compactMap(\.workItemID))),
            current: snapshotCurrent, verified: model.authority.lamp == .green)
    }

    // MARK: 仪表盘与成本

    private var dashboardSection: some View {
        CostDashboardView(document: store.costDashboard, now: store.presentationTime, refresh: store.costRefresh,
            tablePreferences: tablePreferences, platform: platform,
            statusContext: DashboardStatusContext(authority: model.authority, relay: relay,
                harness: harness, health: store.presentation.runtimeHealth,
                incidents: store.snapshot?.incidents ?? [], observedAt: store.snapshot?.generatedAt),
            browser: costBrowser, onOpenDetails: { costBrowser.openDetails(store: store, preferences: tablePreferences) },
            // Panel order: 仪表盘 → 产出与成本 → 在办 → 要你出手 → 自动任务.
            trailing: AnyView(VStack(alignment: .leading, spacing: 12) {
                workBoardSection
                actSection
                automationSection
            }))
    }

    // MARK: 页脚

    private var footer: some View {
        HStack(spacing: 12) {
            Button { diagnosticsExpanded.toggle() } label: {
                Label("数据说明", systemImage: "info.circle")
            }
            .buttonStyle(.plain).font(.caption).foregroundStyle(.secondary)
            .popover(isPresented: $diagnosticsExpanded, arrowEdge: .bottom) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("数据与覆盖").font(.headline)
                        StatsDetailRow("快照时间", StatsFormat.clockBeijing(model.generatedAt) + "（北京）")
                        Text(PanelStatusText.describe(store.transportState))
                        Text("监控范围：" + (store.presentation.coverage == .full ? "已声明范围完整" : store.presentation.coverage == .partial ? "部分覆盖" : "完整范围尚未核验"))
                        if model.hasSnapshot { Text(model.capabilityFooter) }
                        Text("能力接入分类不等于本轮业务均已验证；各卡片保留自己的数据范围与原始时间。")
                        if let producer = model.producerName { StatsDetailRow("生产者", producer) }
                        Text(model.authority.detail)
                    }.font(.caption).frame(maxWidth: .infinity, alignment: .leading)
                        .multilineTextAlignment(.leading).padding(14)
                }.frame(width: 340, height: 250)
            }
            Spacer()
            if case .loading = store.transportState {
                ProgressView().controlSize(.mini)
            }
            Button("刷新") { Task { await store.refresh() } }
                .buttonStyle(.plain).font(.caption).keyboardShortcut("r")
            Button("退出") { NSApplication.shared.terminate(nil) }
                .buttonStyle(.plain).font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: 零件

    private func sectionHeader(lamp: StatusLamp, title: String, count: String?, hint: String) -> some View {
        StatsSectionHeading(lamp: lamp, title: title, count: count, hint: hint)
    }

    private func emptyLine(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// One reducer-backed incident. Kept deliberately shorter than an alert card:
/// it names current impact, last update and the next diagnostic action, while
/// event chronology stays in the alert pool instead of being repeated here.
struct OpenIncidentCard: View {
    let incident: Incident

    private var accentColor: Color {
        switch incident.severity {
        case .critical: .red
        case .warning, .info: .orange
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            Rectangle()
                .fill(accentColor)
                .frame(width: 3)
            VStack(alignment: .leading, spacing: 4) {
                Text(incident.title)
                    .font(.callout.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Text("影响：\(incident.impact)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text("最近更新：\(StatsFormat.clockBeijing(incident.updatedAt))（北京时间）")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                Text("建议：\(incident.suggestedAction)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 9)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 7))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "\(incident.title)，影响 \(incident.impact)，最近更新"
                + " \(StatsFormat.clockBeijing(incident.updatedAt)) 北京时间，"
                + "建议 \(incident.suggestedAction)"
        )
    }
}

/// A request's WI identity and creation-age badge.
private struct WorkItemSummaryHeader: View {
    let item: InFlightWorkItem
    let prefix: String
    let rowID: String

    private var ageColor: Color {
        switch item.ageLamp {
        case .green: return .green
        case .amber: return .orange
        case .red: return .red
        case .gray: return .secondary
        }
    }
    private var identity: some View {
        Text(item.summaryLine).font(.caption.weight(.medium))
            .fixedSize(horizontal: true, vertical: false)
            .renderUATAnchor(prefix + "-summary-" + rowID)
    }
    private var badges: some View {
        HStack(spacing: 4) {
            Text(item.statusAgeText)
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(ageColor).padding(.horizontal, 6).padding(.vertical, 3)
                .background(ageColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 4))
                .help("创建至今：0–7天绿、8–30天橙、31天起红；不是故障等级或停滞时间")
                .renderUATAnchor(prefix + "-status-age-" + rowID)
            if let profile = item.workflowProfile {
                Text(profile).font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.secondary).padding(.horizontal, 5).padding(.vertical, 3)
                    .background(Color.secondary.opacity(0.09), in: RoundedRectangle(cornerRadius: 4))
                    .help("WI 当前流程；lightweight 正式升级为 standard 后显示 standard")
                    .renderUATAnchor(prefix + "-workflow-profile-" + rowID)
            }
        }.fixedSize(horizontal: true, vertical: false)
    }
    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 6) { identity; Spacer(minLength: 0); badges }
            VStack(alignment: .leading, spacing: 4) {
                Text(item.summaryLine).font(.caption.weight(.medium))
                    .fixedSize(horizontal: false, vertical: true)
                    .renderUATAnchor(prefix + "-summary-" + rowID)
                badges
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Preserve the original request; show its supporting metadata only on demand.
struct ActAlertCard: View {
    let alert: TriagedAlert
    @State private var expanded: Bool
    init(alert: TriagedAlert, initiallyExpanded: Bool = false) {
        self.alert = alert; _expanded = State(initialValue: initiallyExpanded)
    }
    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 6) {
                Text(alert.body).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                if let meta = alert.meta { Text(meta).foregroundStyle(.secondary) }
            }
            .font(.caption).frame(maxWidth: .infinity, alignment: .leading)
            .renderUATAnchor("act-detail-" + alert.id)
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                if let item = alert.workItem {
                    WorkItemSummaryHeader(item: item, prefix: "act", rowID: alert.id)
                } else {
                    Text(alert.meta ?? alert.title).font(.caption.weight(.medium)).lineLimit(2)
                        .renderUATAnchor("act-summary-" + alert.id)
                }
                if alert.workItem != nil || alert.meta != nil {
                    Text(alert.title).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                        .help(alert.title).renderUATAnchor("act-title-" + alert.id)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
        .font(.caption).disclosureGroupStyle(StatsDisclosureStyle())
        .padding(.vertical, 4)
        .renderUATAnchor("act-row-" + alert.id)
    }
}

private struct OikosNodeRow: View {
    let node: OikosNode
    var isCurrent = true
    private func percent(_ value: Int?) -> String { value.map { "\($0)%" } ?? "未知" }
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(node.label).font(.caption).fontWeight(.medium)
                Spacer(minLength: 4)
                Text("磁盘 \(percent(node.disk))").font(.caption).monospacedDigit()
                Text(isCurrent ? node.statusText : "历史读数").font(.caption2)
                    .foregroundStyle(!isCurrent ? Color.secondary : node.status == "incident" ? Color.red : ["ok", "unknown"].contains(node.status) ? Color.secondary : Color.orange)
            }
            Text("内存余 \(percent(node.memory)) · swap \(percent(node.swap)) · \(node.writable == "ok" ? "可写" : node.writable == "failed" ? "写入失败" : "可写性未知") · inode \(percent(node.inode))")
                .font(.caption2).foregroundStyle(.secondary).monospacedDigit()
            if node.runnerExpected != "n/a" {
                Text("Runner \(["active":"运行中", "inactive":"未运行", "failed":"运行失败"][node.runner] ?? "待核验") · 预期 \(["inactive":"按安排停用", "active":"运行"][node.runnerExpected] ?? "待核验")")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            if !node.reason.isEmpty {
                Text(node.reason).font(.caption2).foregroundStyle(.secondary)
            }
            if node.status == "incident" {
                Text(node.deliveryText).font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
    }
}


struct OikosOverview: View {
    let model: OikosPlatformViewModel?
    @State private var expanded: Bool
    private let externalExpansion: Binding<Bool>?
    init(model: OikosPlatformViewModel?, initiallyExpanded: Bool = false, expansion: Binding<Bool>? = nil) {
        self.model = model
        self.externalExpansion = expansion
        _expanded = State(initialValue: initiallyExpanded)
    }
    private var expansion: Binding<Bool> { externalExpansion ?? $expanded }
    private var summary: String {
        guard let model, model.available else { return "数据待更新" }
        if model.confirmedCount > 0 { return "有资源异常" }
        if model.unknownCount > 0 { return "部分资源待核验" }
        return model.lamp == .amber ? "容量需关注" : "资源正常"
    }
    var body: some View {
        DisclosureGroup(isExpanded: expansion) {
            VStack(alignment: .leading, spacing: 5) {
                Text(model?.reason ?? "尚未取得 Oikos 资源采样").font(.caption2).foregroundStyle(.secondary)
                if let model {
                    ForEach(model.nodes) { OikosNodeRow(node: $0, isCurrent: model.available) }
                    if let at = model.observedAt {
                        Text("采样 \(StatsFormat.clockBeijing(at))（北京）")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }.frame(maxWidth: .infinity, alignment: .leading).multilineTextAlignment(.leading)
                .automationAnchor("oikos-resource-details")
        } label: {
            HStack(spacing: 6) {
                LampDot(lamp: model?.lamp ?? .gray, size: 7)
                Text("Oikos 平台概览")
                Spacer(minLength: 8)
                Text(summary).foregroundStyle(.secondary)
            }
        }.font(.caption).disclosureGroupStyle(StatsDisclosureStyle()).automationAnchor("oikos-overview")
    }
}

/// Which automation groups and rows the reader opened. Held by the popover so a
/// refresh, which rebuilds the section from new rows, keeps the reader's place.
struct AutomationDisclosure: Equatable {
    var expandedTasks: Set<String> = []
    /// Purpose groups start open; only the ones the reader closed are kept.
    var collapsedGroups: Set<String> = []
    var historyOpen = false
}

/// 「自动任务」: purpose groups, four columns, and two lines per row (node over
/// task name). Colour carries only the actual run result; gray is「未运行」.
struct AutomationTasksSection: View {
    let model: AutomationTasksViewModel
    private let external: Binding<AutomationDisclosure>?
    @State private var local: AutomationDisclosure
    init(model: AutomationTasksViewModel, disclosure: Binding<AutomationDisclosure>) {
        self.model = model; external = disclosure
        _local = State(initialValue: disclosure.wrappedValue)
    }
    init(model: AutomationTasksViewModel, initiallyExpanded: Set<String> = []) {
        self.model = model; external = nil
        _local = State(initialValue: AutomationDisclosure(expandedTasks: initiallyExpanded))
    }
    /// The section reads its own state, seeded from the popover's copy and written back on
    /// every change, so a section rebuilt for a new snapshot reopens what the reader opened.
    private var state: Binding<AutomationDisclosure> { $local }
    static let planWidth: CGFloat = 64, recentWidth: CGFloat = 50, statusWidth: CGFloat = 86
    static func textColor(_ lamp: StatusLamp) -> Color {
        switch lamp {
        case .green: return .green
        case .red: return .red
        case .amber: return .orange
        case .gray: return .secondary
        }
    }
    private func taskBinding(_ id: String) -> Binding<Bool> {
        Binding(get: { state.wrappedValue.expandedTasks.contains(id) },
                set: { open in if open { state.wrappedValue.expandedTasks.insert(id) } else { state.wrappedValue.expandedTasks.remove(id) } })
    }
    private func groupBinding(_ id: String) -> Binding<Bool> {
        Binding(get: { !state.wrappedValue.collapsedGroups.contains(id) },
                set: { open in if open { state.wrappedValue.collapsedGroups.remove(id) } else { state.wrappedValue.collapsedGroups.insert(id) } })
    }
    private func columns<A: View, B: View, C: View, D: View>(_ task: A, _ plan: B, _ recent: C, _ status: D) -> some View {
        HStack(alignment: .top, spacing: 6) {
            task.frame(maxWidth: .infinity, alignment: .leading)
            plan.frame(width: Self.planWidth, alignment: .leading)
            recent.frame(width: Self.recentWidth, alignment: .leading)
            status.frame(width: Self.statusWidth, alignment: .leading)
        }
    }
    private func twoLines(_ upper: String, _ lower: String, upperIsSmall: Bool) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(upper).font(upperIsSmall ? .caption2 : .caption).foregroundStyle(upperIsSmall ? Color.secondary : Color.primary)
                .lineLimit(1).truncationMode(.middle)
            Text(lower).font(upperIsSmall ? .caption : .caption2).foregroundStyle(upperIsSmall ? Color.primary : Color.secondary)
                .lineLimit(upperIsSmall ? 2 : 1).fixedSize(horizontal: false, vertical: true)
        }.help(upper + " " + lower)
    }
    private func row(_ task: AutomationTaskRow) -> some View {
        DisclosureGroup(isExpanded: taskBinding(task.id)) {
            VStack(alignment: .leading, spacing: 5) {
                Text(task.reason).foregroundStyle(Self.textColor(task.lamp))
                if !task.note.isEmpty { Text(task.note).foregroundStyle(.orange) }
                StatsDetailRow("应交付", task.expectation)
                StatsDetailRow("负责人", task.owner + " · " + task.nodeLine)
                StatsDetailRow("计划", task.plan)
                Text(task.progress)
                if let at = task.lastResultAt {
                    StatsDetailRow("最近结果", StatsFormat.clockBeijing(at) + "（北京）")
                }
                if let at = task.nextAt {
                    StatsDetailRow("下次计划", StatsFormat.clockBeijing(at) + "（北京）")
                }
                if let at = task.sourceAt {
                    StatsDetailRow("来源读取", StatsFormat.clockBeijing(at) + "（北京）")
                }
                DisclosureGroup("执行依据") {
                    Text(task.reference).font(.caption2).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }.font(.caption2).foregroundStyle(.secondary).padding(.vertical, 5)
                .frame(maxWidth: .infinity, alignment: .leading)
                .automationAnchor("automation-detail-" + task.id)
                // Nested in a purpose group, the style's own inset is not applied a second time.
                .padding(.leading, 16)
        } label: {
            let recent = task.lastResultLines
            columns(twoLines(task.nodeLine, task.title, upperIsSmall: true),
                    twoLines(task.planHead, task.planSub, upperIsSmall: false),
                    twoLines(recent.0, recent.1, upperIsSmall: false),
                    HStack(alignment: .firstTextBaseline, spacing: 4) {
                        Circle().fill(task.lamp.color).frame(width: 6, height: 6)
                        Text(task.status).font(.caption).foregroundStyle(Self.textColor(task.lamp))
                            .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                    })
                .accessibilityElement(children: .combine)
                .accessibilityLabel(task.title + "：" + task.status)
        }.font(.caption).automationAnchor("automation-row-" + task.id)
    }
    var body: some View {
        StatsCard {
            StatsSectionHeading(lamp: .gray, title: "自动任务",
                count: model.tasks.isEmpty ? nil : "\(model.tasks.count) 项" + (model.purposeCount > 0 ? " · \(model.purposeCount) 类用途" : ""),
                hint: "按用途分组；颜色只表达本期实际运行结果，灰色是未运行")
            if model.tasks.isEmpty {
                Text("任务数据待更新").font(.caption).foregroundStyle(.orange)
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.summary).font(.caption)
                    if !model.coverage.isEmpty {
                        Text(model.coverage + " · 时间均为北京时间").font(.caption2).foregroundStyle(.secondary)
                    }
                }.automationAnchor("automation-summary")
                columns(Text("任务"), Text("执行计划"), Text("最近结果"), Text("本期状态"))
                    .font(.caption2).foregroundStyle(.secondary).padding(.leading, 32)
                    .accessibilityHidden(true).automationAnchor("automation-columns")
            }
            ForEach(model.groups) { group in
                DisclosureGroup(isExpanded: groupBinding(group.id)) {
                    ForEach(group.tasks) { task in row(task) }
                } label: {
                    HStack(alignment: .firstTextBaseline) {
                        Text(group.title).font(.caption.weight(.semibold))
                        Spacer(minLength: 6)
                        Text(group.summary).font(.caption2).foregroundStyle(.secondary)
                    }
                }.automationAnchor("automation-group-" + group.id)
            }
            if !model.history.isEmpty {
                DisclosureGroup(isExpanded: state.historyOpen) {
                    ForEach(model.history) { task in
                        VStack(alignment: .leading, spacing: 1) {
                            Text(task.title).font(.caption)
                            Text(task.note).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    Text("历史问题不计入本期状态，也不因新一轮成功消失。").font(.caption2).foregroundStyle(.secondary)
                } label: {
                    HStack(alignment: .firstTextBaseline) {
                        Text("历史问题").font(.caption.weight(.semibold))
                        Spacer(minLength: 6)
                        Text("\(model.history.count) 项未结").font(.caption2).foregroundStyle(.orange)
                    }
                }.automationAnchor("automation-history")
            }
        }.disclosureGroupStyle(StatsDisclosureStyle()).automationAnchor("automation-section")
            .onChange(of: local) { _, value in external?.wrappedValue = value }
    }
}

/// 「在办」：在办 WI 与协作合同，按“下一步该谁动”分到各 Agent 名下。
/// 折叠行只给件数与卡住摘要；展开后一件一行；再点一行才显示核对凭据。
struct WorkBoardSection: View {
    let model: WorkBoardViewModel
    /// 快照仍在有效期内。
    var current = true
    /// 账本跨端一致性已核验。
    var verified = true
    @State private var expandedGroups: Set<String>
    @State private var expandedEntries: Set<String>
    init(model: WorkBoardViewModel, current: Bool = true, verified: Bool = true,
         initiallyExpanded: Set<String> = [], initiallyExpandedEntries: Set<String> = []) {
        self.model = model; self.current = current; self.verified = verified
        _expandedGroups = State(initialValue: initiallyExpanded)
        _expandedEntries = State(initialValue: initiallyExpandedEntries)
    }
    private func binding(_ set: Binding<Set<String>>, _ id: String) -> Binding<Bool> {
        Binding(get: { set.wrappedValue.contains(id) },
                set: { open in if open { set.wrappedValue.insert(id) } else { set.wrappedValue.remove(id) } })
    }
    private var lamp: StatusLamp {
        guard model.groups != nil, current else { return .gray }
        return model.stuckCount > 0 ? .amber : .green
    }
    private func groupLabel(_ group: WorkBoardGroup) -> some View {
        let spoken = group.title + "，" + [group.countText, group.summary].compactMap { $0 }.joined(separator: "，")
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(group.title).font(.caption.weight(.medium)).lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
            if let count = group.countText {
                Text(count).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    .fixedSize(horizontal: true, vertical: false)
            }
            Text(group.summary).font(.caption2)
                .foregroundStyle(group.stuck ? Color.orange : Color.secondary).lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, group.stuck ? 3 : 0)
        .background(group.stuck ? Color.orange.opacity(0.12) : Color.clear, in: RoundedRectangle(cornerRadius: 4))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(spoken + (group.stuck ? "，有卡住项" : ""))
    }
    var body: some View {
        StatsCard {
            StatsSectionHeading(lamp: lamp, title: "在办", count: model.countText,
                hint: "橙色 = 卡住：合同待接超 6 小时或过了期限、已交回或已完成但发起方未处理、WI 超 7 天没推进、草案超 3 天")
            if let groups = model.groups {
                Text("按“下一步该谁动”分组；橙色 = 卡住").font(.caption2).foregroundStyle(.secondary)
                if !current {
                    Text("以下为上次记录，当前状态待核验").font(.caption).foregroundStyle(.orange)
                } else if !verified {
                    Text("已读取当前账本，跨端一致性待核验").font(.caption).foregroundStyle(.secondary)
                }
                if !model.contractsAvailable {
                    Text("协作合同来源暂不可用，以下只列 WI").font(.caption).foregroundStyle(.orange)
                }
                if groups.isEmpty {
                    Text("当前没有在办的 WI 或合同").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(groups) { group in
                    DisclosureGroup(isExpanded: binding($expandedGroups, group.id)) {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(group.entries) { entry in
                                WorkBoardLine(entry: entry, expanded: binding($expandedEntries, entry.id))
                            }
                        }
                    } label: { groupLabel(group) }
                    .font(.caption).automationAnchor("work-board-group-" + group.id)
                }
            } else {
                Text(current ? "读不到在办看板，说不好有多少" : "以下为上次记录，看板数据不完整")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }.disclosureGroupStyle(StatsDisclosureStyle()).automationAnchor("work-board-section")
    }
}

/// 一张合同或一件 WI 一行；点开才显示时间线和编号。
struct WorkBoardLine: View {
    let entry: WorkBoardEntry
    @Binding var expanded: Bool
    private func tag(_ text: String, _ color: Color) -> some View {
        Text(text).font(.system(size: 10, weight: .medium)).foregroundStyle(color)
            .padding(.horizontal, 3).overlay(RoundedRectangle(cornerRadius: 3).stroke(color.opacity(0.7)))
            .fixedSize()
    }
    private var spoken: String {
        let kind = entry.kind == .contract ? "合同" : "WI"
        let marks = (entry.mainOnly ? "，主身" : "") + "，" + entry.line + "，" + entry.status
        return kind + marks + (entry.stuck ? "，卡住" : "") + (entry.inAct ? "，已在要你出手" : "")
    }
    @ViewBuilder private var contractEvidence: some View {
        StatsDetailRow("合同号", entry.id)
        StatsDetailRow("方向", entry.route + (entry.mainOnly ? "（只投递给主身）" : ""))
        StatsDetailRow("工作项", [entry.workItemID, entry.workItemTitle].filter { !$0.isEmpty }.joined(separator: " · "))
        StatsDetailRow("目的", entry.purpose)
        StatsDetailRow("下一步", entry.next)
        ForEach(Array(entry.timeline.enumerated()), id: \.offset) { _, line in
            Text(line).fixedSize(horizontal: false, vertical: true)
        }
    }
    @ViewBuilder private var workItemEvidence: some View {
        StatsDetailRow("WI", entry.workItemID)
        StatsDetailRow("标题", entry.workItemTitle)
        StatsDetailRow("负责人", entry.owner)
        if !entry.holder.isEmpty, entry.holder != entry.owner { StatsDetailRow("持有人", entry.holder) }
        if let at = entry.updatedAt { StatsDetailRow("最近推进", StatsFormat.clockBeijing(at) + "（北京）") }
        if let at = entry.createdAt { StatsDetailRow("建立", StatsFormat.clockBeijing(at) + "（北京）") }
        if entry.inAct { Text("这件 WI 的原请求在「要你出手」。").foregroundStyle(.secondary) }
    }
    private var evidence: some View {
        VStack(alignment: .leading, spacing: 3) {
            if entry.kind == .contract { contractEvidence } else { workItemEvidence }
        }.font(.caption2).foregroundStyle(.secondary).padding(.vertical, 2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .automationAnchor("work-board-detail-" + entry.id)
    }
    private var label: some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            tag(entry.kind == .contract ? "合同" : "WI", .secondary)
            if entry.mainOnly { tag("主身", .orange) }
            Text(entry.line).lineLimit(1).help(entry.line)
            Spacer(minLength: 4)
            if entry.inAct {
                Text("已在要你出手").font(.system(size: 10)).foregroundStyle(.secondary).fixedSize()
                    .automationAnchor("work-board-in-act-" + entry.id)
            }
            Text(entry.status).foregroundStyle(entry.stuck ? Color.orange : Color.secondary)
                .monospacedDigit().lineLimit(1).fixedSize()
        }
        .font(.caption2)
        .padding(.horizontal, entry.stuck ? 3 : 0)
        .background(entry.stuck ? Color.orange.opacity(0.12) : Color.clear, in: RoundedRectangle(cornerRadius: 4))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(spoken)
    }
    var body: some View {
        DisclosureGroup(isExpanded: $expanded) { evidence } label: { label }
            .automationAnchor("work-board-entry-" + entry.id)
    }
}

#if DEBUG
private struct AutomationRenderAnchor: NSViewRepresentable {
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
    @ViewBuilder func automationAnchor(_ value: String) -> some View {
        #if DEBUG
        background(AutomationRenderAnchor(value: value))
        #else
        self
        #endif
    }
}
