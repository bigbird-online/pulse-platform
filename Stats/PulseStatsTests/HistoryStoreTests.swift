import Foundation
import XCTest
@testable import PulseStats

final class HistoryStoreTests: XCTestCase {
    // MARK: - spec §5: permissions mirror the snapshot cache (0600 / 0700)

    func testAppendUses0600FileIn0700DirectoryAndLeavesNoTemporaryFile() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)

        try await store.append(makeSample(at: Date(timeIntervalSince1970: 1_700_000_000)))

        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        let permissions = try XCTUnwrap(attributes[.posixPermissions] as? NSNumber).intValue
        XCTAssertEqual(permissions & 0o777, 0o600)

        let directoryAttributes = try FileManager.default.attributesOfItem(
            atPath: url.deletingLastPathComponent().path
        )
        let directoryPermissions = try XCTUnwrap(
            directoryAttributes[.posixPermissions] as? NSNumber
        ).intValue
        XCTAssertEqual(directoryPermissions & 0o777, 0o700)

        // No stray temp file survives a successful write.
        let siblings = try FileManager.default.contentsOfDirectory(
            at: url.deletingLastPathComponent(),
            includingPropertiesForKeys: nil
        )
        XCTAssertEqual(siblings.map(\.lastPathComponent), ["history-v1.jsonl"])
    }

    func testPermissionsStay0600AcrossRepeatedAppends() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        let base = Date(timeIntervalSince1970: 1_700_000_000)

        for offset in 0..<5 {
            try await store.append(makeSample(at: base.addingTimeInterval(Double(offset))))
        }

        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        let permissions = try XCTUnwrap(attributes[.posixPermissions] as? NSNumber).intValue
        XCTAssertEqual(permissions & 0o777, 0o600)
        let rows = await store.load(now: base.addingTimeInterval(10))
        XCTAssertEqual(rows.count, 5)
    }

    // MARK: - spec §7: 历史文件损坏 → 丢弃重建，当次刷新成功

    func testCorruptFileIsDiscardedAndRebuiltWithoutFailingTheAppend() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data("{ this is not json\nnor is this\n\u{0}\u{1}binary garbage\n".utf8).write(to: url)

        let store = HistoryStore(url: url)
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        try await store.append(makeSample(at: now, outputTokens: 4242))

        let rows = await store.load(now: now)
        XCTAssertEqual(rows.count, 1, "corrupt rows are dropped, the new row survives")
        XCTAssertEqual(rows.first?.outputTokens, 4242)
        let discarded = await store.discardedRowCount()
        XCTAssertEqual(discarded, 0, "the rebuilt file has no unparseable rows left")
    }

    func testPartiallyCorruptFileKeepsTheGoodRows() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        let base = Date(timeIntervalSince1970: 1_700_000_000)
        try await store.append(makeSample(at: base, outputTokens: 1))

        // Simulate a torn tail: a good row followed by a truncated one.
        let existing = try Data(contentsOf: url)
        var damaged = existing
        damaged.append(Data("{\"observed_at\":\"2026-07-25T00:00".utf8))
        try damaged.write(to: url)

        try await store.append(makeSample(at: base.addingTimeInterval(60), outputTokens: 2))

        let rows = await store.load(now: base.addingTimeInterval(120))
        XCTAssertEqual(rows.map(\.outputTokens), [1, 2], "the good row survived, the torn one was dropped")
    }

    func testUnknownRowVersionIsSkippedRatherThanFailing() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let future = #"{"version":99,"observed_at":"2026-07-25T00:00:00.000Z","output_tokens":7}"#
        try Data((future + "\n").utf8).write(to: url)

        let store = HistoryStore(url: url)
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        try await store.append(makeSample(at: now, outputTokens: 8))
        let rows = await store.load(now: now)
        XCTAssertEqual(rows.map(\.outputTokens), [8])
    }

    // MARK: - spec §7: 历史超 7 天 → 写入时裁剪，文件不无限增长

    func testRowsOlderThanSevenDaysArePrunedOnWrite() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let day: TimeInterval = 24 * 60 * 60

        // Seed rows spanning 10 days, written oldest-first.
        for age in stride(from: 10.0, through: 1.0, by: -1.0) {
            try await store.append(
                makeSample(at: now.addingTimeInterval(-age * day), outputTokens: Int(age))
            )
        }
        try await store.append(makeSample(at: now, outputTokens: 0))

        let rows = await store.load(now: now)
        let ages = rows.compactMap(\.outputTokens)
        // Retention is a closed 7-day window: strictly-older-than-7-days rows
        // are dropped, the row sitting exactly on the boundary is kept (see
        // testRowExactlyAtTheRetentionBoundaryIsRetained).
        XCTAssertFalse(ages.contains(where: { $0 > 7 }), "rows older than 7 days are gone: \(ages)")
        XCTAssertEqual(ages, [7, 6, 5, 4, 3, 2, 1, 0], "the 7-day window plus today, oldest first")

        // The pruning actually shrinks the file rather than only filtering reads.
        let lineCount = try Data(contentsOf: url)
            .split(separator: 0x0A, omittingEmptySubsequences: true).count
        XCTAssertEqual(lineCount, 8)
    }

    /// 30 days of samples must not accumulate beyond the retention window.
    ///
    /// Sampled every 6 hours rather than hourly: each append rewrites the whole
    /// file, so this is O(n²) in the number of appends and an hourly loop costs
    /// ~35s of test time for no extra coverage. Steady-state production cost is
    /// bounded — at one refresh per hour the retained file is ~170 rows (~70
    /// KiB), rewritten once per refresh.
    func testRepeatedAppendsDoNotGrowTheFileWithoutBound() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        let step: TimeInterval = 6 * 3600
        let perDay = 4

        for index in 0..<(perDay * 30) {
            try await store.append(makeSample(at: start.addingTimeInterval(Double(index) * step)))
        }

        let lineCount = try Data(contentsOf: url)
            .split(separator: 0x0A, omittingEmptySubsequences: true).count
        XCTAssertLessThanOrEqual(lineCount, perDay * 7 + 1, "retained at most 7 days of rows")
        XCTAssertGreaterThan(lineCount, perDay * 6, "did not over-prune")
    }

    func testRowExactlyAtTheRetentionBoundaryIsRetained() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let boundary = now.addingTimeInterval(-HistoryStore.retention)

        try await store.append(makeSample(at: boundary, outputTokens: 111))
        try await store.append(makeSample(at: now, outputTokens: 222))

        let rows = await store.load(now: now)
        XCTAssertEqual(rows.map(\.outputTokens), [111, 222])
    }

    // MARK: - spec §5: 并发写不交错

    func testConcurrentAppendsProduceOneWellFormedRowEachWithoutTearing() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        let base = Date(timeIntervalSince1970: 1_700_000_000)
        let writers = 40

        await withTaskGroup(of: Void.self) { group in
            for index in 0..<writers {
                group.addTask {
                    let sample = Self.sample(
                        at: base.addingTimeInterval(Double(index)),
                        outputTokens: index
                    )
                    await store.appendIgnoringFailure(sample)
                }
            }
        }

        // Every line must be independently decodable — no interleaved bytes.
        let data = try Data(contentsOf: url)
        let lines = data.split(separator: 0x0A, omittingEmptySubsequences: true)
        XCTAssertEqual(lines.count, writers, "each concurrent append contributed exactly one row")
        for line in lines {
            XCTAssertNoThrow(
                try JSONSerialization.jsonObject(with: Data(line)),
                "a torn line would fail to parse: \(String(decoding: line, as: UTF8.self))"
            )
        }
        let rows = await store.load(now: base.addingTimeInterval(1000))
        XCTAssertEqual(Set(rows.compactMap(\.outputTokens)), Set(0..<writers))
        XCTAssertTrue(data.last == 0x0A, "file ends on a newline, never mid-row")
    }

    // MARK: - spec §5: 写入失败绝不能让刷新失败

    func testAppendIgnoringFailureReportsFalseWhenThePathIsUnwritable() async throws {
        let (root, _) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        // Point the store at a path whose parent is a *file*, so the directory
        // can never be created and every write must fail.
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let blocker = root.appendingPathComponent("PulseStats")
        try Data("not a directory".utf8).write(to: blocker)
        let url = blocker.appendingPathComponent("history-v1.jsonl")

        let store = HistoryStore(url: url)
        let persisted = await store.appendIgnoringFailure(makeSample(at: Date()))
        XCTAssertFalse(persisted, "the write failed")

        // The throwing variant surfaces the same failure explicitly.
        do {
            try await store.append(makeSample(at: Date()))
            XCTFail("expected the write to throw")
        } catch {
            XCTAssertEqual(error as? HistoryStoreError, .writeFailed)
        }
    }

    func testUnwritableHistoryLeavesNoPartialFileBehind() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        let base = Date(timeIntervalSince1970: 1_700_000_000)
        try await store.append(makeSample(at: base, outputTokens: 1))
        let before = try Data(contentsOf: url)

        // Make the directory read-only so the temp file cannot be created.
        let directory = url.deletingLastPathComponent()
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: directory.path)
        let persisted = await store.appendIgnoringFailure(
            makeSample(at: base.addingTimeInterval(60), outputTokens: 2)
        )
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)

        XCTAssertFalse(persisted)
        let after = try Data(contentsOf: url)
        XCTAssertEqual(before, after, "the previous good file is untouched by a failed write")
        let siblings = try FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil
        )
        XCTAssertEqual(siblings.map(\.lastPathComponent), ["history-v1.jsonl"], "no temp file leaked")
    }

    // MARK: - spec §6: 历史文件不得含 session 明细

    func testEncodedRowCarriesNoSessionOrIdentityDetail() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        try await store.append(
            makeSample(at: Date(timeIntervalSince1970: 1_700_000_000), outputTokens: 465_814)
        )

        let text = String(decoding: try Data(contentsOf: url), as: UTF8.self)
        let json = try XCTUnwrap(
            try JSONSerialization.jsonObject(
                with: Data(text.split(separator: "\n").first!.utf8)
            ) as? [String: Any]
        )
        // Only aggregate keys — nothing that can hold an id, name or title.
        let forbidden = ["session", "sessions", "agent", "agents", "who", "open_id",
                         "title", "prompt", "text", "by_agent", "by_session", "stalled"]
        for key in json.keys {
            XCTAssertFalse(
                forbidden.contains(key),
                "history row must not carry \(key)"
            )
        }
        XCTAssertTrue(json.keys.contains("output_tokens"))
        XCTAssertTrue(json.keys.contains("observed_at"))
        // Counts are allowed; the identities behind them are not.
        XCTAssertTrue(json.keys.contains("agent_count"))
        XCTAssertTrue(json.keys.contains("session_count"))
    }

    // MARK: - round trip

    func testRoundTripPreservesEveryAggregateField() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let sample = HistorySample(
            observedAt: now,
            inputTokens: 7118,
            outputTokens: 465_814,
            turns: 73,
            failures: 4,
            inbound: 87,
            durationP50Ms: 93_387,
            ccProcesses: 3,
            ccBusy: 2,
            botsOnline: 15,
            ccRateLimited: false,
            codexUsedPercent: 98.0,
            wiWeekCreated: 58,
            wiDayCreated: 3,
            wiDayCompleted: 2,
            wiStalledCount: 6,
            handoffPending: 5,
            deliverySent: 178,
            deliveryTotal: 178,
            agentCount: 7,
            sessionCount: 11
        )
        try await store.append(sample)
        let rows = await store.load(now: now)
        XCTAssertEqual(rows, [sample])
    }

    func testMissingFileYieldsEmptyHistoryRatherThanAnError() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = HistoryStore(url: url)
        let rows = await store.load(now: Date())
        XCTAssertTrue(rows.isEmpty)
    }

    // MARK: - Helpers

    private func makeHistoryURL() -> (URL, URL) {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("pulse-stats-history-test-\(UUID().uuidString)", isDirectory: true)
        let url = root
            .appendingPathComponent("PulseStats", isDirectory: true)
            .appendingPathComponent("history-v1.jsonl", isDirectory: false)
        return (root, url)
    }

    private func makeSample(at date: Date, outputTokens: Int? = 1000) -> HistorySample {
        Self.sample(at: date, outputTokens: outputTokens)
    }

    fileprivate static func sample(at date: Date, outputTokens: Int? = 1000) -> HistorySample {
        HistorySample(
            observedAt: date,
            inputTokens: 100,
            outputTokens: outputTokens,
            turns: 5,
            failures: 0,
            inbound: 7,
            durationP50Ms: 1234,
            ccProcesses: 3,
            ccBusy: 1,
            botsOnline: 15,
            ccRateLimited: false,
            codexUsedPercent: 98.0,
            wiWeekCreated: 58,
            wiDayCreated: 3,
            wiDayCompleted: 2,
            wiStalledCount: 6,
            handoffPending: 5,
            deliverySent: 178,
            deliveryTotal: 178,
            agentCount: 7,
            sessionCount: 11
        )
    }
}

