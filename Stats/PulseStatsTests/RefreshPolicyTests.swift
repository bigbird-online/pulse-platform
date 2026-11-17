import Foundation
import XCTest
@testable import PulseStats

/// The menu bar had no refresh timer at all until 2026-07-28 — one fetch at
/// launch and nothing after, against snapshots that expire in three minutes.
/// These tests exist so the absence is impossible to reintroduce silently.
final class RefreshPolicyTests: XCTestCase {
    /// Beijing wall-clock → `Date`, matching how the owner states the rule.
    private func beijing(_ month: Int, _ day: Int, _ hour: Int, _ minute: Int = 0) -> Date {
        var components = DateComponents()
        components.year = 2026
        components.month = month
        components.day = day
        components.hour = hour
        components.minute = minute
        components.timeZone = RefreshPolicy.timeZone
        return RefreshPolicy.beijingCalendar.date(from: components)!
    }

    func testTheIntervalIsHalfAnHourAsInstructed() {
        XCTAssertEqual(RefreshPolicy.interval, 1800)
    }

    func testTheQuietWindowIsTwelveHoursOfBeijingNight() {
        // Evening and small hours are quiet...
        XCTAssertTrue(RefreshPolicy.isQuiet(at: beijing(7, 28, 20)))
        XCTAssertTrue(RefreshPolicy.isQuiet(at: beijing(7, 28, 23, 59)))
        XCTAssertTrue(RefreshPolicy.isQuiet(at: beijing(7, 29, 0)))
        XCTAssertTrue(RefreshPolicy.isQuiet(at: beijing(7, 29, 7, 59)))
        // ...the working day is not.
        XCTAssertFalse(RefreshPolicy.isQuiet(at: beijing(7, 29, 8)))
        XCTAssertFalse(RefreshPolicy.isQuiet(at: beijing(7, 29, 12)))
        XCTAssertFalse(RefreshPolicy.isQuiet(at: beijing(7, 29, 19, 59)))
    }

    /// The window wraps midnight, so a naive `start..<end` comparison would
    /// never be true. Pin the total at twelve hours.
    func testTheQuietWindowSpansExactlyTwelveHours() {
        let quietHours = (0..<24).filter {
            RefreshPolicy.isQuiet(at: beijing(7, 28, $0))
        }
        XCTAssertEqual(quietHours.count, 12)
    }

    func testAColdStartRefreshesEvenAtNight() {
        // "没数据" all night is worse than one SSH call, and this is the path
        // taken when the App is launched inside the quiet window.
        XCTAssertTrue(
            RefreshPolicy.shouldRefresh(now: beijing(7, 29, 3), lastRefresh: nil)
        )
    }

    func testItRefreshesOnceHalfAnHourHasPassedDuringTheDay() {
        let last = beijing(7, 28, 14, 0)
        XCTAssertFalse(
            RefreshPolicy.shouldRefresh(now: beijing(7, 28, 14, 29), lastRefresh: last)
        )
        XCTAssertTrue(
            RefreshPolicy.shouldRefresh(now: beijing(7, 28, 14, 30), lastRefresh: last)
        )
    }

    func testItStaysSilentOvernightNoMatterHowStaleTheDataIs() {
        // Twelve hours past due, but inside the quiet window: still no fetch.
        XCTAssertFalse(
            RefreshPolicy.shouldRefresh(
                now: beijing(7, 29, 3),
                lastRefresh: beijing(7, 28, 15)
            )
        )
    }

    func testTheFirstMorningTickCatchesUp() {
        // Quiet from 20:00; at 08:00 the backlog is collected immediately
        // rather than waiting out another half hour.
        XCTAssertTrue(
            RefreshPolicy.shouldRefresh(
                now: beijing(7, 29, 8, 0),
                lastRefresh: beijing(7, 28, 19, 45)
            )
        )
    }

    // MARK: - The quiet window as an explanation, not an alarm

