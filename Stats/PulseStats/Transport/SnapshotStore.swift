import Foundation
import Observation

@MainActor
protocol SnapshotCache: AnyObject {
    func load() throws -> PulseSnapshot?
    func save(_ snapshot: PulseSnapshot) throws
    func save(_ snapshot: PulseSnapshot, source: Data) throws
    func clear() throws
}

extension SnapshotCache {
    // In-memory and injected test caches need only the decoded presentation.
    // FileSnapshotCache overrides this to preserve the validated source bytes.
    func save(_ snapshot: PulseSnapshot, source: Data) throws { try save(snapshot) }
}

@MainActor
final class MemorySnapshotCache: SnapshotCache {
    private var cached: PulseSnapshot?

    init(snapshot: PulseSnapshot? = nil) {
        cached = snapshot
    }

    func load() throws -> PulseSnapshot? { cached }
    func save(_ snapshot: PulseSnapshot) throws { cached = snapshot }
    func clear() throws { cached = nil }
}

enum TransportState: Equatable, Sendable {
    case idle
    case loading
    case live(generatedAt: Date, cacheError: String?)
    case cached(generatedAt: Date, expired: Bool, fallback: CacheFallback)
    case failed(String)
    case incompatibleSchema(String)
}

enum CacheFallback: Equatable, Sendable {
    case transportFailure(String)
    case incompatibleSchema(String)
}

@MainActor
@Observable
final class SnapshotStore {
    private let transport: any SnapshotTransport
    private let cache: any SnapshotCache
    private let decoder: SnapshotDecoder
    private let now: @Sendable () -> Date
    private var refreshInFlight = false
    /// Observable clock used by presentation freshness.
    ///
    /// The App's one-minute policy tick advances this even when no fetch is
    /// due. Without an observed clock change, SwiftUI can keep rendering a
    /// once-green snapshot after its three-minute expiry until the next fetch.
    private(set) var presentationTime: Date

    /// Optional history sink (spec v2 §5). Absent by default so existing
    /// callers and tests keep their current behaviour.
    private let history: HistoryStore?

    /// Builds the row to persist from a freshly decoded snapshot. Injected so
    /// this file does not need to know how aggregates are extracted — that
    /// mapping lives with whoever owns the snapshot value types.
    private let historySample: (@Sendable (PulseSnapshot) -> HistorySample?)?

    private(set) var snapshot: PulseSnapshot?
    private(set) var transportState: TransportState = .idle

    /// The cost publication on screen. A snapshot whose carrier is missing,
    /// incompatible or `unavailable` does not replace figures already shown;
    /// only a newer publication with content does (大鸟 2026-11-04).
    private(set) var costDashboard: CostDashboard?

    /// The publisher's latest cycle from the newest snapshot.
    var costRefresh: CostRefreshStatus? { snapshot?.costRefresh }

    private func adoptCost(_ incoming: CostDashboard?) {
        let current = now()
        if let kept = costDashboard, kept.hasContent(at: current),
           !(incoming.map { $0.hasContent(at: current) && $0.generatedAt >= kept.generatedAt } ?? false) {
            return
        }
        costDashboard = incoming
    }

    /// When a refresh was last *attempted*, successful or not.
    ///
    /// Attempt rather than success on purpose: if the box is unreachable, a
    /// success-based clock would retry every tick of the polling timer instead
    /// of every half hour, turning an outage into a stream of exporter runs.
    private(set) var lastRefreshAttempt: Date?

    /// The refresh loop's task handle, owned by the store rather than a view.
    ///
    /// This used to be a `.task` modifier on the menu bar's `label:` closure.
    /// SwiftUI ties `.task` to the view's lifetime and cancels it when that view
    /// goes away — and a `MenuBarExtra` label is rebuilt whenever its model
    /// changes, so the loop was being torn down and restarted on every render,
    /// and stayed dead whenever a rebuild raced the restart. Observed
    /// 2026-08-03: a 16-hour-old process had burned 38 seconds of CPU, made no
    /// fetch for 52 minutes, and sat on a snapshot that had long since expired
    /// — the bar was reporting 数据已过期 perfectly accurately, because nothing
    /// was refreshing it any more.
    ///
    /// The store outlives every view, so the loop belongs here.
    private var refreshLoop: Task<Void, Never>?

