import Foundation

/// One appended history row (spec v2 §5).
///
/// Deliberately **flat and self-contained**: it carries only the aggregate
/// values named in spec §2.3 plus a timestamp. It does not reference
/// `PulseSnapshot` / `Metric`, so the history layer stays decoupled from the
/// snapshot decoding types (which v2-C is reworking). Callers assemble a
/// sample from whatever they decoded; this layer only persists it.
///
/// Privacy (spec §6): there is no field here that can hold a session id, a
/// Feishu `open_id`, an agent name, a work-item title, a prompt or any token
/// text. Per-session and per-agent breakdowns are **counts only**. This is
/// enforced by the type itself, not by convention — the struct has no place to
/// put such data even if a caller wanted to.
struct HistorySample: Codable, Equatable, Sendable {
    /// Schema tag for the row. Readers must skip rows whose version they do
    /// not recognise rather than treating the file as corrupt.
    static let currentVersion = 1

    var version: Int = HistorySample.currentVersion

    /// When the refresh that produced this sample was observed.
    let observedAt: Date

    // MARK: Usage aggregates (spec §2.3) — input and output stay separate.
    let inputTokens: Int?
    let outputTokens: Int?
    let turns: Int?
    let failures: Int?
    let inbound: Int?
    let durationP50Ms: Int?

    // MARK: Relay aggregates (spec §2.2)
    let ccProcesses: Int?
    let ccBusy: Int?
    let botsOnline: Int?
    let ccRateLimited: Bool?
    let codexUsedPercent: Double?

    // MARK: harness aggregates (spec §2.5)
    let wiWeekCreated: Int?
    let wiDayCreated: Int?
    let wiDayCompleted: Int?
    let wiStalledCount: Int?
    let handoffPending: Int?

    // MARK: Delivery aggregates (spec §2.2 block 5)
    let deliverySent: Int?
    let deliveryTotal: Int?

    /// Counts only — never the agent names or session ids themselves (§6).
    let agentCount: Int?
    let sessionCount: Int?

    init(
        observedAt: Date,
        inputTokens: Int? = nil,
        outputTokens: Int? = nil,
        turns: Int? = nil,
        failures: Int? = nil,
        inbound: Int? = nil,
        durationP50Ms: Int? = nil,
        ccProcesses: Int? = nil,
        ccBusy: Int? = nil,
        botsOnline: Int? = nil,
        ccRateLimited: Bool? = nil,
        codexUsedPercent: Double? = nil,
        wiWeekCreated: Int? = nil,
        wiDayCreated: Int? = nil,
        wiDayCompleted: Int? = nil,
        wiStalledCount: Int? = nil,
        handoffPending: Int? = nil,
        deliverySent: Int? = nil,
        deliveryTotal: Int? = nil,
        agentCount: Int? = nil,
        sessionCount: Int? = nil
    ) {
        self.version = HistorySample.currentVersion
        self.observedAt = observedAt
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
        self.turns = turns
        self.failures = failures
        self.inbound = inbound
        self.durationP50Ms = durationP50Ms
        self.ccProcesses = ccProcesses
        self.ccBusy = ccBusy
        self.botsOnline = botsOnline
        self.ccRateLimited = ccRateLimited
        self.codexUsedPercent = codexUsedPercent
        self.wiWeekCreated = wiWeekCreated
        self.wiDayCreated = wiDayCreated
        self.wiDayCompleted = wiDayCompleted
        self.wiStalledCount = wiStalledCount
        self.handoffPending = handoffPending
        self.deliverySent = deliverySent
        self.deliveryTotal = deliveryTotal
        self.agentCount = agentCount
        self.sessionCount = sessionCount
    }
}