// MARK: - Wiring: history must never break a refresh (spec §5)

@MainActor
final class SnapshotStoreHistoryWiringTests: XCTestCase {
    func testSuccessfulRefreshAppendsExactlyOneHistoryRow() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let history = HistoryStore(url: url)
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("ok-full"))),
            now: { Date(timeIntervalSince1970: 1_700_000_000) },
            history: history,
            historySample: { snapshot in HistorySample(observedAt: snapshot.generatedAt, outputTokens: 42) }
        )

        await store.refresh()

        let rows = await history.load(now: Date(timeIntervalSince1970: 1_700_000_000))
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.outputTokens, 42)
    }

    func testHistoryWriteFailureLeavesTheRefreshFullyLive() async throws {
        let (root, _) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        // Unwritable path: parent is a regular file.
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let blocker = root.appendingPathComponent("PulseStats")
        try Data("not a directory".utf8).write(to: blocker)
        let history = HistoryStore(url: blocker.appendingPathComponent("history-v1.jsonl"))

        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("ok-full"))),
            now: { Date(timeIntervalSince1970: 1_700_000_000) },
            history: history,
            historySample: { snapshot in HistorySample(observedAt: snapshot.generatedAt, outputTokens: 42) }
        )

        await store.refresh()

        // The refresh is indistinguishable from one with no history at all.
        let generatedAt = try XCTUnwrap(store.snapshot?.generatedAt)
        XCTAssertEqual(store.transportState, .live(generatedAt: generatedAt, cacheError: nil))
        XCTAssertEqual(store.displayRuntimeHealth, .ok)
        XCTAssertNotNil(store.snapshot)
    }

    func testHistorySampleReturningNilIsSimplySkipped() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let history = HistoryStore(url: url)
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("ok-full"))),
            now: { Date(timeIntervalSince1970: 1_700_000_000) },
            history: history,
            historySample: { _ in nil }
        )

        await store.refresh()

        let generatedAt = try XCTUnwrap(store.snapshot?.generatedAt)
        XCTAssertEqual(store.transportState, .live(generatedAt: generatedAt, cacheError: nil))
        let rows = await history.load(now: Date(timeIntervalSince1970: 1_700_000_000))
        XCTAssertTrue(rows.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path), "no file created for a skipped sample")
    }

    func testFailedRefreshWritesNoHistoryRow() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let history = HistoryStore(url: url)
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .failure(ErrorBox(message: "fixture offline"))),
            now: { Date(timeIntervalSince1970: 1_700_000_000) },
            history: history,
            historySample: { snapshot in HistorySample(observedAt: snapshot.generatedAt, outputTokens: 42) }
        )

        await store.refresh()

        let rows = await history.load(now: Date(timeIntervalSince1970: 1_700_000_000))
        XCTAssertTrue(rows.isEmpty, "history only records successful refreshes")
    }

    func testCacheWriteFailureStillRecordsHistory() async throws {
        let (root, url) = makeHistoryURL()
        defer { try? FileManager.default.removeItem(at: root) }
        let history = HistoryStore(url: url)
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("ok-full"))),
            cache: AlwaysFailingCache(),
            now: { Date(timeIntervalSince1970: 1_700_000_000) },
            history: history,
            historySample: { snapshot in HistorySample(observedAt: snapshot.generatedAt, outputTokens: 42) }
        )

        await store.refresh()

        let generatedAt = try XCTUnwrap(store.snapshot?.generatedAt)
        XCTAssertEqual(
            store.transportState,
            .live(generatedAt: generatedAt, cacheError: "cache_write_failed")
        )
        let rows = await history.load(now: Date(timeIntervalSince1970: 1_700_000_000))
        XCTAssertEqual(rows.count, 1, "a cache failure does not suppress history")
    }

    private func makeHistoryURL() -> (URL, URL) {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("pulse-stats-history-wiring-\(UUID().uuidString)", isDirectory: true)
        let url = root
            .appendingPathComponent("PulseStats", isDirectory: true)
            .appendingPathComponent("history-v1.jsonl", isDirectory: false)
        return (root, url)
    }
}

