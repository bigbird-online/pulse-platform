import AppKit
import Observation
import SwiftUI

/// One read-only selection for the preview and its single, resizable detail window.
/// Owns no snapshots, ledger, timers, scheduler or extra transport.
@MainActor @Observable
final class CostBrowserState {
    var period: CostPeriod {
        didSet { if period != oldValue { selection.filters.removeAll(); selectedRowID = nil } }
    }
    var selection = CostTableSelection()
    var filtersVisible = false
    var selectedRowID: String?
    @ObservationIgnored private(set) var window: NSWindow?
    init(period: CostPeriod = .today) { self.period = period }
    func select(_ row: CostQueryGroup) { selectedRowID = selectedRowID == row.id ? nil : row.id }
    func reconcile(_ rows: [CostQueryGroup]) {
        if let selectedRowID, !rows.contains(where: { $0.id == selectedRowID }) { self.selectedRowID = nil }
    }
    func openDetails(store: SnapshotStore, preferences: UserDefaults? = .standard) {
        if window == nil {
            let created = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 650),
                styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            created.identifier = NSUserInterfaceItemIdentifier("cost-details-window")
            created.title = "产出与成本 · 完整明细"
            created.contentMinSize = NSSize(width: 760, height: 460)
            created.isReleasedWhenClosed = false
            created.tabbingMode = .disallowed
            created.contentView = NSHostingView(rootView: CostDetailsView(store: store, browser: self, preferences: preferences))
            created.center()
            window = created
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