    /// 08:00 looking at a 19:45 snapshot: twelve hours old entirely by design.
    func testTheMorningGlanceAtLastNightsSnapshotIsExplainedByTheWindow() {
        XCTAssertTrue(
            RefreshPolicy.staleIsExplainedByQuietWindow(
                now: beijing(7, 29, 7, 59),
                lastRefresh: beijing(7, 28, 19, 45)
            )
        )
    }

    /// Same staleness, but in working hours nothing excuses it.
    func testDaytimeStalenessIsNeverExplainedAway() {
        XCTAssertFalse(
            RefreshPolicy.staleIsExplainedByQuietWindow(
                now: beijing(7, 29, 14, 0),
                lastRefresh: beijing(7, 29, 11, 0)
            )
        )
        // 08:00 sharp is already outside the window.
        XCTAssertFalse(
            RefreshPolicy.staleIsExplainedByQuietWindow(
                now: beijing(7, 29, 8, 0),
                lastRefresh: beijing(7, 28, 19, 45)
            )
        )
    }

    /// Data older than the window itself was already stale before 20:00, so the
    /// night cannot be blamed for it — it keeps its amber.
    func testStalenessPredatingTheWindowIsNotExcused() {
        XCTAssertFalse(
            RefreshPolicy.staleIsExplainedByQuietWindow(
                now: beijing(7, 29, 7, 0),
                lastRefresh: beijing(7, 27, 9, 0) // ~46h: two days back
            )
        )
    }

    /// Never having loaded is not something the night explains.
    func testNeverHavingRefreshedIsNotExcusedByTheWindow() {
        XCTAssertFalse(
            RefreshPolicy.staleIsExplainedByQuietWindow(
                now: beijing(7, 29, 3, 0),
                lastRefresh: nil
            )
        )
    }

    func testAClockMovingBackwardsIsNotExcusedByTheWindow() {
        XCTAssertFalse(
            RefreshPolicy.staleIsExplainedByQuietWindow(
                now: beijing(7, 29, 3, 0),
                lastRefresh: beijing(7, 29, 5, 0)
            )
        )
    }

    func testAClockMovingBackwardsDoesNotStallRefreshes() {
        // A last-attempt stamp in the future would otherwise suppress fetches
        // until real time caught up with it.
        XCTAssertTrue(
            RefreshPolicy.shouldRefresh(
                now: beijing(7, 28, 14),
                lastRefresh: beijing(7, 28, 16)
            )
        )
    }
}

/// The store side of the same rule.
@MainActor
final class SnapshotStoreRefreshDueTests: XCTestCase {
    private actor CountingTransport: SnapshotTransport {
        private let data: Data
        private(set) var fetchCount = 0
        init(data: Data) { self.data = data }
        func fetch() async throws -> Data {
            fetchCount += 1
            return data
        }
    }

    private func beijing(_ hour: Int, _ minute: Int = 0) -> Date {
        var components = DateComponents()
        components.year = 2026
        components.month = 7
        components.day = 28
        components.hour = hour
        components.minute = minute
        components.timeZone = RefreshPolicy.timeZone
        return RefreshPolicy.beijingCalendar.date(from: components)!
    }

    private func quotaFixture(observed: Date?, reset: Date?, available: Bool = true) throws -> Data {
        var document = try JSONSerialization.jsonObject(with: fixtureData("live-v2a")) as! [String: Any]
        var metrics = document["metrics"] as! [[String: Any]]
        metrics.removeAll { ($0["id"] as? String)?.hasPrefix("relay.codex.") == true }
        let common: [String: Any] = ["capability_id": "s1.request_trace_capture", "unit": "percent",
                                    "window": "current", "quality": "validated", "scope": "fixture"]
        let formatter = ISO8601DateFormatter()
        if let observed {
            metrics.append(["id": MetricID.codexUsedPercent, "status": available ? "available" : "unknown",
                            "value": 51, "observed_at": formatter.string(from: observed)].merging(common) { value, _ in value })
        } else if !available {
            metrics.append(["id": MetricID.codexUsedPercent, "status": "unknown", "value": NSNull()].merging(common) { value, _ in value })
        }
        if let reset {
            metrics.append(["id": MetricID.codexResetsAt, "status": "available", "value": reset.timeIntervalSince1970].merging(common) { value, _ in value })
        }
        document["metrics"] = metrics
        return try JSONSerialization.data(withJSONObject: document)
    }