extension HistorySample {
    /// Build one history row from a freshly decoded snapshot.
    ///
    /// This is the mapping `SnapshotStore.historySample` expects, and the only
    /// reason `PulseStatsApp` can wire history at all. It lives here rather
    /// than in the App so it is reachable from tests without standing up a
    /// `MenuBarExtra`.
    ///
    /// **Missing reads as `nil`, never as 0.** Every accessor below is the
    /// optional-returning `SnapshotMetrics` API, which already yields `nil` for
    /// a metric that is absent, `warming_up`, `unknown` or the wrong shape. A 0
    /// in this file therefore always means "measured, and it was zero" — which
    /// is what makes the retained week worth plotting at all. Defaulting a gap
    /// to 0 would draw a trough that never happened.
    ///
    /// Returns `nil` when the snapshot carries no usable metric at all, so a
    /// decode that produced an empty `metrics` array does not append a row of
    /// twenty nulls.
    init?(snapshot: PulseSnapshot) {
        let metrics = SnapshotMetrics(snapshot)
        guard !metrics.isEmpty else { return nil }
        self.init(
            // The producer's own observation time, not the App's clock: two
            // refreshes of the same snapshot must not read as two moments.
            observedAt: snapshot.generatedAt,
            inputTokens: metrics.int(MetricID.inputTokens),
            outputTokens: metrics.int(MetricID.outputTokens),
            turns: metrics.int(MetricID.turns),
            failures: metrics.int(MetricID.failures),
            inbound: metrics.int(MetricID.inbound),
            // Already milliseconds at the source (`duration_p50_ms`, exported
            // with unit "milliseconds"), so this is a narrowing, not a
            // conversion.
            durationP50Ms: metrics.int(MetricID.durationP50),
            ccProcesses: metrics.int(MetricID.ccProcesses),
            ccBusy: metrics.int(MetricID.ccBusy),
            botsOnline: metrics.int(MetricID.botsOnline),
            ccRateLimited: metrics.bool(MetricID.ccRateLimited),
            codexUsedPercent: metrics.double(MetricID.codexUsedPercent),
            wiWeekCreated: metrics.int(MetricID.wiWeekCreated),
            wiDayCreated: metrics.int(MetricID.wiDayCreated),
            wiDayCompleted: metrics.int(MetricID.wiDayCompleted),
            // The metric is the stalled *list*; history keeps only its size
            // (§6: counts only, never the work-item ids or titles).
            wiStalledCount: metrics.stalledWorkItems?.count,
            handoffPending: metrics.int(MetricID.handoffPending),
            deliverySent: metrics.int(MetricID.deliverySent),
            // There is no `delivery.today.total` metric. The exporter carries
            // the denominator as `sample_count` on `delivery.today.sent`
            // (live_sources.py: `sample_count=usage["delivery_total"]`), so
            // that is where the attempted-delivery count has to come from.
            // `rawMetric` is deliberate: `sample_count` is metadata about the
            // reading, and is still meaningful on a metric whose own value did
            // not come through.
            deliveryTotal: metrics.rawMetric(MetricID.deliverySent)?.sampleCount,
            // Counts only — the leaderboards themselves never reach this file.
            agentCount: metrics.agentUsage?.count,
            sessionCount: metrics.sessionUsage?.count
        )
    }
}

enum HistoryStoreError: Error, Equatable, LocalizedError {
    case applicationSupportUnavailable
    case encodeFailed
    case writeFailed

    var errorDescription: String? {
        switch self {
        case .applicationSupportUnavailable: "history_directory_unavailable"
        case .encodeFailed: "history_encode_failed"
        case .writeFailed: "history_write_failed"
        }
    }
}

