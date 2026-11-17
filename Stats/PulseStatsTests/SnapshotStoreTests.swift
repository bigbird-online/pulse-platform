import Foundation
import XCTest
@testable import PulseStats

@MainActor
final class SnapshotStoreTests: XCTestCase {
    func testSuccessfulFixtureBecomesLiveAndCachesSnapshot() async throws {
        let cache = MemorySnapshotCache()
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("ok-full"))),
            cache: cache,
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        await store.refresh()
        XCTAssertEqual(store.snapshot?.overall.runtimeHealth, .ok)
        XCTAssertEqual(store.transportState, .live(generatedAt: try XCTUnwrap(store.snapshot?.generatedAt), cacheError: nil))
        XCTAssertEqual(try cache.load(), store.snapshot)
    }

    func testExpiredCacheFallsBackToStaleWhenTransportFails() async throws {
        let cached = try SnapshotDecoder().decode(try fixtureData("expired-cache"))
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .failure(ErrorBox(message: "fixture offline"))),
            cache: MemorySnapshotCache(snapshot: cached),
            now: { Date(timeIntervalSince1970: 1_800_000_000) }
        )
        await store.refresh()
        XCTAssertEqual(store.snapshot, cached)
        XCTAssertEqual(store.displayRuntimeHealth, .stale)
        XCTAssertEqual(
            store.transportState,
            .cached(generatedAt: cached.generatedAt, expired: true, fallback: .transportFailure("fixture offline"))
        )
    }

    func testExpiredSnapshotPresentationDrivesMenuBarAndPanelToStale() async throws {
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("expired-cache"))),
            now: { Date(timeIntervalSince1970: 1_800_000_000) }
        )
        await store.refresh()
        let snapshot = try XCTUnwrap(store.snapshot)

        let menu = MenuBarViewModel(
            metrics: SnapshotMetrics(snapshot),
            alertTiers: snapshot.alertTiers,
            hasSnapshot: true,
            presentation: store.presentation
        )
        let panel = TriageViewModel(
            snapshot: snapshot,
            presentation: store.presentation
        )

        XCTAssertEqual(store.displayRuntimeHealth, .stale)
        XCTAssertEqual(menu.statusLamp, .amber)
        XCTAssertEqual(menu.statusText, "数据已过期")
        XCTAssertEqual(panel.statusLamp, .amber)
        XCTAssertEqual(panel.statusText, "数据已过期")
    }

    func testUnknownMajorPreservesLastSuccessButMarksItStale() async throws {
        let cached = try SnapshotDecoder().decode(try fixtureData("ok-full"))
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("unknown-major"))),
            cache: MemorySnapshotCache(snapshot: cached),
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        await store.refresh()
        XCTAssertEqual(store.snapshot, cached)
        XCTAssertEqual(store.displayRuntimeHealth, .stale)
        XCTAssertEqual(
            store.transportState,
            .cached(
                generatedAt: cached.generatedAt,
                expired: false,
                fallback: .incompatibleSchema("pulse.monitoring.snapshot.v3")
            )
        )
    }

    func testInvalidJSONUsesUnexpiredCacheWithoutChangingItsHealth() async throws {
        let cached = try SnapshotDecoder().decode(try fixtureData("warning-partial"))
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("invalid"))),
            cache: MemorySnapshotCache(snapshot: cached),
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        await store.refresh()
        XCTAssertEqual(store.snapshot, cached)
        XCTAssertEqual(store.displayRuntimeHealth, .warning)
        XCTAssertEqual(
            store.transportState,
            .cached(
                generatedAt: cached.generatedAt,
                expired: false,
                fallback: .transportFailure("Snapshot is not valid JSON.")
            )
        )
    }

    func testUnknownMajorWithoutCacheIsExplicitlyIncompatibleAndUnknown() async throws {
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("unknown-major"))),
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        await store.refresh()
        XCTAssertNil(store.snapshot)
        XCTAssertEqual(store.displayRuntimeHealth, .unknown)
        XCTAssertEqual(store.transportState, .incompatibleSchema("pulse.monitoring.snapshot.v3"))
    }

    func testConcurrentRefreshesUseOneTransportFetch() async throws {
        let transport = BlockingSnapshotTransport(data: try fixtureData("ok-full"))
        let store = SnapshotStore(
            transport: transport,
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        let first = Task { await store.refresh() }
        await transport.waitUntilStarted()
        let second = Task { await store.refresh() }
        await Task.yield()
        let countWhileBlocked = await transport.fetchCount
        XCTAssertEqual(countWhileBlocked, 1)
        await transport.release()
        await first.value
        await second.value
        let finalCount = await transport.fetchCount
        XCTAssertEqual(finalCount, 1)
    }

    func testCacheWriteFailureKeepsLiveSnapshotButIsExplicit() async throws {
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("ok-full"))),
            cache: FailingSnapshotCache(),
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        await store.refresh()
        let generatedAt = try XCTUnwrap(store.snapshot?.generatedAt)
        XCTAssertEqual(
            store.transportState,
            .live(generatedAt: generatedAt, cacheError: "cache_write_failed")
        )
        XCTAssertEqual(store.displayRuntimeHealth, .ok)
    }

    /// Clicking the menu bar must not blink an amber 数据已过期 mid-fetch.
    ///
    /// The 2026-08-02 regression, found by the owner *after* the quiet-window
    /// fix had landed: `staleIsMerelyPaused` keyed on `transportState == .live`,
    /// but `refresh()` sets `.loading` while the SSH round trip runs — measured
    /// at ~12s against production. Opening the panel starts a refresh, so every
    /// click turned the excuse off for twelve seconds and the bar flashed the
    /// exact alarm the fix was supposed to have removed.
    ///
    /// The fetch here is held open deliberately: the assertion is about what
    /// the bar renders *while* it is in flight, which is the window the owner
    /// was looking at.
    func testRefreshInFlightDoesNotBlinkStaleDuringTheQuietWindow() async throws {
        let cached = try SnapshotDecoder().decode(try fixtureData("expired-cache"))
        // 22:40 Beijing — inside the quiet window, snapshot past its TTL.
        let night = beijing(2026, 8, 2, 22, 40)
        let transport = BlockingSnapshotTransport(data: try fixtureData("expired-cache"))
        let store = SnapshotStore(
            transport: transport,
            cache: MemorySnapshotCache(snapshot: cached),
            now: { night }
        )

        // First fetch succeeds, establishing "the last fetch worked".
        let priming = Task { await store.refresh() }
        await transport.waitUntilStarted()
        await transport.release()
        await priming.value
        XCTAssertTrue(store.presentation.pausedOvernight, "settled state should be excused")

        // Now the click: a second refresh, observed mid-flight.
        let clicking = Task { await store.refresh() }
        await transport.waitUntilStarted()
        await Task.yield()
        XCTAssertEqual(store.transportState, .loading, "precondition: fetch is in flight")
        XCTAssertTrue(
            store.presentation.pausedOvernight,
            "a fetch in progress must not cancel the quiet-window excuse"
        )
        let summary = SnapshotStatusSummary(
            hasSnapshot: true,
            actCount: 0,
            presentation: store.presentation
        )
        XCTAssertEqual(summary.text, "夜间暂停刷新")
        XCTAssertEqual(summary.lamp, .gray)

        await transport.release()
        await clicking.value
    }

    /// The flag must track success, not merely "a fetch finished".
    ///
    /// Guards the other half: if the box stops answering overnight, the excuse
    /// has to switch back off. A quiet window explains *not asking*; it never
    /// explains asking and being refused.
    func testAFailedFetchOvernightStillReportsStale() async throws {
        let cached = try SnapshotDecoder().decode(try fixtureData("expired-cache"))
        let night = beijing(2026, 8, 2, 22, 40)
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .failure(ErrorBox(message: "box offline"))),
            cache: MemorySnapshotCache(snapshot: cached),
            now: { night }
        )
        await store.refresh()
        XCTAssertFalse(
            store.presentation.pausedOvernight,
            "a failed fetch must not hide behind the quiet window"
        )
        XCTAssertEqual(store.displayRuntimeHealth, .stale)
        let summary = SnapshotStatusSummary(
            hasSnapshot: true,
            actCount: 0,
            presentation: store.presentation
        )
        XCTAssertEqual(summary.text, "数据已过期")
        XCTAssertEqual(summary.lamp, .amber)
    }

    /// 刷新循环归 store 所有，不随 view 生死。
    ///
    /// 2026-08-03：循环原本是菜单栏 label 上的 `.task`。SwiftUI 把 `.task` 绑在
    /// view 生命周期上，label 每次重建都会取消它——实测一个跑了 16 小时的进程
    /// 只烧了 38 秒 CPU、52 分钟没取过数、快照早已过期。大鸟看到的「数据已过期」
    /// 报得完全准确，只是没人再去刷新它了。
    ///
    /// 幂等很关键：`.task` 会被 SwiftUI 反复触发，若不守卫就会起第二个循环，
    /// 对单核机器意味着每轮双倍 SSH。
    func testRefreshLoopIsOwnedByTheStoreAndStartsOnlyOnce() async throws {
        let transport = CountingSnapshotTransport(data: try fixtureData("ok-full"))
        let store = SnapshotStore(
            transport: transport,
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        // 连叫三次，只应有一个循环在跑。
        store.startRefreshLoop(pollSeconds: 1)
        store.startRefreshLoop(pollSeconds: 1)
        store.startRefreshLoop(pollSeconds: 1)

        // 等够两个 tick：单循环最多取 2 次，三个循环会取 6 次。
        try await Task.sleep(for: .seconds(2))
        store.stopRefreshLoop()
        let count = await transport.fetchCount
        XCTAssertLessThanOrEqual(count, 2, "重复 start 不得叠加出多个循环")

        // 停掉之后不得再取数——否则 App 退出时循环会活着继续打 SSH。
        let afterStop = await transport.fetchCount
        try await Task.sleep(for: .seconds(2))
        let settled = await transport.fetchCount
        XCTAssertEqual(settled, afterStop, "stopRefreshLoop 后不得继续取数")
    }

    private func beijing(_ year: Int, _ month: Int, _ day: Int, _ hour: Int, _ minute: Int) -> Date {
        var components = DateComponents()
        components.year = year
        components.month = month
        components.day = day
        components.hour = hour
        components.minute = minute
        components.timeZone = RefreshPolicy.timeZone
        return RefreshPolicy.beijingCalendar.date(from: components)!
    }

    func testInvalidPersistentCacheIsRejectedExplicitly() async throws {
        let temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("pulse-stats-store-test-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: temporaryDirectory) }
        let cacheURL = temporaryDirectory.appendingPathComponent("snapshot-v2.json")
        try FileManager.default.createDirectory(at: temporaryDirectory, withIntermediateDirectories: true)
        try Data("not-json".utf8).write(to: cacheURL)
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .failure(ErrorBox(message: "fixture offline"))),
            cache: FileSnapshotCache(url: cacheURL),
            now: { Date(timeIntervalSince1970: 1_700_000_000) }
        )
        await store.refresh()
        XCTAssertNil(store.snapshot)
        XCTAssertEqual(store.displayRuntimeHealth, .unknown)
        XCTAssertEqual(store.transportState, .failed("cache_invalid"))
    }
}

@MainActor
private final class FailingSnapshotCache: SnapshotCache {
    func load() throws -> PulseSnapshot? { nil }
    func save(_ snapshot: PulseSnapshot) throws { throw FileSnapshotCacheError.writeFailed }
    func clear() throws {}
}

/// 只数取数次数，不阻塞——用于观察循环行为。
private actor CountingSnapshotTransport: SnapshotTransport {
    let data: Data
    private(set) var fetchCount = 0

    init(data: Data) { self.data = data }

    func fetch() async throws -> Data {
        fetchCount += 1
        return data
    }
}

private actor BlockingSnapshotTransport: SnapshotTransport {
    let data: Data
    private(set) var fetchCount = 0
    private var continuation: CheckedContinuation<Void, Never>?

    init(data: Data) {
        self.data = data
    }

    func fetch() async throws -> Data {
        fetchCount += 1
        await withCheckedContinuation { continuation = $0 }
        return data
    }

    func waitUntilStarted() async {
        while fetchCount == 0 {
            await Task.yield()
        }
    }

    func release() {
        continuation?.resume()
        continuation = nil
    }
}