// MARK: - Mapping: PulseSnapshot → HistorySample (the closure the App wires in)

/// The half that `HistoryStoreTests` could never reach.
///
/// Until 2026-08-01 `HistoryStore` had 19 green tests and `PulseStatsApp`
/// passed neither `history` nor `historySample`, so production wrote nothing.
/// Those tests proved the file format; they could not prove anything was ever
/// handed to it. These do the other half: that a real production snapshot
/// yields a populated row, and that a gap stays a gap.
final class HistorySampleMappingTests: XCTestCase {
    private func liveSnapshot() throws -> PulseSnapshot {
        try SnapshotDecoder().decode(try fixtureData("live-v3-triage"))
    }

    /// The headline: a real capture must produce a row with real numbers in it.
    func testRealProductionSnapshotProducesANonEmptySample() throws {
        let snapshot = try liveSnapshot()
        let sample = try XCTUnwrap(
            HistorySample(snapshot: snapshot),
            "a production snapshot must map to a history row"
        )

        XCTAssertEqual(sample.observedAt, snapshot.generatedAt)
        XCTAssertEqual(sample.version, HistorySample.currentVersion)

        // Asserted against the snapshot's own metrics rather than pinned
        // literals: these figures move with every recapture, but the mapping
        // must always agree with the source it read (same discipline as
        // LiveTriageContractTests).
        let metrics = SnapshotMetrics(snapshot)
        XCTAssertEqual(sample.inputTokens, metrics.int(MetricID.inputTokens))
        XCTAssertEqual(sample.outputTokens, metrics.int(MetricID.outputTokens))
        XCTAssertEqual(sample.turns, metrics.int(MetricID.turns))
        XCTAssertEqual(sample.failures, metrics.int(MetricID.failures))
        XCTAssertEqual(sample.inbound, metrics.int(MetricID.inbound))
        XCTAssertEqual(sample.durationP50Ms, metrics.int(MetricID.durationP50))
        XCTAssertEqual(sample.ccProcesses, metrics.int(MetricID.ccProcesses))
        XCTAssertEqual(sample.ccBusy, metrics.int(MetricID.ccBusy))
        XCTAssertEqual(sample.botsOnline, metrics.int(MetricID.botsOnline))
        XCTAssertEqual(sample.ccRateLimited, metrics.bool(MetricID.ccRateLimited))
        XCTAssertEqual(sample.codexUsedPercent, metrics.double(MetricID.codexUsedPercent))
        XCTAssertEqual(sample.wiWeekCreated, metrics.int(MetricID.wiWeekCreated))
        XCTAssertEqual(sample.wiDayCreated, metrics.int(MetricID.wiDayCreated))
        XCTAssertEqual(sample.wiDayCompleted, metrics.int(MetricID.wiDayCompleted))
        XCTAssertEqual(sample.wiStalledCount, metrics.stalledWorkItems?.count)
        XCTAssertEqual(sample.handoffPending, metrics.int(MetricID.handoffPending))
        XCTAssertEqual(sample.deliverySent, metrics.int(MetricID.deliverySent))
        XCTAssertEqual(sample.agentCount, metrics.agentUsage?.count)
        XCTAssertEqual(sample.sessionCount, metrics.sessionUsage?.count)

        // Not merely non-nil: a row of twenty nulls would satisfy "produced a
        // sample" while recording nothing worth keeping.
        XCTAssertNotNil(sample.outputTokens)
        XCTAssertNotNil(sample.ccProcesses)
        XCTAssertNotNil(sample.handoffPending)
        XCTAssertNotNil(sample.agentCount)
    }

