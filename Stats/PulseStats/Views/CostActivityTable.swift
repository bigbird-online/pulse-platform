import AppKit
import SwiftUI

/// A row is one upstream joint group, never a join of marginal totals.
enum CostTableColumn: String, CaseIterable, Identifiable {
    case tenant, agent, wi, outcome, amount, entrypoint, node, runtime, model
    var id: String { rawValue }
    var title: String {
        switch self {
        case .tenant: return "租户"
        case .agent: return "Agent"
        case .wi: return "WI"
        case .outcome: return "产出"
        case .entrypoint: return "入口"
        case .amount: return "金额"
        case .node: return "节点"
        case .runtime: return "Runtime"
        case .model: return "模型"
        }
    }
    var initialWidth: CGFloat {
        switch self {
        case .tenant: return 96
        case .agent: return 96
        case .wi: return 176
        case .entrypoint: return 116
        case .amount: return 72
        case .outcome: return 64
        case .node: return 96
        case .runtime: return 90
        case .model: return 158
        }
    }
    /// Whether a published view carries the tenant dimension (its rows have a tenant key).
    static func showsTenant(_ rows: [CostQueryGroup]) -> Bool { rows.contains { $0.key["tenant"] != nil } }
    /// The columns a published view supports: the tenant column only with the tenant dimension.
    /// Shared by the full table and the menu-bar preview.
    static func available(in rows: [CostQueryGroup]) -> [CostTableColumn] {
        showsTenant(rows) ? allCases : allCases.filter { $0 != .tenant }
    }
    func text(_ row: CostQueryGroup) -> String {
        if self == .tenant { return row.tenantText }
        if self == .outcome { return row.outputText }
        if self == .amount {
            if row.usage?.eventCount == 0 { return "未提供" }
            return row.cost.amount.map { StatsFormat.usdDecimal($0) }
                ?? (row.cost.reasons.contains("join_conflict") ? "冲突" : "未计价")
        }
        let value = row.key[rawValue] ?? "unassigned"
        if value == "not_applicable" { return "—" }
        if self == .wi {
            if let job = row.scheduledJobText { return job }
            switch value {
            case "cron": return "cron"
            case "none": return "无"
            case "unassigned": return "未关联"
            case "undeclared": return "未声明 WI"
            case "ambiguous": return "待核验"
            default: return row.workItem?.parent == nil ? value : "↳ " + value
            }
        }
        let text = CostQueryGroup(key: [rawValue: value], cost: row.cost).title
        return self == .entrypoint ? text + (row.subcallLabel.map { " · " + $0 } ?? "") : text
    }
}

/// Local sorting/filtering only. Does not reprice or reconstruct the summary.
struct CostTableSelection: Equatable {
    var filters: [CostTableColumn: String] = [:]
    var sortColumn: CostTableColumn = .amount
    var ascending = false
    var isFiltered: Bool { filters.values.contains { !$0.trimmingCharacters(in: .whitespaces).isEmpty } }
    /// This selection without filters or sorting on columns the view does not carry.
    func limited(to columns: [CostTableColumn]) -> CostTableSelection {
        var result = self
        result.filters = filters.filter { columns.contains($0.key) }
        if !columns.contains(sortColumn) { result.sortColumn = .amount; result.ascending = false }
        return result
    }