    /// Whether the most recently *completed* fetch succeeded.
    ///
    /// `transportState` cannot answer this on its own, because `refresh()`
    /// sets `.loading` for the seconds an exporter run takes. Reading
    /// liveness off the instantaneous state therefore reported "no successful
    /// fetch" for the whole duration of every refresh — and since opening the
    /// panel triggers one, the owner saw an amber 数据已过期 flash the moment
    /// he clicked, on data that was merely being re-fetched. A fetch in
    /// progress says nothing about whether the last one worked; this flag
    /// keeps that fact across the gap.
    private var lastFetchSucceeded = false

    init(
        transport: any SnapshotTransport,
        cache: any SnapshotCache = MemorySnapshotCache(),
        decoder: SnapshotDecoder = SnapshotDecoder(),
        now: @escaping @Sendable () -> Date = Date.init,
        history: HistoryStore? = nil,
        historySample: (@Sendable (PulseSnapshot) -> HistorySample?)? = nil
    ) {
        self.transport = transport
        self.cache = cache
        self.decoder = decoder
        self.now = now
        presentationTime = now()
        self.history = history
        self.historySample = historySample
    }

    var presentation: StatusPresentation {
        StatusPresentation(
            runtimeHealth: displayRuntimeHealth,
            coverage: snapshot?.overall.coverage ?? .unknown,
            pausedOvernight: staleIsMerelyPaused
        )
    }

    /// Whether the only reason this data is old is the overnight quiet window.
    ///
    /// Two conditions, and the first is the one that matters: the last fetch
    /// must have *succeeded*. A `.cached`, `.failed`, or `.incompatibleSchema`
    /// store is showing data because something went wrong, and the quiet window
    /// is not an explanation for that — if the box stops answering at 02:00 the
    /// bar must still say so in the morning. The window only ever excuses *not
    /// having asked*.
    ///
    /// That success is read from `lastFetchSucceeded` rather than from
    /// `transportState` being `.live`. The state is momentary: `refresh()`
    /// flips it to `.loading` for the seconds an exporter run takes, and
    /// opening the panel starts one. Keying on `.live` therefore blinked an
    /// amber 数据已过期 on every click — the bug the owner hit on 2026-08-02,
    /// after the first fix had already landed. "A fetch is in flight" is not
    /// "the last fetch failed", and only the latter may cancel the excuse.
    ///
    /// The second condition is the clock rule itself, which lives in
    /// `RefreshPolicy` so it can be tested without a store or a running scene.
    private var staleIsMerelyPaused: Bool {
        guard lastFetchSucceeded else { return false }
        return RefreshPolicy.staleIsExplainedByQuietWindow(
            now: presentationTime,
            lastRefresh: lastRefreshAttempt
        )
    }

    var displayRuntimeHealth: RuntimeHealth {
        guard let snapshot else { return .unknown }
        if case let .cached(_, expired, fallback) = transportState {
            if expired { return .stale }
            if case .incompatibleSchema = fallback { return .stale }
        }
        if snapshot.expiresAt <= presentationTime {
            return .stale
        }
        return snapshot.overall.runtimeHealth
    }