    /// `delivery_total` has no metric of its own — the exporter carries it as
    /// `sample_count` on `delivery.today.sent`. If that ever moves, this fails
    /// here rather than quietly writing nulls for a week.
    func testDeliveryTotalComesFromTheSentMetricSampleCount() throws {
        let snapshot = try liveSnapshot()
        let sample = try XCTUnwrap(HistorySample(snapshot: snapshot))
        let metrics = SnapshotMetrics(snapshot)

        let sampleCount = try XCTUnwrap(
            metrics.rawMetric(MetricID.deliverySent)?.sampleCount,
            "the production capture carries the delivery denominator"
        )
        XCTAssertEqual(sample.deliveryTotal, sampleCount)
    }

    /// 缺数不读 0：a metric that is absent must land as `nil`, never as zero.
    /// A zero here would draw a trough on the retained week that never happened.
    func testMissingMetricsMapToNilNeverZero() throws {
        // One lone available metric: everything else is genuinely absent.
        let snapshot = try SnapshotDecoder().decode(try fixtureData("ok-full"))
        let sample = try XCTUnwrap(
            HistorySample(snapshot: snapshot),
            "one usable metric is still a snapshot worth recording"
        )

        XCTAssertNil(sample.outputTokens)
        XCTAssertNil(sample.inputTokens)
        XCTAssertNil(sample.turns)
        XCTAssertNil(sample.ccProcesses)
        XCTAssertNil(sample.ccBusy)
        XCTAssertNil(sample.botsOnline)
        XCTAssertNil(sample.ccRateLimited)
        XCTAssertNil(sample.codexUsedPercent)
        XCTAssertNil(sample.wiDayCreated)
        XCTAssertNil(sample.wiStalledCount)
        XCTAssertNil(sample.handoffPending)
        XCTAssertNil(sample.deliverySent)
        XCTAssertNil(sample.deliveryTotal)
        XCTAssertNil(sample.agentCount)
        XCTAssertNil(sample.sessionCount)
    }