    func rows(_ source: [CostQueryGroup]) -> [CostQueryGroup] {
        source.filter { row in
            row.hasActivity && filters.allSatisfy { column, value in
                let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else { return true }
                if column == .amount { return amountMatches(row, text: text) }
                if column == .outcome {
                    let number = text.replacingOccurrences(of: "个", with: "").replacingOccurrences(of: "PR", with: "")
                        .trimmingCharacters(in: .whitespaces)
                    if let n = row.outputCount { return numericMatches(Decimal(n), text: number) }
                    return row.outputText.localizedCaseInsensitiveContains(text)
                }
                return column.text(row).localizedCaseInsensitiveContains(text)
                    || (row.key[column.rawValue]?.localizedCaseInsensitiveContains(text) ?? false)
            }
        }.sorted { a, b in
            let comparison: ComparisonResult
            if sortColumn == .amount || sortColumn == .outcome {
                let lhs = sortColumn == .amount ? a.cost.amount : a.outputCount.map { Decimal($0) }
                let rhs = sortColumn == .amount ? b.cost.amount : b.outputCount.map { Decimal($0) }
                // Unpriced is not zero and always follows known amounts.
                if (lhs == nil) != (rhs == nil) { return lhs != nil }
                comparison = lhs == rhs ? .orderedSame : (lhs! < rhs! ? .orderedAscending : .orderedDescending)
            } else {
                comparison = sortColumn.text(a).localizedStandardCompare(sortColumn.text(b))
            }
            if comparison == .orderedSame { return a.id < b.id }
            return comparison == (ascending ? .orderedAscending : .orderedDescending)
        }
    }

    private func amountMatches(_ row: CostQueryGroup, text: String) -> Bool {
        if ["未知", "未计价", "unknown", "unpriced"].contains(text.lowercased()) { return row.cost.amount == nil }
        guard let amount = row.cost.amount else { return false }
        return numericMatches(amount, text: text)
    }

    private func numericMatches(_ amount: Decimal, text: String) -> Bool {
        let value = text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "$", with: "").replacingOccurrences(of: ",", with: "")
        for operation in [">=", "<=", ">", "<", "="] where value.hasPrefix(operation) {
            guard let boundary = CostText.decimal(String(value.dropFirst(operation.count)).trimmingCharacters(in: .whitespaces)) else { return false }
            switch operation {
            case ">=": return amount >= boundary
            case "<=": return amount <= boundary
            case ">": return amount > boundary
            case "<": return amount < boundary
            default: return amount == boundary
            }
        }
        let range = value.components(separatedBy: "..")
        if range.count == 2, let low = CostText.decimal(range[0]), let high = CostText.decimal(range[1]) {
            return low <= high && low <= amount && amount <= high
        }
        if let exact = CostText.decimal(value) { return amount == exact }
        return false
    }
}

/// Colors that tie a table mark to its explanation in the record detail.
enum CostMarks {
    static let holderSupplement = NSColor.systemPurple
}