    func testCodexReadingInsideItsWeeklyWindowKeepsTheNormalTick() async throws {
        // Upstream is sampled independently: a newly fetched snapshot can carry
        // a quota observation already 37 minutes old. It stays valid until the
        // weekly reset, so only the ordinary half-hour interval refreshes it.
        let clock = Clock(now: beijing(14))
        let transport = CountingTransport(data: try quotaFixture(
            observed: beijing(13, 23), reset: beijing(18)))
        let store = SnapshotStore(transport: transport, now: { clock.now })
        await store.refresh()
        for minute in [8, 20, 29] {
            clock.now = beijing(14, minute)
            let early = await store.refreshIfDue(now: clock.now)
            XCTAssertFalse(early, "minute \(minute): an in-window reading is not expiring")
        }
        clock.now = beijing(14, 30)
        let interval = await store.refreshIfDue(now: clock.now)
        XCTAssertTrue(interval)
        let count = await transport.fetchCount
        XCTAssertEqual(count, 2)
    }

    func testUnknownCodexRetriesOnExistingTickButRemainsQuietAtNight() async throws {
        let clock = Clock(now: beijing(19, 40))
        let transport = CountingTransport(data: try quotaFixture(observed: nil, reset: nil, available: false))
        let store = SnapshotStore(transport: transport, now: { clock.now })
        await store.refresh()
        clock.now = beijing(19, 44)
        let tooSoon = await store.refreshIfDue(now: clock.now)
        XCTAssertFalse(tooSoon)
        clock.now = beijing(19, 45)
        let retryDue = await store.refreshIfDue(now: clock.now)
        XCTAssertTrue(retryDue)
        clock.now = beijing(20)
        let quiet = await store.refreshIfDue(now: clock.now)
        XCTAssertFalse(quiet)
    }

    private actor BlockingTransport: SnapshotTransport {
        private let data: Data
        private(set) var fetchCount = 0
        private var completion: CheckedContinuation<Data, Never>?
        private var started: CheckedContinuation<Void, Never>?
        init(data: Data) { self.data = data }
        func fetch() async throws -> Data {
            fetchCount += 1
            started?.resume()
            started = nil
            return await withCheckedContinuation { completion = $0 }
        }
        func waitUntilStarted(_ expectedCount: Int = 1) async {
            if fetchCount >= expectedCount { return }
            await withCheckedContinuation { started = $0 }
        }
        func finish() { completion?.resume(returning: data); completion = nil }
    }

    func testQuotaRetryAndManualRefreshCannotOverlapAnExistingFetch() async throws {
        let clock = Clock(now: beijing(14))
        let data = try quotaFixture(observed: nil, reset: nil, available: false)
        let transport = BlockingTransport(data: data)
        let store = SnapshotStore(transport: transport, now: { clock.now })
        let first = Task { await store.refresh() }
        await transport.waitUntilStarted()
        await transport.finish()
        await first.value  // Install unknown quota through the real consumer.
        let second = Task { await store.refresh() }
        await transport.waitUntilStarted(2)
        clock.now = beijing(14, 5)
        let due = await store.refreshIfDue(now: clock.now)
        XCTAssertTrue(due, "reach quota-due refresh before the in-flight guard")
        await store.refresh()
        let count = await transport.fetchCount
        XCTAssertEqual(count, 2)
        await transport.finish()
        await second.value
    }