    /// A snapshot carrying no metrics at all appends nothing, rather than a row
    /// of twenty nulls that would later read as a measured flatline.
    func testSnapshotWithNoMetricsProducesNoSampleAtAll() throws {
        let snapshot = try SnapshotDecoder().decode(try fixtureData("stale-partial"))
        XCTAssertTrue(snapshot.metrics.isEmpty, "fixture premise: no metrics")
        XCTAssertNil(HistorySample(snapshot: snapshot))
    }

    /// End to end through the real `SnapshotStore` seam with the real mapping —
    /// the wiring the App now performs, minus `SSHSnapshotTransport`.
    @MainActor
    func testRefreshWithTheRealMappingPersistsAPopulatedRow() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("pulse-stats-history-mapping-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let url = root
            .appendingPathComponent("PulseStats", isDirectory: true)
            .appendingPathComponent("history-v1.jsonl", isDirectory: false)

        let history = HistoryStore(url: url)
        let store = SnapshotStore(
            transport: DataSnapshotTransport(result: .success(try fixtureData("live-v3-triage"))),
            history: history,
            historySample: { HistorySample(snapshot: $0) }
        )

        await store.refresh()

        let generatedAt = try XCTUnwrap(store.snapshot?.generatedAt)
        let rows = await history.load(now: generatedAt)
        XCTAssertEqual(rows.count, 1, "one successful refresh, one row")
        let row = try XCTUnwrap(rows.first)
        XCTAssertEqual(row.observedAt, generatedAt)
        XCTAssertNotNil(row.outputTokens)
        XCTAssertNotNil(row.ccProcesses)
    }
}

@MainActor
private final class AlwaysFailingCache: SnapshotCache {
    func load() throws -> PulseSnapshot? { nil }
    func save(_ snapshot: PulseSnapshot) throws { throw FileSnapshotCacheError.writeFailed }
    func clear() throws {}
}