/// Native resizable columns and synchronized header filters, even when scrolled.
struct CostActivityTable: NSViewRepresentable {
    let rows: [CostQueryGroup]
    let scope: String
    var preferences: UserDefaults? = .standard
    var showFilters = true
    var selectedRowID: String? = nil
    var onSelection: ((CostQueryGroup?) -> Void)? = nil
    var querySelection: CostTableSelection? = nil
    var onQueryChange: ((CostTableSelection) -> Void)? = nil

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: CostGridContainer, context: Context) -> CGSize? {
        CGSize(width: proposal.width ?? 392, height: proposal.height ?? 290)
    }
    func makeCoordinator() -> Coordinator { Coordinator(preferences: preferences) }
    func makeNSView(context: Context) -> CostGridContainer {
        let view = CostGridContainer()
        context.coordinator.connect(view)
        context.coordinator.onSelection = onSelection
        context.coordinator.onQueryChange = onQueryChange
        context.coordinator.selectedID = selectedRowID
        view.setFiltersVisible(showFilters)
        context.coordinator.update(rows, scope: scope, querySelection: querySelection)
        return view
    }
    func updateNSView(_ view: CostGridContainer, context: Context) {
        context.coordinator.onSelection = onSelection
        context.coordinator.onQueryChange = onQueryChange
        context.coordinator.selectedID = selectedRowID
        view.setFiltersVisible(showFilters)
        context.coordinator.update(rows, scope: scope, querySelection: querySelection)
    }

    @MainActor final class Coordinator: NSObject, NSTableViewDataSource, NSTableViewDelegate {
        static let widthsKey = "PulseStats.costDetailColumnWidths.v1"
        let preferences: UserDefaults?
        weak var view: CostGridContainer?
        var selection = CostTableSelection()
        var selectedID: String?
        var onQueryChange: ((CostTableSelection) -> Void)?
        var onSelection: ((CostQueryGroup?) -> Void)?
        private(set) var source: [CostQueryGroup] = []
        private(set) var visible: [CostQueryGroup] = []
        private var scope: String?
        private var configuring = false

        init(preferences: UserDefaults?) { self.preferences = preferences }
        func connect(_ view: CostGridContainer) {
            self.view = view
            let table = view.table
            configuring = true
            let saved = preferences?.dictionary(forKey: Self.widthsKey) ?? [:]
            for item in CostTableColumn.allCases {
                let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(item.rawValue))
                column.title = item.title
                column.minWidth = item == .outcome ? 48 : 64; column.maxWidth = 1600
                let stored = (saved[item.rawValue] as? NSNumber)?.doubleValue
                column.width = stored.map { $0.isFinite ? min(1600, max(column.minWidth, $0)) : item.initialWidth } ?? item.initialWidth
                column.resizingMask = .userResizingMask
                column.sortDescriptorPrototype = NSSortDescriptor(key: item.rawValue, ascending: item != .amount && item != .outcome)
                table.addTableColumn(column)
            }
            table.dataSource = self; table.delegate = self
            table.target = self; table.action = #selector(selectClickedRow)
            table.sortDescriptors = [NSSortDescriptor(key: "amount", ascending: false)]
            view.filters.onFilter = { [weak self] column, text in
                self?.selection.filters[column] = text
                if let self { self.onQueryChange?(self.selection); self.refresh() }
            }
            view.clear.target = self; view.clear.action = #selector(clearFilters)
            configuring = false
        }
        func update(_ rows: [CostQueryGroup], scope: String, querySelection: CostTableSelection? = nil) {
            if self.scope != scope {
                selection.filters.removeAll()
                if querySelection == nil { selectedID = nil }
                self.scope = scope
            }
            if let querySelection { selection = querySelection }
            source = rows
            refresh()
        }
        @objc func clearFilters() { selection.filters.removeAll(); onQueryChange?(selection); refresh() }
        func refresh() {
            guard let view else { return }
            configuring = true
            let expectedSort = NSSortDescriptor(key: selection.sortColumn.rawValue, ascending: selection.ascending)
            if view.table.sortDescriptors != [expectedSort] { view.table.sortDescriptors = [expectedSort] }
            configuring = false
            // The tenant column, its filter and sorting only when the publication carries the tenant
            // dimension; otherwise every row would read "租户未知" for a dimension that was not published.
            let available = CostTableColumn.available(in: source)
            if selection.limited(to: available) != selection {
                selection = selection.limited(to: available)
                let sort = NSSortDescriptor(key: selection.sortColumn.rawValue, ascending: selection.ascending)
                configuring = true; view.table.sortDescriptors = [sort]; configuring = false
            }
            visible = selection.rows(source)
            let tenantColumn = view.table.tableColumn(withIdentifier: NSUserInterfaceItemIdentifier(CostTableColumn.tenant.rawValue))
            let hidesTenant = !available.contains(.tenant)
            if tenantColumn?.isHidden != hidesTenant {
                tenantColumn?.isHidden = hidesTenant
                view.filters.needsLayout = true
            }
            let clip = view.table.enclosingScrollView?.contentView
            let origin = clip?.bounds.origin
            view.table.reloadData()
            if let selectedID, let row = visible.firstIndex(where: { $0.id == selectedID }) {
                view.table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
            } else { view.table.deselectAll(nil) }
            if let clip, let origin {
                clip.scroll(to: clip.constrainBoundsRect(NSRect(origin: origin, size: clip.bounds.size)).origin)
                view.table.enclosingScrollView?.reflectScrolledClipView(clip)
            }
            view.filters.values = selection.filters
            view.filters.needsLayout = true; view.filters.needsDisplay = true
            if let selectedID, !visible.contains(where: { $0.id == selectedID }) {
                self.selectedID = nil
                let callback = onSelection
                DispatchQueue.main.async { callback?(nil) }
            }
            let count = source.filter(\.hasActivity).count
            view.status.stringValue = visible.isEmpty
                ? (selection.isFiltered ? "没有匹配项 · 清除筛选以查看全部" : "本期暂无已记录成果或用量")
                : (selection.isFiltered ? "筛选 \(visible.count) / \(count) 行" : "\(count) 行")
            view.hint.stringValue = selection.isFiltered ? "上方合计不随筛选改变" : "点行展开 · 横向查看更多列"
            view.clear.isHidden = !selection.isFiltered
        }
        @objc func selectClickedRow() {
            guard let table = view?.table, visible.indices.contains(table.clickedRow) else { return }
            let row = visible[table.clickedRow]
            if table.tableColumns.indices.contains(table.clickedColumn),
               table.tableColumns[table.clickedColumn].identifier.rawValue == CostTableColumn.wi.rawValue,
               let page = row.workItem?.projectPageURL {
                // The WI cell opens the kernel project page; the rest of the row expands details.
                NSWorkspace.shared.open(page)
                return
            }
            select(row)
        }
        @objc func selectDisclosure(_ sender: NSButton) {
            guard visible.indices.contains(sender.tag) else { return }
            select(visible[sender.tag])
        }
        func select(_ row: CostQueryGroup) {
            selectedID = selectedID == row.id ? nil : row.id
            onSelection?(selectedID == nil ? nil : row)
            refresh()
        }
        func numberOfRows(in tableView: NSTableView) -> Int { visible.count }
        func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
            guard let tableColumn, let column = CostTableColumn(rawValue: tableColumn.identifier.rawValue), visible.indices.contains(row) else { return nil }
            let record = visible[row]
            let identifier = NSUserInterfaceItemIdentifier("cost-cell-" + column.rawValue)
            if column == .agent {
                let container = (tableView.makeView(withIdentifier: identifier, owner: self) as? CostAgentCell) ?? CostAgentCell()
                container.identifier = identifier
                container.label.stringValue = column.text(record)
                container.label.font = .systemFont(ofSize: 12)
                container.label.textColor = .labelColor
                container.disclosure.title = selectedID == record.id ? "▾" : "▸"
                container.disclosure.target = self; container.disclosure.action = #selector(selectDisclosure(_:))
                container.disclosure.tag = row
                container.disclosure.setAccessibilityLabel("展开 " + container.label.stringValue + " 的产出与成本详情")
                container.toolTip = container.label.stringValue + " · 点击展开成果、用量与凭据"
                container.needsLayout = true
                return container
            }
            if column == .entrypoint {
                let cell = (tableView.makeView(withIdentifier: identifier, owner: nil) as? CostEntrypointCell) ?? CostEntrypointCell()
                cell.identifier = identifier
                cell.label.stringValue = CostQueryGroup(key: ["entrypoint": record.key["entrypoint"] ?? "unassigned"], cost: record.cost).title
                cell.badge.stringValue = record.subcallLabel ?? ""
                cell.badge.isHidden = record.subcallLabel == nil
                cell.toolTip = column.text(record)
                cell.setAccessibilityLabel(column.title + "：" + column.text(record))
                cell.needsLayout = true
                return cell
            }
            let cell = (tableView.makeView(withIdentifier: identifier, owner: nil) as? CostTextCell) ?? CostTextCell()
            cell.identifier = identifier
            cell.label.stringValue = column.text(record)
            cell.label.font = (column == .amount || column == .outcome)
                ? .monospacedDigitSystemFont(ofSize: 12, weight: .medium) : .systemFont(ofSize: 12)
            cell.label.alignment = (column == .amount || column == .outcome) ? .right : .left
            cell.label.textColor = column == .amount && record.cost.hasAnomaly ? .systemOrange : .labelColor
            cell.label.lineBreakMode = .byTruncatingMiddle
            if column == .wi && record.hasHolderSupplement {
                // The same mark opens the holder-supplement line in the detail.
                let text = NSMutableAttributedString(string: "● ", attributes: [.foregroundColor: CostMarks.holderSupplement, .font: NSFont.systemFont(ofSize: 12)])
                text.append(NSAttributedString(string: column.text(record), attributes: [.foregroundColor: NSColor.labelColor, .font: NSFont.systemFont(ofSize: 12)]))
                cell.label.attributedStringValue = text
            }
            cell.toolTip = column == .amount
                ? ([record.cost.exactAmount.map { "USD \($0) · \(record.usage?.eventCount ?? 0) 条用量" },
                    record.cost.note.isEmpty ? nil : "需处理：" + record.cost.note,
                    record.cost.proofGapNote.isEmpty ? nil : "完整性未证明：" + record.cost.proofGapNote]
                    .compactMap { $0 }.joined(separator: "\n"))
                : column == .wi
                ? ([[column.text(record), record.workItem?.title].compactMap { $0 }.joined(separator: " · "), record.workItem?.summary,
                    record.hasHolderSupplement ? "● 部分请求未申报，按会话写入的 WI 归入" : nil,
                    record.workItem?.projectPageURL == nil ? nil : "点击打开项目页"].compactMap { $0 }.joined(separator: "\n"))
                : (record.key[column.rawValue] ?? "未识别")
            cell.label.setAccessibilityLabel(column.title + "：" + cell.label.stringValue)
            if column == .outcome { cell.toolTip = record.outputText + " · 本 WI 内核合并的 PR，跨行不可相加；点击行看 PR 与验收成果" }
            cell.needsLayout = true
            return cell
        }
        func tableView(_ tableView: NSTableView, sortDescriptorsDidChange oldDescriptors: [NSSortDescriptor]) {
            guard !configuring, let sort = tableView.sortDescriptors.first, let key = sort.key, let column = CostTableColumn(rawValue: key) else { return }
            selection.sortColumn = column; selection.ascending = sort.ascending
            onQueryChange?(selection)
            refresh()
        }
        func tableViewColumnDidResize(_ notification: Notification) {
            guard let view else { return }
            view.filters.needsLayout = true; view.filters.needsDisplay = true
            guard !configuring else { return }
            preferences?.set(Dictionary(uniqueKeysWithValues: view.table.tableColumns.map {
                ($0.identifier.rawValue, Double($0.width))
            }), forKey: Self.widthsKey)
        }
    }
}