    /// Start the background refresh loop. Idempotent.
    ///
    /// Called from the menu bar's `.task`, which SwiftUI may run more than once
    /// as it rebuilds the label; the guard makes the extra calls harmless
    /// instead of spawning a second loop that would double every exporter run.
    ///
    /// The loop ticks far more often than it fetches: `RefreshPolicy` owns the
    /// half-hour interval and the overnight quiet window, so a tick costs one
    /// date comparison. That short tick is what lets the first refresh after the
    /// quiet window ends — or after the Mac wakes from sleep — land within a
    /// minute rather than waiting out a half-hour phase inherited from launch.
    func startRefreshLoop(pollSeconds: Int = 60) {
        guard refreshLoop == nil else { return }
        refreshLoop = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(pollSeconds))
                if Task.isCancelled { return }
                guard let self else { return }
                await self.refreshIfDue()
            }
        }
    }

    /// Stop the loop. Only the App's teardown needs this; tests use it to keep
    /// a store from outliving the case that made it.
    func stopRefreshLoop() {
        refreshLoop?.cancel()
        refreshLoop = nil
    }

    /// Refresh only if `RefreshPolicy` says it is time.
    ///
    /// This is what the background timer calls. `refresh()` stays unconditional
    /// so the panel's 刷新 button and the cold-start path keep working exactly
    /// as before — an explicit request from the owner is never rate-limited or
    /// silenced by the overnight window.
    @discardableResult
    func refreshIfDue(now current: Date = Date()) async -> Bool {
        presentationTime = current
        let metrics = SnapshotMetrics(snapshot)
        let oikosExpiry = metrics.rows("oikos.platform.nodes")?
            .compactMap { OikosNode.date($0["expires_at"]?.string) }.min()
            ?? (metrics.rawMetric("oikos.platform.nodes") != nil ? current : nil)
        let codexExpiry: Date?
        if metrics.rawMetric(MetricID.codexUsedPercent) != nil {
            if let observed = metrics.observedAt(MetricID.codexUsedPercent),
               observed <= current.addingTimeInterval(60), let reset = metrics.codexResetsAt,
               metrics.double(MetricID.codexUsedPercent) != nil {
                // A reading stays valid until its weekly window resets.
                codexExpiry = reset
            } else {
                // Unknown/invalid quota can recover on the existing tick,
                // subject to the same five-minute floor and quiet window.
                codexExpiry = current
            }
        } else {
            codexExpiry = nil
        }
        let resourceExpiry = [oikosExpiry, codexExpiry].compactMap { $0 }.min()
        guard RefreshPolicy.shouldRefresh(now: current, lastRefresh: lastRefreshAttempt,
                                          resourceExpiresAt: resourceExpiry) else {
            return false
        }
        await refresh()
        return true
    }

    func refresh() async {
        guard !refreshInFlight else { return }
        refreshInFlight = true
        let current = now()
        presentationTime = current
        lastRefreshAttempt = current
        defer { refreshInFlight = false }
        transportState = .loading
        do {
            let payload = try await transport.fetch()
            let decoded = try decoder.decode(payload)
            snapshot = decoded
            adoptCost(decoded.costDashboard)
            presentationTime = now()
            // The fetch itself worked; a failed *cache write* does not change
            // that, which is why both branches below stay `.live` and the flag
            // is set once, above them.
            lastFetchSucceeded = true
            do {
                try cache.save(decoded, source: payload)
                transportState = .live(generatedAt: decoded.generatedAt, cacheError: nil)
            } catch {
                transportState = .live(generatedAt: decoded.generatedAt, cacheError: "cache_write_failed")
            }
            await recordHistory(for: decoded)
        } catch let error as SnapshotDecodeError {
            switch error {
            case let .unsupportedSchema(version):
                loadCacheForIncompatibleSchema(version)
            default:
                loadCacheOrFail(error)
            }
        } catch {
            loadCacheOrFail(error)
        }
    }

    /// Append one history row after a successful refresh (spec v2 §5).
    ///
    /// History is explicitly not on the critical path: this never throws and
    /// never touches `transportState`, so a failed or impossible history write
    /// leaves the refresh result exactly as it was.
    private func recordHistory(for snapshot: PulseSnapshot) async {
        guard let history, let historySample, let sample = historySample(snapshot) else { return }
        await history.appendIgnoringFailure(sample)
    }

    private func loadCacheOrFail(_ transportError: Error) {
        // Reached only when the fetch threw. Whatever we show from here on is
        // fallback, and the quiet window must not excuse it.
        lastFetchSucceeded = false
        let cached: PulseSnapshot
        do {
            guard let loaded = try cache.load() else {
                snapshot = nil
                transportState = .failed(transportError.localizedDescription)
                return
            }
            cached = loaded
        } catch {
            snapshot = nil
            transportState = .failed(error.localizedDescription)
            return
        }
        snapshot = cached
        adoptCost(cached.costDashboard)
        transportState = .cached(
            generatedAt: cached.generatedAt,
            expired: cached.expiresAt <= now(),
            fallback: .transportFailure(transportError.localizedDescription)
        )
    }

    private func loadCacheForIncompatibleSchema(_ version: String) {
        // The bytes arrived but we could not use them — not a success.
        lastFetchSucceeded = false
        let cached: PulseSnapshot
        do {
            guard let loaded = try cache.load() else {
                snapshot = nil
                transportState = .incompatibleSchema(version)
                return
            }
            cached = loaded
        } catch {
            snapshot = nil
            transportState = .failed(error.localizedDescription)
            return
        }
        snapshot = cached
        adoptCost(cached.costDashboard)
        transportState = .cached(
            generatedAt: cached.generatedAt,
            expired: cached.expiresAt <= now(),
            fallback: .incompatibleSchema(version)
        )
    }
}
