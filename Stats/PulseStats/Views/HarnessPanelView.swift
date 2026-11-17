import SwiftUI

/// 🔧 panel — spec §2.5.
struct HarnessPanelView: View {
    let store: SnapshotStore

    private var model: HarnessViewModel {
        HarnessViewModel(metrics: SnapshotMetrics(store.snapshot), hasSnapshot: store.snapshot != nil)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    PanelSection(title: "要你操心的") {
                        ConcernList(concerns: model.concerns, hasSnapshot: model.hasSnapshot)
                    }
                    Divider()
                    workItemSection
                    Divider()
                    stalledSection
                    Divider()
                    completionTrendSection
                }
                .padding(14)
            }
            Divider()
            PanelFooter(store: store, statusText: PanelStatusText.describe(store.transportState))
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
        }
        .frame(width: 380, height: 500)
        .task {
            if store.snapshot == nil {
                await store.refresh()
            }
        }
    }

    // MARK: 工作项

    private var workItemSection: some View {
        PanelSection(title: "工作项") {
            VStack(alignment: .leading, spacing: 6) {
                StatRow(
                    lamp: model.weekLamp,
                    label: "本周新建",
                    value: StatsFormat.grouped(model.weekCreated),
                    note: stalledNote
                )
                StatRow(
                    lamp: model.dayLamp,
                    label: "今天新建",
                    value: StatsFormat.grouped(model.dayCreated),
                    note: model.dayCreated == nil ? nil : (model.dayCreated == 0 ? "还没开新的" : "都在推进")
                )
                StatRow(
                    lamp: model.completionLamp,
                    label: "今天完成",
                    value: StatsFormat.grouped(model.dayCompleted),
                    note: completionNote
                )
                Divider().padding(.vertical, 2)
                StatRow(
                    lamp: model.pendingHandoffs == nil ? .gray : .green,
                    label: "派出去还没人接",
                    value: StatsFormat.grouped(model.pendingHandoffs),
                    note: model.pendingHandoffs == nil ? nil : "时限内"
                )
            }
        }
    }

    private var stalledNote: String? {
        guard let stalled = model.stalled else { return nil }
        return stalled.isEmpty ? "都在动" : "有 \(stalled.count) 件没动"
    }

    /// The 0/0 case gets words, not a percentage — there is no rate to show.
    private var completionNote: String? {
        guard let created = model.dayCreated else { return nil }
        guard created > 0 else { return "今天没开新的" }
        return model.dayCompleted == nil ? nil : model.completionRateText
    }

    // MARK: 挂着不动的

    private var stalledSection: some View {
        PanelSection(title: "挂着不动的", caption: "超过一周没动") {
            if let stalled = model.stalled {
                if stalled.isEmpty {
                    Text("没有挂着的")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(stalled) { item in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text("\(item.days)天")
                                    .font(.caption)
                                    .monospacedDigit()
                                    .foregroundStyle(item.days > 14 ? Color.red : Color.orange)
                                    .frame(width: 34, alignment: .leading)
                                Text(item.title)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .truncationMode(.tail)
                            }
                            .help(item.title)
                            .accessibilityElement(children: .combine)
                        }
                    }
                }
            } else {
                Text("拿不到清单")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: 近 7 天完成率

    private var completionTrendSection: some View {
        PanelSection(title: "近 7 天完成率", caption: "虚线 95% / 80%") {
            VStack(alignment: .leading, spacing: 3) {
                // Needs stored history (v2-B). Until that lands this stays an
                // explicit empty state rather than a line drawn from one point.
                Sparkline(values: nil, emptyText: "要攒够几天才画得出来")
                Text("现在只有今天一天的数，还画不成趋势")
                    .font(.system(size: 9.5))
                    .foregroundStyle(.tertiary)
            }
        }
    }
}
