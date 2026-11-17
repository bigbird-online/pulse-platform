import SwiftUI

/// ⚡ panel — spec §2.2. Five blocks ordered by "do I have to do something".
struct RelayPanelView: View {
    let store: SnapshotStore

    private var model: RelayViewModel {
        RelayViewModel(metrics: SnapshotMetrics(store.snapshot), hasSnapshot: store.snapshot != nil, at: store.presentationTime)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    PanelSection(title: "要你操心的") {
                        ConcernList(concerns: model.concerns, hasSnapshot: model.hasSnapshot)
                    }
                    Divider()
                    computeSection
                    Divider()
                    usageSection
                    Divider()
                    leaderboardSection
                    Divider()
                    deliverySection
                }
                .padding(14)
            }
            Divider()
            PanelFooter(store: store, statusText: PanelStatusText.describe(store.transportState))
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
        }
        .frame(width: 380, height: 560)
        .task {
            if store.snapshot == nil {
                await store.refresh()
            }
        }
    }

    // MARK: 算力

    private var computeSection: some View {
        PanelSection(title: "算力") {
            VStack(alignment: .leading, spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    StatRow(
                        lamp: model.ccLamp,
                        label: "Claude 这周额度",
                        value: StatsFormat.percent(model.ccUsedPercent),
                        note: model.ccResetsAt.map { "\(StatsFormat.clockUTC($0)) 重置" }
                    )
                    if let pct = model.ccUsedPercent {
                        MeterBar(fraction: pct / 100, lamp: model.ccLamp)
                    }
                }

                StatRow(
                    lamp: model.ccLivenessLamp,
                    label: "对话进程",
                    value: model.ccProcesses.map { "\($0) 个" } ?? StatsFormat.unknown,
                    note: busyNote
                )

                VStack(alignment: .leading, spacing: 4) {
                    StatRow(
                        lamp: model.codexLamp,
                        label: "Codex 这周额度",
                        value: StatsFormat.percent(model.codexUsedPercent)
                    )
                    MeterBar(
                        fraction: model.codexUsedPercent.map { $0 / 100 },
                        lamp: model.codexLamp
                    )
                    Text(codexResetNote)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }

                StatRow(
                    lamp: model.botsOnline.map { $0 > 0 ? .green : .red } ?? .gray,
                    label: "飞书机器人",
                    value: model.botsOnline.map { "\($0)" } ?? StatsFormat.unknown,
                    note: model.botsOnline == nil ? nil : "在线"
                )
            }
        }
    }

    private var busyNote: String? {
        guard let busy = model.ccBusy, let idle = model.ccIdle else { return nil }
        return "\(busy) 忙 \(idle) 闲"
    }

    private var codexResetNote: String {
        guard let resetsAt = model.codexResetsAt else { return "重置时间未知" }
        return "\(StatsFormat.clockUTC(resetsAt)) 重置（UTC）"
    }

    // MARK: 今天用量

    private var usageSection: some View {
        PanelSection(title: "今天用量", caption: "UTC 00:00 起") {
            VStack(alignment: .leading, spacing: 10) {
                DualFigure(
                    leftTitle: "输入",
                    leftValue: StatsFormat.grouped(model.inputTokens),
                    leftCaption: "走 resume，历史不重计",
                    rightTitle: "输出",
                    rightValue: StatsFormat.grouped(model.outputTokens),
                    rightCaption: model.outputShare.map { "占总量 \($0)" } ?? "占比未知"
                )

                QuadStats(items: [
                    .init(id: "inbound", value: StatsFormat.grouped(model.inbound), caption: "入站"),
                    .init(id: "turns", value: StatsFormat.grouped(model.turns), caption: "回合"),
                    .init(
                        id: "failures",
                        value: StatsFormat.grouped(model.failures),
                        caption: "失败",
                        isBad: (model.failures ?? 0) > 0
                    ),
                    .init(
                        id: "p50",
                        value: StatsFormat.duration(milliseconds: model.durationP50),
                        caption: "中位时长"
                    ),
                ])

                VStack(alignment: .leading, spacing: 3) {
                    // v2-B will supply a stored series here; today the curve is
                    // drawn from this snapshot's own hourly buckets and shows an
                    // empty state when they are missing.
                    Sparkline(values: model.hourlyOutputTokens, emptyText: "还没有曲线数据")
                    HStack {
                        Text("00时").font(.system(size: 9.5)).foregroundStyle(.tertiary)
                        Spacer()
                        Text(peakCaption).font(.system(size: 9.5)).foregroundStyle(.tertiary)
                        Spacer()
                        Text("23时").font(.system(size: 9.5)).foregroundStyle(.tertiary)
                    }
                }
            }
        }
    }

    private var peakCaption: String {
        guard let hourly = model.hourlyOutputTokens,
              let peak = hourly.max(), peak > 0,
              let hour = hourly.firstIndex(of: peak)
        else { return "每小时输出" }
        return "每小时输出 · 高峰 \(String(format: "%02d", hour))时 \(StatsFormat.compactCount(Int(peak)))"
    }

    // MARK: 谁在烧

    private var leaderboardSection: some View {
        PanelSection(title: "谁在烧", caption: "按输出排序") {
            if let agents = model.agents {
                if agents.isEmpty {
                    Text("今天还没人用")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    let peak = Double(agents.first?.outputTokens ?? 0)
                    // v2-C lists agents flat. v2-D hangs the expandable session
                    // list off `model.sessions`, which is already grouped by
                    // agent below — the structure is here, the interaction is not.
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(agents) { agent in
                            UsageBarRow(
                                name: agent.agent,
                                value: StatsFormat.grouped(agent.outputTokens),
                                trailing: agent.turns.map { "\($0)轮" } ?? "",
                                fraction: peak > 0 ? Double(agent.outputTokens) / peak : 0,
                                badge: agent.hasMultipleSessions ? "\(agent.sessionCount ?? 0) 个对话" : nil
                            )
                            ForEach(sessions(for: agent.agent)) { session in
                                UsageBarRow(
                                    name: sessionCaption(session),
                                    value: StatsFormat.grouped(session.outputTokens),
                                    trailing: session.turns.map { "\($0)轮" } ?? "",
                                    fraction: peak > 0 ? Double(session.outputTokens) / peak : 0,
                                    isSub: true
                                )
                            }
                        }
                    }
                }
            } else {
                Text("拿不到用量明细")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// Sub-rows are only interesting when an agent has more than one session;
    /// a single-session agent would just repeat its own bar.
    private func sessions(for agent: String) -> [SessionUsage] {
        let owned = (model.sessions ?? []).filter { $0.agent == agent }
        return owned.count > 1 ? owned : []
    }

    /// Spec §2.4: newest session is 当前, the rest are 残留 — worded plainly.
    private func sessionCaption(_ session: SessionUsage) -> String {
        let owned = (model.sessions ?? [])
            .filter { $0.agent == session.agent }
            .sorted { ($0.lastActivity ?? .distantPast) > ($1.lastActivity ?? .distantPast) }
        let isCurrent = owned.first?.id == session.id
        return "\(session.sessionPrefix) · \(isCurrent ? "在用" : "旧的")"
    }

    // MARK: 投递质量

    private var deliverySection: some View {
        PanelSection(title: "投递质量") {
            VStack(alignment: .leading, spacing: 6) {
                StatRow(
                    lamp: deliveryLamp,
                    label: "送达",
                    value: deliveryValue,
                    note: deliveryNote
                )
                StatRow(
                    lamp: model.inbound == nil ? .gray : .green,
                    label: "入站",
                    value: StatsFormat.grouped(model.inbound),
                    note: inboundTypesNote
                )
            }
        }
    }

    private var deliveryLamp: StatusLamp {
        guard let attempts = model.deliveryAttempts, !attempts.isEmpty else { return .gray }
        let retried = attempts.filter { (Int($0.key) ?? 1) > 1 }.values.reduce(0, +)
        return retried == 0 ? .green : .amber
    }

    private var deliveryValue: String {
        guard let sent = model.deliverySent else { return StatsFormat.unknown }
        return StatsFormat.grouped(sent)
    }

    private var deliveryNote: String? {
        guard let attempts = model.deliveryAttempts, !attempts.isEmpty else { return nil }
        let retried = attempts.filter { (Int($0.key) ?? 1) > 1 }.values.reduce(0, +)
        return retried == 0 ? "都是一次成功" : "\(retried) 次重试过"
    }

    private var inboundTypesNote: String? {
        guard let types = model.inboundTypes, !types.isEmpty else { return nil }
        return types
            .sorted { $0.value > $1.value }
            .prefix(2)
            .map { "\($0.key) \($0.value)" }
            .joined(separator: " · ")
    }
}