    func testCodexWeeklyResetAlsoAdvancesTheExistingTick() async throws {
        let clock = Clock(now: beijing(14))
        let transport = CountingTransport(data: try quotaFixture(observed: clock.now, reset: beijing(14, 8)))
        let store = SnapshotStore(transport: transport, now: { clock.now })
        await store.refresh()
        clock.now = beijing(14, 7)
        let nearReset = await store.refreshIfDue(now: clock.now)
        XCTAssertTrue(nearReset)
    }

    func testDueRefreshesFetchAndUndueOnesDoNot() async throws {
        let transport = CountingTransport(data: try quotaFixture(observed: nil, reset: nil))
        let clock = Clock(now: beijing(14, 0))
        let store = SnapshotStore(
            transport: transport,
            now: { clock.now }
        )

        // Cold start: always due.
        var fetched = await store.refreshIfDue(now: clock.now)
        XCTAssertTrue(fetched)
        var count = await transport.fetchCount
        XCTAssertEqual(count, 1)

        // Ten minutes later: not due.
        clock.now = beijing(14, 10)
        fetched = await store.refreshIfDue(now: clock.now)
        XCTAssertFalse(fetched)
        count = await transport.fetchCount
        XCTAssertEqual(count, 1, "an early tick must not cost an SSH call")

        // Half an hour after the last attempt: due again.
        clock.now = beijing(14, 30)
        fetched = await store.refreshIfDue(now: clock.now)
        XCTAssertTrue(fetched)
        count = await transport.fetchCount
        XCTAssertEqual(count, 2)
    }

    func testTheManualButtonIsNeverRateLimitedOrSilenced() async throws {
        let transport = CountingTransport(data: try fixtureData("live-v2a"))
        // 03:00 Beijing — deep inside the quiet window.
        let clock = Clock(now: beijing(3, 0))
        let store = SnapshotStore(transport: transport, now: { clock.now })

        // What the 刷新 button calls, three times in a row.
        await store.refresh()
        await store.refresh()
        await store.refresh()

        let count = await transport.fetchCount
        XCTAssertEqual(count, 3, "an explicit request must always be honoured")
    }

    func testTheBackgroundTickStaysQuietOvernight() async throws {
        let transport = CountingTransport(data: try fixtureData("live-v2a"))
        let clock = Clock(now: beijing(19, 0))
        let store = SnapshotStore(transport: transport, now: { clock.now })

        await store.refreshIfDue(now: clock.now)
        var count = await transport.fetchCount
        XCTAssertEqual(count, 1)

        // Long past due, but it is now night.
        clock.now = beijing(23, 30)
        let fetched = await store.refreshIfDue(now: clock.now)
        XCTAssertFalse(fetched)
        count = await transport.fetchCount
        XCTAssertEqual(count, 1)
    }

    func testUndueBackgroundTickStillExpiresThePresentation() async throws {
        let data = try fixtureData("ok-full")
        let snapshot = try SnapshotDecoder().decode(data)
        let transport = CountingTransport(data: data)
        let clock = Clock(now: snapshot.generatedAt.addingTimeInterval(60))
        let store = SnapshotStore(transport: transport, now: { clock.now })

        await store.refresh()
        XCTAssertEqual(store.displayRuntimeHealth, .ok)

        // Two minutes after the fetch is below the 30-minute fetch interval,
        // but just beyond this snapshot's own three-minute TTL.
        clock.now = snapshot.expiresAt.addingTimeInterval(1)
        let fetched = await store.refreshIfDue(now: clock.now)
        let count = await transport.fetchCount

        XCTAssertFalse(fetched)
        XCTAssertEqual(count, 1)
        XCTAssertEqual(store.displayRuntimeHealth, .stale)
        XCTAssertEqual(store.presentation.tone, .amber)
    }

    // MARK: - Overnight pause must not look like a fault, nor hide one