final class CostGridContainer: NSView {
    let table = NSTableView()
    let filters = CostFilterHeader(frame: NSRect(x: 0, y: 0, width: 0, height: 58))
    let status = NSTextField(labelWithString: "")
    let hint = NSTextField(labelWithString: "")
    let clear = NSButton(title: "清除筛选", target: nil, action: nil)
    let headerClip = NSClipView()
    private var headerHeight: NSLayoutConstraint?
    override init(frame: NSRect) {
        super.init(frame: frame)
        identifier = NSUserInterfaceItemIdentifier("cost-activity-grid")
        table.identifier = NSUserInterfaceItemIdentifier("cost-activity-table")
        // The header is a sibling of the scroll view, never its overlay.
        // NSTableHeaderView retains native column resize/sort via tableView.
        table.headerView = nil
        filters.tableView = table
        headerClip.documentView = filters
        headerClip.clipsToBounds = true
        headerClip.drawsBackground = true
        headerClip.backgroundColor = .controlBackgroundColor
        table.rowHeight = 34
        table.usesAlternatingRowBackgroundColors = false
        table.backgroundColor = .controlBackgroundColor
        table.gridStyleMask = [.solidHorizontalGridLineMask]
        table.gridColor = NSColor.separatorColor.withAlphaComponent(0.35)
        table.columnAutoresizingStyle = .noColumnAutoresizing
        table.allowsColumnReordering = false
        table.allowsColumnResizing = true
        table.allowsMultipleSelection = false
        table.allowsEmptySelection = true
        table.intercellSpacing = NSSize(width: 8, height: 1)
        let scroll = NSScrollView()
        // Each viewport has its own clipping boundary; an outer round-corner
        // mask cannot prevent row painting from crossing into a shared header.
        scroll.automaticallyAdjustsContentInsets = false
        scroll.drawsBackground = true
        scroll.contentView.clipsToBounds = true
        filters.clipsToBounds = true
        filters.wantsLayer = true
        scroll.documentView = table
        scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .legacy
        scroll.borderType = .noBorder
        scroll.wantsLayer = true
        scroll.layer?.cornerRadius = 6
        scroll.layer?.masksToBounds = true
        status.font = .systemFont(ofSize: 11); status.textColor = .secondaryLabelColor
        hint.font = .systemFont(ofSize: 10); hint.textColor = .secondaryLabelColor
        hint.lineBreakMode = .byTruncatingTail
        hint.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        status.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        clear.bezelStyle = .inline; clear.controlSize = .small
        let spacer = NSView(); spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let footer = NSStackView(views: [status, spacer, hint, clear])
        footer.orientation = .horizontal; footer.spacing = 8
        for child in [headerClip, scroll, footer] { child.translatesAutoresizingMaskIntoConstraints = false; addSubview(child) }
        let height = headerClip.heightAnchor.constraint(equalToConstant: 58)
        headerHeight = height
        NSLayoutConstraint.activate([
            headerClip.topAnchor.constraint(equalTo: topAnchor), headerClip.leadingAnchor.constraint(equalTo: leadingAnchor),
            headerClip.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor), height,
            scroll.topAnchor.constraint(equalTo: headerClip.bottomAnchor), scroll.leadingAnchor.constraint(equalTo: leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: trailingAnchor), scroll.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -6),
            footer.leadingAnchor.constraint(equalTo: leadingAnchor), footer.trailingAnchor.constraint(equalTo: trailingAnchor),
            footer.bottomAnchor.constraint(equalTo: bottomAnchor), footer.heightAnchor.constraint(equalToConstant: 20)
        ])
        scroll.contentView.postsBoundsChangedNotifications = true
        table.postsFrameChangedNotifications = true
        NotificationCenter.default.addObserver(self, selector: #selector(synchronizeHeader(_:)),
            name: NSView.boundsDidChangeNotification, object: scroll.contentView)
        NotificationCenter.default.addObserver(self, selector: #selector(synchronizeHeader(_:)),
            name: NSView.frameDidChangeNotification, object: table)
    }
    override func layout() { super.layout(); synchronizeHeader(nil) }
    @objc private func synchronizeHeader(_ notification: Notification?) {
        guard let scroll = table.enclosingScrollView else { return }
        let next = NSRect(x: 0, y: 0, width: max(table.bounds.width, scroll.contentSize.width), height: filters.showsFilters ? 58 : 30)
        if filters.frame != next { filters.frame = next }
        headerClip.scroll(to: NSPoint(x: scroll.contentView.bounds.minX, y: 0))
        filters.needsLayout = true; filters.needsDisplay = true
    }
    deinit { NotificationCenter.default.removeObserver(self) }
    func setFiltersVisible(_ visible: Bool) {
        guard filters.showsFilters != visible else { return }
        filters.showsFilters = visible
        headerHeight?.constant = visible ? 58 : 30
        needsLayout = true
        table.enclosingScrollView?.tile()
        filters.needsLayout = true; filters.needsDisplay = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
}

