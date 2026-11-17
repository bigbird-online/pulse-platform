import AppKit
import SwiftUI

#if DEBUG
private struct BrowserAnchor: NSViewRepresentable {
    let id: String
    func makeNSView(context: Context) -> NSView {
        let view = NSView(); view.identifier = NSUserInterfaceItemIdentifier(id); return view
    }
    func updateNSView(_ view: NSView, context: Context) { view.identifier = NSUserInterfaceItemIdentifier(id) }
}
#endif
extension View {
    @ViewBuilder func browserAnchor(_ id: String) -> some View {
        #if DEBUG
        background(BrowserAnchor(id: id))
        #else
        self
        #endif
    }
}

/// Five original joint-query groups, not a second aggregation. No nested scroller
/// or floating header: labels and rows move together with the main popover.
struct CostCompactPreview: View {
    let rows: [CostQueryGroup]
    @Bindable var browser: CostBrowserState
    var onOpenDetails: (() -> Void)?
    private var filtered: [CostQueryGroup] { browser.selection.rows(rows) }
    private let columns: [CostTableColumn] = [.agent, .wi, .outcome, .amount]
    private func width(_ column: CostTableColumn) -> CGFloat? {
        switch column { case .agent: return 68; case .outcome: return 52; case .amount: return 72; default: return nil }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if browser.filtersVisible {
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 6) {
                    ForEach(CostTableColumn.allCases) { column in
                        HStack(spacing: 4) {
                            Text(column.title).font(.caption2).frame(width: 44, alignment: .leading)
                            TextField(column == .amount ? ">=1 / 未计价" : "筛选", text: Binding(
                                get: { browser.selection.filters[column] ?? "" },
                                set: { browser.selection.filters[column] = $0 }
                            )).textFieldStyle(.roundedBorder).font(.caption)
                                .accessibilityLabel("筛选 " + column.title)
                        }
                    }
                }.padding(.bottom, 8)
            }
            HStack(spacing: 8) {
                ForEach(columns) { column in
                    Button {
                        if browser.selection.sortColumn == column { browser.selection.ascending.toggle() }
                        else { browser.selection.sortColumn = column; browser.selection.ascending = column != .amount && column != .outcome }
                    } label: {
                        Text(column.title + (browser.selection.sortColumn == column ? (browser.selection.ascending ? " ↑" : " ↓") : ""))
                            .frame(maxWidth: .infinity, alignment: [.outcome,.amount].contains(column) ? .trailing : .leading)
                    }.buttonStyle(.plain).frame(width: width(column))
                }
            }.font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                .padding(.horizontal, 6).padding(.vertical, 8).browserAnchor("cost-preview-header")
            Divider()
            ForEach(Array(filtered.prefix(5))) { row in
                Button { browser.select(row) } label: {
                    HStack(alignment: .center, spacing: 8) {
                        ForEach(columns) { column in
                            Group {
                                if column == .agent {
                                    HStack(spacing: 3) {
                                        Image(systemName: browser.selectedRowID == row.id ? "chevron.down" : "chevron.right").font(.system(size: 8))
                                        VStack(alignment: .leading, spacing: 1) {
                                            Text(column.text(row))
                                            Text(row.tenantText).font(.system(size: 9)).foregroundStyle(.secondary)
                                        }
                                    }.frame(maxWidth: .infinity, alignment: .leading)
                                } else {
                                    Text(column.text(row))
                                        .foregroundStyle(column == .amount && row.cost.hasAnomaly ? Color.orange : Color.primary)
                                        .frame(maxWidth: .infinity, alignment: [.outcome,.amount].contains(column) ? .trailing : .leading)
                                }
                            }.frame(width: width(column)).lineLimit(1).truncationMode(.middle)
                                .monospacedDigit().help(column.text(row))
                        }
                    }.font(.system(size: 11)).foregroundStyle(.primary)
                        .padding(.horizontal, 6).padding(.vertical, 10)
                        .background(browser.selectedRowID == row.id ? Color.accentColor.opacity(0.12) : Color.clear)
                        .contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel("展开 " + CostTableColumn.agent.text(row) + " · " + CostTableColumn.wi.text(row))
                    .browserAnchor("cost-preview-row-" + row.id)
                Divider().opacity(0.4)
            }
            if filtered.isEmpty {
                Text(browser.selection.isFiltered ? "没有匹配项，清除筛选可查看全部" : "本期暂无已记录成果或用量")
                    .font(.caption).foregroundStyle(.secondary).padding(.vertical, 12)
            }
            HStack(spacing: 8) {
                Text("显示 \(min(filtered.count,5)) / \(filtered.count) 行")
                if browser.selection.isFiltered {
                    Button("清除筛选") { browser.selection.filters.removeAll() }.buttonStyle(.plain)
                }
                Spacer(minLength: 4)
                Button("查看全部 ↗") { onOpenDetails?() }.buttonStyle(.plain).foregroundStyle(.tint)
                    .disabled(onOpenDetails == nil).accessibilityLabel("打开可调整大小的完整明细窗口")
            }.font(.caption2).foregroundStyle(.secondary).padding(.top, 9)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .browserAnchor("cost-compact-preview")
        .onChange(of: filtered.map(\.id)) { _, _ in browser.reconcile(filtered) }
    }
}