/// Appends aggregate samples to `history-v1.jsonl` and prunes rows older than
/// the retention window (spec §5).
///
/// Concurrency: this is an `actor`, so `append` runs to completion for one
/// caller before another begins. Two concurrent refreshes cannot interleave a
/// read-modify-write and cannot produce a torn line.
///
/// Durability: every write goes to a sibling temporary file which is then
/// atomically renamed over the target. A crash mid-write leaves either the old
/// complete file or the new complete file, never a half-written one.
actor HistoryStore {
    static let retention: TimeInterval = 7 * 24 * 60 * 60

    static func defaultURL(fileManager: FileManager = .default) throws -> URL {
        guard let root = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            throw HistoryStoreError.applicationSupportUnavailable
        }
        return root
            .appendingPathComponent("PulseStats", isDirectory: true)
            .appendingPathComponent("history-v1.jsonl", isDirectory: false)
    }

    private let url: URL
    private let fileManager: FileManager
    private let retention: TimeInterval

    /// Number of rows discarded by the most recent `append` because they could
    /// not be parsed. Surfaced for tests and diagnostics; a non-zero value
    /// means the file was partially rebuilt, not that the refresh failed.
    private(set) var lastDiscardedRowCount = 0

    init(
        url: URL,
        fileManager: FileManager = .default,
        retention: TimeInterval = HistoryStore.retention
    ) {
        self.url = url
        self.fileManager = fileManager
        self.retention = retention
    }

    init(fileManager: FileManager = .default, retention: TimeInterval = HistoryStore.retention) throws {
        try self.init(
            url: Self.defaultURL(fileManager: fileManager),
            fileManager: fileManager,
            retention: retention
        )
    }

    /// Append one sample, dropping rows older than the retention window.
    ///
    /// A corrupt file is discarded and rebuilt around the new sample rather
    /// than aborting (spec §5: "文件损坏 → 丢弃重建，不阻断当次刷新").
    /// Throws only when the new row cannot be persisted at all; callers on the
    /// refresh path are expected to swallow that error — see
    /// `appendIgnoringFailure`.
    func append(_ sample: HistorySample) throws {
        var rows = loadRetainedRows(now: sample.observedAt)
        rows.append(sample)

        let encoder = Self.makeEncoder()
        var payload = Data()
        for row in rows {
            guard let line = try? encoder.encode(row) else {
                // A row we just read back but can no longer encode is treated
                // as corrupt and dropped, rather than failing the write.
                lastDiscardedRowCount += 1
                continue
            }
            payload.append(line)
            payload.append(0x0A)
        }
        guard !payload.isEmpty else { throw HistoryStoreError.encodeFailed }
        try writeAtomically(payload)
    }

    /// Refresh-path entry point. History is a nice-to-have, never a reason for
    /// a refresh to fail (spec §5), so every error is swallowed here.
    /// Returns `true` when the sample was persisted.
    @discardableResult
    func appendIgnoringFailure(_ sample: HistorySample) -> Bool {
        do {
            try append(sample)
            return true
        } catch {
            return false
        }
    }

    /// All retained samples, oldest first. Unparseable rows are skipped.
    func load(now: Date = Date()) -> [HistorySample] {
        loadRetainedRows(now: now)
    }

    func discardedRowCount() -> Int { lastDiscardedRowCount }

    // MARK: - Internals

    /// Read the file, drop rows that are unparseable or past the retention
    /// window. Never throws: an unreadable file simply yields no history.
    private func loadRetainedRows(now: Date) -> [HistorySample] {
        lastDiscardedRowCount = 0
        guard fileManager.fileExists(atPath: url.path),
              let data = try? Data(contentsOf: url) else {
            return []
        }
        let cutoff = now.addingTimeInterval(-retention)
        let decoder = Self.makeDecoder()
        var rows: [HistorySample] = []
        for line in data.split(separator: 0x0A, omittingEmptySubsequences: true) {
            guard let row = try? decoder.decode(HistorySample.self, from: Data(line)) else {
                lastDiscardedRowCount += 1
                continue
            }
            guard row.version == HistorySample.currentVersion else {
                lastDiscardedRowCount += 1
                continue
            }
            // Strictly older than the cutoff is dropped; exactly at the cutoff
            // is retained.
            guard row.observedAt >= cutoff else { continue }
            rows.append(row)
        }
        return rows.sorted { $0.observedAt < $1.observedAt }
    }

    /// Write via a sibling temp file + atomic rename, so a reader never sees a
    /// partial line and a crash cannot truncate the existing file.
    private func writeAtomically(_ payload: Data) throws {
        let directory = url.deletingLastPathComponent()
        // Only create-and-chmod when the directory is genuinely absent. Forcing
        // 0700 on every write would silently re-widen a directory the user (or
        // an operator) had deliberately locked down, and would mask a real
        // permission failure by repairing it behind their back.
        if !fileManager.fileExists(atPath: directory.path) {
            do {
                try fileManager.createDirectory(
                    at: directory,
                    withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700]
                )
            } catch {
                throw HistoryStoreError.writeFailed
            }
        }

        let temporary = directory.appendingPathComponent(
            ".history-v1.jsonl.\(UUID().uuidString).tmp",
            isDirectory: false
        )
        do {
            // Create the temp file at 0600 up front so the contents are never
            // briefly world-readable between write and chmod.
            guard fileManager.createFile(
                atPath: temporary.path,
                contents: payload,
                attributes: [.posixPermissions: 0o600]
            ) else {
                throw HistoryStoreError.writeFailed
            }
            try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
            // replaceItemAt falls back to a plain rename when the destination
            // is absent; both paths are atomic within the same directory.
            if fileManager.fileExists(atPath: url.path) {
                _ = try fileManager.replaceItemAt(url, withItemAt: temporary)
            } else {
                try fileManager.moveItem(at: temporary, to: url)
            }
            try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            try? fileManager.removeItem(at: temporary)
            throw HistoryStoreError.writeFailed
        }
    }

    private static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        // Sorted keys keep rows byte-stable so diffs and tests are readable.
        encoder.outputFormatting = [.sortedKeys]
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(Self.makeISO8601Formatter().string(from: date))
        }
        return encoder
    }

    private static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            guard let date = Self.makeISO8601Formatter().date(from: value) else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Invalid ISO-8601 date: \(value)"
                )
            }
            return date
        }
        return decoder
    }

    /// `ISO8601DateFormatter` is a mutable reference type and therefore not
    /// `Sendable`; a shared `static let` trips Swift 6 strict concurrency.
    /// Building one per call keeps each instance confined to its caller.
    private static func makeISO8601Formatter() -> ISO8601DateFormatter {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }
}