/// The native header still owns resizing/sorting; fields stay in its clip view.
final class CostFilterHeader: NSTableHeaderView, NSTextFieldDelegate {
    var showsFilters = true
    var values: [CostTableColumn: String] = [:]
    var onFilter: ((CostTableColumn, String) -> Void)?
    private var fields: [CostTableColumn: NSTextField] = [:]
    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }
    override func layout() {
        super.layout()
        guard let tableView else { return }
        for (index, column) in tableView.tableColumns.enumerated() {
            guard let key = CostTableColumn(rawValue: column.identifier.rawValue) else { continue }
            let field: NSTextField
            if let existing = fields[key] { field = existing }
            else {
                field = NSTextField()
                field.identifier = NSUserInterfaceItemIdentifier("cost-filter-" + key.rawValue)
                field.bezelStyle = .roundedBezel
                field.font = .systemFont(ofSize: 11)
                field.placeholderString = key == .amount ? "如 >=1" : "筛选"
                field.toolTip = key == .amount ? "金额支持 >=1、<10、1..10 或 未计价" : "筛选" + key.title
                field.setAccessibilityLabel("筛选 " + key.title)
                field.delegate = self; fields[key] = field; addSubview(field)
            }
            field.isHidden = !showsFilters || column.isHidden
            let rect = headerRect(ofColumn: index)
            field.frame = NSRect(x: rect.minX + 5, y: 29, width: max(35, rect.width - 10), height: 22)
            if field.stringValue != (values[key] ?? "") { field.stringValue = values[key] ?? "" }
        }
    }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.controlBackgroundColor.setFill(); bounds.fill()
        guard let tableView else { return }
        for (index, column) in tableView.tableColumns.enumerated() where !column.isHidden {
            let rect = headerRect(ofColumn: index)
            let sorted = tableView.sortDescriptors.first
            let marker = sorted?.key == column.identifier.rawValue ? (sorted!.ascending ? "  ↑" : "  ↓") : ""
            let paragraph = NSMutableParagraphStyle()
            paragraph.alignment = ["amount", "outcome"].contains(column.identifier.rawValue) ? .right : .left
            paragraph.lineBreakMode = .byTruncatingTail
            ((column.title + marker) as NSString).draw(in: NSRect(x: rect.minX + 6, y: 7, width: max(0, rect.width - 12), height: 18),
                withAttributes: [.font: NSFont.systemFont(ofSize: 11, weight: .semibold),
                                 .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: paragraph])
        }
    }
    func controlTextDidChange(_ obj: Notification) {
        guard let field = obj.object as? NSTextField, let key = fields.first(where: { $0.value === field })?.key else { return }
        values[key] = field.stringValue
        onFilter?(key, field.stringValue)
    }
}