/// This is a standalone window, not a sheet or an inner popover scroller.
/// The single App store supplies live publications; opening never triggers I/O.
struct CostDetailsView: View {
    let store: SnapshotStore
    @Bindable var browser: CostBrowserState
    var preferences: UserDefaults? = .standard
    private var document: CostDashboard? { store.costDashboard }
    private var activity: CostQueryView? { document?.activityView(period: browser.period, at: store.presentationTime) }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("产出与成本").font(.title3.weight(.semibold))
                    Text(activity?.populationScopeText ?? "已登记活动").font(.caption2).foregroundStyle(.secondary)
                }
                Picker("统计期间", selection: $browser.period) {
                    ForEach(CostPeriod.allCases) { Text($0.title).tag($0) }
                }.pickerStyle(.segmented).labelsHidden().frame(width: 160)
                Spacer()
                Button(browser.filtersVisible ? "收起筛选" : "筛选") { browser.filtersVisible.toggle() }
                Button("刷新") { Task { await store.refresh() } }.keyboardShortcut("r")
            }
            if let failure = PanelStatusText.refreshFailure(store.transportState) {
                Text(failure).font(.caption).foregroundStyle(.orange)
                    .accessibilityIdentifier("snapshot-refresh-failure")
            }
            if let notice = document?.notice(at: store.presentationTime, refresh: store.costRefresh), activity != nil {
                Text(notice.text).font(.caption).foregroundStyle(.orange)
                    .browserAnchor("cost-full-notice")
            }
            if let activity {
                HStack {
                    Text("\(activity.query.fromDate) — \(activity.query.toDate)（结束日不含）")
                    Spacer()
                    Text(activity.summary.cost.machineCashUsd != nil || activity.totalCostAmount == nil
                        ? "标准成本 \(StatsFormat.usdDecimal(activity.summary.cost.amount))"
                        : activity.summary.cost.hasAnomaly
                        ? "已计价部分 \(StatsFormat.usdDecimal(activity.totalCostAmount)) · 完整成本未知"
                        : "标准成本 \(StatsFormat.usdDecimal(activity.totalCostAmount)) · 完整性未证明")
                    Text("筛选不改变统计范围")
                }.font(.caption).foregroundStyle(.secondary)
                CostActivityTable(rows: activity.presentationRows,
                    scope: "\(browser.period.rawValue)|\(activity.query.fromDate)|\(activity.query.toDate)",
                    preferences: preferences, showFilters: browser.filtersVisible, selectedRowID: browser.selectedRowID,
                    onSelection: { browser.selectedRowID = $0?.id },
                    querySelection: browser.selection, onQueryChange: { browser.selection = $0 })
                    .frame(maxWidth: .infinity, maxHeight: .infinity).clipped()
                    .browserAnchor("cost-full-table")
                if let selected = browser.selection.rows(activity.presentationRows).first(where: { $0.id == browser.selectedRowID }) {
                    Divider()
                    ScrollView {
                        CostRecordDetail(row: selected, view: activity, onClose: { browser.selectedRowID = nil })
                    }.frame(height: 190).clipped().browserAnchor("cost-full-detail")
                }
                Text("发布于 " + StatsFormat.clockBeijing(document?.generatedAt) + "（北京） · 与菜单栏使用同一份数据")
                    .font(.caption2).foregroundStyle(.secondary)
            } else {
                Text(document?.unavailableReason(at: store.presentationTime) ?? "明细尚未发布，不能把缺失显示成零")
                    .foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
        }
        .padding(16).frame(minWidth: 760, minHeight: 460)
        .background(Color(nsColor: .windowBackgroundColor))
        .disclosureGroupStyle(StatsDisclosureStyle()).multilineTextAlignment(.leading).textSelection(.enabled)
        .onChange(of: activity?.presentationRows.map(\.id)) { _, _ in
            browser.reconcile(browser.selection.rows(activity?.presentationRows ?? []))
        }
    }
}