    /// The morning glance: a successful 19:xx fetch, read at 07:30. Stale by
    /// the clock, but the pause is the owner's own instruction — gray and
    /// "夜间暂停刷新", not an amber 数据已过期 that cries wolf every single day.
    func testOvernightPauseReadsAsPausedNotBroken() async throws {
        let transport = CountingTransport(data: try fixtureData("live-v2a"))
        let clock = Clock(now: beijing(19, 45))
        let store = SnapshotStore(transport: transport, now: { clock.now })

        await store.refresh()

        // Next morning, inside the quiet window, long past any TTL.
        clock.now = beijing(31, 30) // 07:30 the following day
        let fetched = await store.refreshIfDue(now: clock.now)
        XCTAssertFalse(fetched, "the window must still suppress the fetch")

        XCTAssertEqual(store.displayRuntimeHealth, .stale)
        XCTAssertTrue(store.presentation.pausedOvernight)
        XCTAssertEqual(store.presentation.tone, .gray)

        let menu = MenuBarViewModel(
            metrics: SnapshotMetrics(store.snapshot),
            alertTiers: store.snapshot?.alertTiers ?? .zero,
            hasSnapshot: true,
            presentation: store.presentation
        )
        XCTAssertEqual(menu.statusLamp, .gray)
        XCTAssertEqual(menu.statusText, "夜间暂停刷新")
    }

    /// The load-bearing case: night is an excuse for not *asking*, never for a
    /// fetch that failed. A box that stops answering at 02:00 must still be
    /// reported as such in the morning.
    func testATransportFailureAtNightStillAlarms() async throws {
        let cached = try SnapshotDecoder().decode(try fixtureData("expired-cache"))
        let clock = Clock(now: beijing(31, 30)) // 07:30, inside the window
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .failure(ErrorBox(message: "box offline"))),
            cache: MemorySnapshotCache(snapshot: cached),
            now: { clock.now }
        )

        await store.refresh()

        XCTAssertEqual(store.displayRuntimeHealth, .stale)
        XCTAssertFalse(
            store.presentation.pausedOvernight,
            "a failed fetch must never be excused by the quiet window"
        )
        XCTAssertEqual(store.presentation.tone, .amber)

        let menu = MenuBarViewModel(
            metrics: SnapshotMetrics(store.snapshot),
            alertTiers: store.snapshot?.alertTiers ?? .zero,
            hasSnapshot: true,
            presentation: store.presentation
        )
        XCTAssertEqual(menu.statusLamp, .amber)
        XCTAssertEqual(menu.statusText, "数据已过期")
    }

    /// Daytime staleness is real staleness: same data, same store, amber.
    func testDaytimeStalenessStillAlarms() async throws {
        let transport = CountingTransport(data: try fixtureData("live-v2a"))
        let clock = Clock(now: beijing(10, 0))
        let store = SnapshotStore(transport: transport, now: { clock.now })

        await store.refresh()

        // 11:00 — past the 45-minute TTL of a snapshot generated at fetch time,
        // and squarely inside working hours.
        clock.now = beijing(11, 30)
        _ = await store.refreshIfDue(now: clock.now)

        XCTAssertFalse(store.presentation.pausedOvernight)
    }

    /// A mutable clock the store can read through its `now` closure.
    private final class Clock: @unchecked Sendable {
        var now: Date
        init(now: Date) { self.now = now }
    }
}


extension RefreshPolicyTests {
    func testResourceExpiryAdvancesTheExistingDaytimeRefreshWithoutChangingItsTTL() {
        let last = beijing(9,18,14,0)
        let expiry = beijing(9,18,14,25)
        XCTAssertFalse(RefreshPolicy.shouldRefresh(now: beijing(9,18,14,23), lastRefresh: last, resourceExpiresAt: expiry))
        XCTAssertTrue(RefreshPolicy.shouldRefresh(now: beijing(9,18,14,24), lastRefresh: last, resourceExpiresAt: expiry))
        XCTAssertFalse(RefreshPolicy.shouldRefresh(now: beijing(9,18,14,24), lastRefresh: beijing(9,18,14,23), resourceExpiresAt: expiry))
        XCTAssertFalse(RefreshPolicy.shouldRefresh(now: beijing(9,18,21,0), lastRefresh: last, resourceExpiresAt: expiry))
    }
}