/// Reusable native table cell with explicit layout. A freshly created stack
/// can lose its label width when NSTableView recycles rows during expansion.
final class CostAgentCell: NSTableCellView {
    let disclosure = NSButton(title: "▸", target: nil, action: nil)
    let label = NSTextField(labelWithString: "")
    init() {
        super.init(frame: .zero)
        disclosure.isBordered = false; disclosure.bezelStyle = .inline
        disclosure.font = .systemFont(ofSize: 10)
        label.isSelectable = true; label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = 1
        textField = label
        addSubview(disclosure); addSubview(label)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layout() {
        super.layout()
        disclosure.frame = NSRect(x: 0, y: (bounds.height-20)/2, width: 12, height: 20)
        label.frame = NSRect(x: 14, y: (bounds.height-18)/2, width: max(0,bounds.width-14), height: 18)
    }
}


/// A label is a child, not the row-sized view. Every reused cell therefore
/// retains its intrinsic text height and shares Agent's vertical centre.
class CostTextCell: NSTableCellView {
    let label = NSTextField(labelWithString: "")
    init() {
        super.init(frame: .zero)
        label.isSelectable = true
        label.maximumNumberOfLines = 1
        textField = label
        addSubview(label)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layout() {
        super.layout()
        let height: CGFloat = 18
        label.frame = NSRect(x: 0, y: (bounds.height - height) / 2, width: bounds.width, height: height)
    }
}


@MainActor private final class CostEntrypointCell: CostTextCell {
    let badge = NSTextField(labelWithString: "")
    override init() {
        super.init()
        label.font = .systemFont(ofSize: 12)
        label.alignment = .left
        label.lineBreakMode = .byTruncatingMiddle
        badge.font = .systemFont(ofSize: 9, weight: .medium)
        badge.textColor = .secondaryLabelColor
        badge.alignment = .center
        badge.wantsLayer = true
        badge.layer?.cornerRadius = 3
        addSubview(badge)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layout() {
        super.layout()
        let width = badge.isHidden ? 0 : min(max(38, badge.intrinsicContentSize.width + 8), max(0, bounds.width - 8))
        badge.frame = NSRect(x: bounds.width - width - 3, y: (bounds.height - 15) / 2, width: width, height: 15)
        badge.layer?.backgroundColor = NSColor.quaternaryLabelColor.withAlphaComponent(0.18).cgColor
        label.frame.size.width = max(0, bounds.width - width - (badge.isHidden ? 0 : 7))
    }
}
