import Foundation
import XCTest
@testable import PulseStats

/// Spec §2.1 / §10: the two `MenuBarExtra` items must share one store, because
/// two stores would mean two SSH sessions per refresh on a single-core box.
///
/// SwiftUI scenes cannot be instantiated headlessly, so these tests pin the
/// property that actually protects us: **one store instance serves both sides,
/// and driving it from both sides still costs exactly one fetch.** The App-level
/// wiring that makes this true is `@State private var store` in
/// `PulseStatsApp`, passed by reference to both labels and both panels.
@MainActor
final class MenuBarSharingTests: XCTestCase {
    /// Counts every transport call, which is one SSH invocation in production.
    private actor CountingTransport: SnapshotTransport {
        private let data: Data
        private(set) var fetchCount = 0

        init(data: Data) { self.data = data }

        func fetch() async throws -> Data {
            fetchCount += 1
            return data
        }
    }

    /// Both menu bar items appear at launch and each runs its `.task`. With a
    /// shared store the second one sees `snapshot != nil` (or is collapsed by
    /// the in-flight guard) and no second SSH happens.
    func testBothMenuBarItemsDrivingOneStoreCauseOneFetch() async throws {
        let transport = CountingTransport(data: try fixtureData("live-v2a"))
        let store = SnapshotStore(
            transport: transport,
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )

        // ⚡ label's .task
        if store.snapshot == nil { await store.refresh() }
        // 🔧 label's .task, same store instance
        if store.snapshot == nil { await store.refresh() }
        // Both panels' .task, when opened
        if store.snapshot == nil { await store.refresh() }
        if store.snapshot == nil { await store.refresh() }

        let count = await transport.fetchCount
        XCTAssertEqual(count, 1, "one refresh cycle must cost exactly one SSH call")
    }

    /// The failure mode we are guarding against, stated explicitly: two stores
    /// double the collection. If someone later gives each `MenuBarExtra` its own
    /// `@State` store, this is what it would cost.
    func testTwoSeparateStoresWouldDoubleTheFetchCount() async throws {
        let transport = CountingTransport(data: try fixtureData("live-v2a"))
        let first = SnapshotStore(transport: transport, now: { Date(timeIntervalSince1970: 1_700_000_000) })
        let second = SnapshotStore(transport: transport, now: { Date(timeIntervalSince1970: 1_700_000_000) })
        await first.refresh()
        await second.refresh()
        let count = await transport.fetchCount
        XCTAssertEqual(count, 2, "documents why the shared instance matters")
    }

    /// Both sides render from the same snapshot, so they can never disagree.
    func testBothPanelsReadTheSameSnapshotInstance() async throws {
        let transport = CountingTransport(data: try fixtureData("live-v2a"))
        let store = SnapshotStore(
            transport: transport,
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        await store.refresh()

        let relay = RelayViewModel(metrics: SnapshotMetrics(store.snapshot), hasSnapshot: store.snapshot != nil)
        let harness = HarnessViewModel(metrics: SnapshotMetrics(store.snapshot), hasSnapshot: store.snapshot != nil)
        XCTAssertEqual(relay.outputTokens, 477_197)
        XCTAssertEqual(harness.weekCreated, 58)

        let count = await transport.fetchCount
        XCTAssertEqual(count, 1, "building both view models must not trigger collection")
    }

    /// A manual "刷新" from either panel is one refresh, not two.
    func testExplicitRefreshFromBothPanelsIsSerialisedNotDoubledPerClick() async throws {
        let transport = CountingTransport(data: try fixtureData("live-v2a"))
        let store = SnapshotStore(
            transport: transport,
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        await store.refresh()
        await store.refresh()
        // Two deliberate clicks are two collections — that is intended. What we
        // forbid is one click costing two.
        let count = await transport.fetchCount
        XCTAssertEqual(count, 2)
    }

    /// View models are pure reads over the snapshot: constructing them any
    /// number of times never reaches the transport.
    func testViewModelConstructionIsSideEffectFree() async throws {
        let transport = CountingTransport(data: try fixtureData("live-v2a"))
        let store = SnapshotStore(
            transport: transport,
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        await store.refresh()
        for _ in 0 ..< 50 {
            _ = RelayViewModel(metrics: SnapshotMetrics(store.snapshot), hasSnapshot: true)
            _ = HarnessViewModel(metrics: SnapshotMetrics(store.snapshot), hasSnapshot: true)
        }
        let count = await transport.fetchCount
        XCTAssertEqual(count, 1, "SwiftUI re-renders must never trigger collection")
    }
}
