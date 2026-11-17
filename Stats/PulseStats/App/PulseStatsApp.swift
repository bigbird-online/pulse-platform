import SwiftUI

@main
struct PulseStatsApp: App {
    /// The one store behind the one menu bar item.
    ///
    /// `SnapshotStore` is `@Observable`, so `@State` owns exactly one instance
    /// for the App's lifetime. This used to be shared across two scenes (⚡ and
    /// 🔧) precisely so a refresh would not start two exporter runs; spec v3
    /// §3 merged the items, so there is now only one caller and the sharing
    /// question is moot. `refresh()` still guards with `refreshInFlight`.
    @State private var store: SnapshotStore
    @State private var costBrowser = CostBrowserState()
    private let snapshotSource: AppSnapshotSource

    init() {
        snapshotSource = AppSnapshotSource(arguments: CommandLine.arguments)
        #if DEBUG
        // Test hosts never touch the user's cache or production transport.
        if ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
            || ProcessInfo.processInfo.environment["PULSE_STATS_ISOLATED_TEST"] == "1" {
            _store = State(initialValue: SnapshotStore(
                transport: DataSnapshotTransport(result: .failure(ErrorBox(message: "isolated-test-host"))),
                cache: MemorySnapshotCache()
            ))
            return
        }
        #endif
        if let inspection = snapshotSource.inspectionStore() {
            _store = State(initialValue: inspection)
            // Opening a file should reveal its content without hunting for a menu item.
            NSApplication.shared.setActivationPolicy(.regular)
            inspection.startRefreshLoop()
            Task { @MainActor [notice = snapshotSource.notice] in
                SnapshotInspectionWindow.show(store: inspection, notice: notice)
            }
            return
        }
        let cache: any SnapshotCache
        do {
            cache = try FileSnapshotCache()
        } catch {
            cache = MemorySnapshotCache()
        }

        // History is a nice-to-have that must never keep the App from starting.
        // `HistoryStore(fileManager:retention:)` throws only when Application
        // Support cannot be located at all; in that case we simply run without
        // history, exactly as every build before this one did — same shape as
        // the cache fallback above. `SnapshotStore` treats a nil `history` as
        // "don't record", so no other code path needs to know.
        let history = try? HistoryStore()

        // The annotation cannot be left to inference: a bare closure literal
        // in a ternary against `nil` infers as non-`@Sendable`, which does not
        // convert to the parameter type. Binding the closure to its own
        // explicitly typed constant keeps the sendability where the compiler
        // can see it.
        let sampler: (@Sendable (PulseSnapshot) -> HistorySample?)?
        if history == nil {
            sampler = nil
        } else {
            let make: @Sendable (PulseSnapshot) -> HistorySample? = {
                HistorySample(snapshot: $0)
            }
            sampler = make
        }

        let created = SnapshotStore(
            transport: ExporterSnapshotTransport(),
            cache: cache,
            history: history,
            // Passing the mapping only alongside a live store keeps the
            // two halves from disagreeing: `recordHistory` needs both, and
            // a mapping with nowhere to write is just wasted work on every
            // refresh.
            historySample: sampler
        )
        _store = State(initialValue: created)

        // Start the loop here, not from a view.
        //
        // It first lived in a `.task` on the menu bar label, where SwiftUI
        // cancelled it on every rebuild. Moving it into the store fixed the
        // ownership but not the trigger: the `.task` that called `start` hangs
        // off that same label, and a `MenuBarExtra` label is not guaranteed to
        // render — measured 2026-08-03, a freshly installed build sat for 3.5
        // minutes with a snapshot an hour past its TTL and never once fetched,
        // burning 0.01s of CPU in 30 seconds. `start` had simply never been
        // called.
        //
        // `init` runs exactly once per process, before any view exists, which
        // is the only place with no such precondition. The loop's first tick is
        // a date comparison a minute from now, so this costs nothing at launch.
        created.startRefreshLoop()
    }

    var body: some Scene {
        MenuBarExtra(isInserted: .constant(snapshotSource.notice == nil)) {
            StatsPopoverView(store: store, costBrowser: costBrowser)
        } label: {
            PulseMenuBarLabel(
                model: MenuBarViewModel(
                    metrics: SnapshotMetrics(store.snapshot),
                    alertTiers: store.snapshot?.alertTiers ?? .zero,
                    hasSnapshot: store.snapshot != nil,
                    presentation: store.presentation,
                    requestSourceCurrent: store.snapshot?.capabilities.first(where: { $0.id == "notify.action_package" })?.freshness.state == .fresh,
                    resources: OikosPlatformViewModel(metrics: SnapshotMetrics(store.snapshot), now: store.presentationTime),
                    now: store.presentationTime
                )
            )
            .task {
                if store.snapshot == nil { await store.refresh() }
            }
        }
        .menuBarExtraStyle(.window)
    }
}

/// Owns only the inspection window; data and error semantics remain in the shared panel/store.
@MainActor
private final class SnapshotInspectionWindow: NSObject, NSWindowDelegate {
    private static let owner = SnapshotInspectionWindow()
    private var controller: NSWindowController?

    static func show(store: SnapshotStore, notice: String?) {
        let panel = StatsPopoverView(store: store, tablePreferences: nil, sourceNotice: notice)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 580),
                              styleMask: [.titled, .closable, .miniaturizable],
                              backing: .buffered, defer: false)
        window.title = "PULSE · 本地快照"
        window.contentView = NSHostingView(rootView: panel)
        window.delegate = owner
        window.center()
        owner.controller = NSWindowController(window: window)
        owner.controller?.showWindow(nil)
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        NSApplication.shared.terminate(nil)
    }
}
