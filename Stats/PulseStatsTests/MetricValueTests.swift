import Foundation
import XCTest
@testable import PulseStats

/// v2-A widened `metrics[].value` past `Double`. These tests pin the two
/// promises that matter: every real shape decodes, and an unrecognised one
/// degrades to unknown instead of rejecting the whole snapshot.
final class MetricValueTests: XCTestCase {
    // MARK: the seven non-scalar shapes

    func testNumberArrayMetricDecodes() throws {
        let metric = try decodeMetric(value: "[0, 23433, 171672]")
        XCTAssertEqual(metric.value.numbers, [0, 23433, 171672])
        XCTAssertNil(metric.value.number, "an array must not read as a scalar")
    }

    func testAgentRowsMetricDecodes() throws {
        let metric = try decodeMetric(
            value: #"[{"agent": "pulse", "output_tokens": 160731, "input_tokens": 6134, "turns": 23, "session_count": 3}]"#
        )
        let rows = try XCTUnwrap(metric.value.rows)
        XCTAssertEqual(rows.count, 1)
        let agent = try XCTUnwrap(AgentUsage(row: rows[0]))
        XCTAssertEqual(agent.agent, "pulse")
        XCTAssertEqual(agent.outputTokens, 160_731)
        XCTAssertEqual(agent.sessionCount, 3)
        XCTAssertTrue(agent.hasMultipleSessions)
    }

    func testSessionRowsMetricDecodes() throws {
        let metric = try decodeMetric(
            value: #"[{"agent": "pulse", "session_prefix": "3c501b48", "output_tokens": 156285, "turns": 17, "last_activity": "2026-07-25T07:16:20.240000+00:00"}]"#
        )
        let rows = try XCTUnwrap(metric.value.rows)
        let session = try XCTUnwrap(SessionUsage(row: rows[0]))
        XCTAssertEqual(session.sessionPrefix, "3c501b48")
        XCTAssertEqual(session.outputTokens, 156_285)
        XCTAssertNotNil(session.lastActivity)
    }

    func testStalledRowsMetricDecodesUsingWorkItemIDField() throws {
        let metric = try decodeMetric(
            value: #"[{"days": 12, "status": "in_progress", "title": "补齐 Amanda", "work_item_id": "wi_20260713_043"}]"#
        )
        let rows = try XCTUnwrap(metric.value.rows)
        let item = try XCTUnwrap(StalledWorkItem(row: rows[0]))
        // Spec §3.3: the id field is `work_item_id`, not `id`.
        XCTAssertEqual(item.workItemID, "wi_20260713_043")
        XCTAssertEqual(item.days, 12)
    }

    func testAttemptsHistogramMapDecodes() throws {
        let metric = try decodeMetric(value: #"{"1": 179}"#)
        XCTAssertEqual(metric.value.counts, ["1": 179])
    }

    func testInboundTypesMapDecodes() throws {
        let metric = try decodeMetric(value: #"{"post": 2, "text": 85}"#)
        XCTAssertEqual(metric.value.counts, ["post": 2, "text": 85])
    }

    func testBooleanMetricDecodesAsBooleanNotOneOrZero() throws {
        let truthy = try decodeMetric(value: "true")
        XCTAssertEqual(truthy.value.boolean, true)
        XCTAssertNil(truthy.value.number, "a bool must never read as 1.0")

        let falsy = try decodeMetric(value: "false")
        XCTAssertEqual(falsy.value.boolean, false)
        XCTAssertNil(falsy.value.number, "false must never read as 0.0")
    }

    // MARK: scalars still work

    func testScalarMetricKeepsConvenienceNumberAccessor() throws {
        let metric = try decodeMetric(value: "98.0")
        XCTAssertEqual(metric.value.number, 98.0)
        XCTAssertNil(metric.value.boolean)
    }

    func testNullValueIsAbsentNotZero() throws {
        let metric = try decodeMetric(value: "null")
        XCTAssertNil(metric.value.number)
        XCTAssertTrue(metric.value.isAbsent)
    }

    // MARK: degradation

    func testUnknownShapeDegradesToUnsupportedWithoutThrowing() throws {
        // A nested array of objects-of-arrays is a shape we do not model.
        let metric = try decodeMetric(value: #"[[{"a": [1, 2]}]]"#)
        XCTAssertEqual(metric.value, .unsupported)
        XCTAssertTrue(metric.value.isAbsent)
        XCTAssertNil(metric.value.number, "unsupported must not fall back to a number")
    }

    func testOneUnreadableMetricDoesNotRejectTheWholeSnapshot() throws {
        let payload = snapshotJSON(metricsJSON: """
        {"id": "good", "capability_id": "s1.request_trace_capture", "status": "available",
         "value": 42, "unit": "count", "window": "current", "sample_count": null,
         "quality": "validated", "scope": "x", "observed_at": null, "reason": null},
        {"id": "weird", "capability_id": "s1.request_trace_capture", "status": "available",
         "value": [[{"nested": [1]}]], "unit": "count", "window": "current", "sample_count": null,
         "quality": "validated", "scope": "x", "observed_at": null, "reason": null}
        """)
        let snapshot = try SnapshotDecoder().decode(payload)
        XCTAssertEqual(snapshot.metrics.count, 2)
        let metrics = SnapshotMetrics(snapshot.metrics)
        XCTAssertEqual(metrics.int("good"), 42)
        // The unreadable one is simply not readable — and it took nothing with it.
        XCTAssertNil(metrics.double("weird"))
    }

    func testMissingValueKeyDecodesAsAbsent() throws {
        let payload = snapshotJSON(metricsJSON: """
        {"id": "no-value", "capability_id": "s1.request_trace_capture", "status": "unknown",
         "unit": "count", "window": "current", "quality": "unavailable", "scope": "x"}
        """)
        let snapshot = try SnapshotDecoder().decode(payload)
        XCTAssertTrue(snapshot.metrics[0].value.isAbsent)
    }

    func testDescriptiveQualityDecodesWithoutRejectingSnapshot() throws {
        let payload = snapshotJSON(metricsJSON: """
        {"id": "usage.today.output_tokens", "capability_id": "s1.request_trace_capture",
         "status": "available", "value": 42, "unit": "tokens", "window": "today_cst",
         "sample_count": 1, "quality": "descriptive", "scope": "Beijing calendar day",
         "observed_at": null, "reason": "One malformed row was skipped"}
        """)
        let snapshot = try SnapshotDecoder().decode(payload)
        let metric = try XCTUnwrap(snapshot.metrics.first)
        XCTAssertEqual(metric.status, .available)
        XCTAssertEqual(metric.quality, .descriptive)
        XCTAssertEqual(SnapshotMetrics(snapshot.metrics).int(metric.id), 42)
    }

    func testIllegalPartialMetricQualityStillRejectsSnapshot() throws {
        let payload = snapshotJSON(metricsJSON: """
        {"id": "usage.today.output_tokens", "capability_id": "s1.request_trace_capture",
         "status": "available", "value": 42, "unit": "tokens", "window": "today_cst",
         "sample_count": 1, "quality": "partial", "scope": "Beijing calendar day",
         "observed_at": null, "reason": null}
        """)
        XCTAssertThrowsError(try SnapshotDecoder().decode(payload))
    }

    // MARK: hostile numbers must not crash

    /// `Int(Double)` traps on out-of-range values. A malformed exporter field
    /// must degrade to unknown, not take the menu bar down with it. This was a
    /// real crash found while probing, not a hypothetical.
    func testOutOfRangeNumbersDegradeToUnknownInsteadOfTrapping() throws {
        for literal in ["1e300", "-1e300"] {
            let metric = try decodeMetric(value: literal)
            XCTAssertNotNil(metric.value.number)
            XCTAssertNil(safeInt(metric.value.number), "\(literal) must not convert to Int")
            let metrics = SnapshotMetrics([metric])
            XCTAssertNil(metrics.int("probe"))
        }
    }

    func testHostileNumbersRenderAsUnknownNotGarbage() {
        XCTAssertEqual(StatsFormat.percent(1e300), "未知")
        XCTAssertEqual(StatsFormat.compactCount(nil), "未知")
        XCTAssertEqual(StatsFormat.duration(milliseconds: 1e300), "未知")
        XCTAssertEqual(StatsFormat.duration(milliseconds: .infinity), "未知")
        XCTAssertEqual(StatsFormat.duration(milliseconds: .nan), "未知")
        XCTAssertEqual(StatsFormat.percent(.nan), "未知")
    }

    func testSafeIntBoundaries() {
        XCTAssertNil(safeInt(.nan))
        XCTAssertNil(safeInt(.infinity))
        XCTAssertNil(safeInt(-.infinity))
        XCTAssertNil(safeInt(nil))
        XCTAssertEqual(safeInt(42.0), 42)
        XCTAssertEqual(safeInt(0), 0)
    }

    // MARK: real production payload

    /// The one that counts. `live-v2a.json` is verbatim output of
    /// `pulse-stats-snapshot.py --json` captured on the production box —
    /// constructed data would not prove this.
    func testRealProductionPayloadDecodesCompletely() throws {
        let snapshot = try SnapshotDecoder().decode(try fixtureData("live-v2a"))
        XCTAssertEqual(snapshot.schemaVersion, PulseSnapshot.supportedSchemaVersion)
        XCTAssertEqual(snapshot.metrics.count, 27)

        let metrics = SnapshotMetrics(snapshot.metrics)
        // Every non-scalar shape reads back as its own type.
        XCTAssertEqual(metrics.numbers(MetricID.hourlyOutputTokens)?.count, 24)
        XCTAssertNotNil(metrics.agentUsage)
        XCTAssertNotNil(metrics.sessionUsage)
        XCTAssertNotNil(metrics.stalledWorkItems)
        XCTAssertNotNil(metrics.counts(MetricID.deliveryAttempts))
        XCTAssertNotNil(metrics.counts(MetricID.inboundTypes))
        XCTAssertNotNil(metrics.bool(MetricID.ccRateLimited))
        // And the scalars are still scalars.
        XCTAssertEqual(metrics.double(MetricID.codexUsedPercent), 98.0)
        XCTAssertEqual(metrics.int(MetricID.outputTokens), 477_197)

        // Nothing silently degraded: no metric landed on `.unsupported`.
        let unsupported = snapshot.metrics.filter { $0.value == .unsupported }
        XCTAssertTrue(unsupported.isEmpty, "unsupported shapes: \(unsupported.map(\.id))")
    }

    func testRealPayloadSurvivesACacheRoundTrip() throws {
        let snapshot = try SnapshotDecoder().decode(try fixtureData("live-v2a"))
        let encoded = try JSONEncoder().encode(snapshot)
        let restored = try JSONDecoder().decode(PulseSnapshot.self, from: encoded)
        XCTAssertEqual(restored, snapshot)
    }

    func testGlobalAuthorityProjectionDecodesWithoutChangingSnapshotV2() throws {
        let payload = snapshotJSON(
            metricsJSON: "",
            globalAuthorityJSON: """
            {
              "schema_version": "pulse_stats_global_read_model/v1",
              "status": "validated",
              "scope": "global",
              "authority_revision": "sha256:4a3acd1cb3af2c3bb72b3c74cb861e4d1ab66947e089688206e33c200af7e21b",
              "required_endpoint_count": 4,
              "materialized_endpoint_count": 4,
              "materialization_digest": "sha256:b42272a4d8de947fd1f2448e1404bace265f0e89245cd0048c2f716da44628b3",
              "freshness": "fresh",
              "degraded_reasons": []
            }
            """
        )

        let snapshot = try SnapshotDecoder().decode(payload)

        XCTAssertEqual(snapshot.globalAuthority?.schemaVersion, "pulse_stats_global_read_model/v1")
        XCTAssertEqual(snapshot.globalAuthority?.status, "validated")
        XCTAssertEqual(snapshot.globalAuthority?.scope, "global")
        XCTAssertEqual(snapshot.globalAuthority?.requiredEndpointCount, 4)
        XCTAssertEqual(snapshot.globalAuthority?.materializedEndpointCount, 4)
        XCTAssertEqual(snapshot.globalAuthority?.freshness, "fresh")
    }

    func testOlderSnapshotWithoutGlobalAuthorityRemainsDecodableAsUnknownEvidence() throws {
        let snapshot = try SnapshotDecoder().decode(snapshotJSON(metricsJSON: ""))

        XCTAssertNil(snapshot.globalAuthority)
    }

    // MARK: helpers

    private func decodeMetric(value: String) throws -> Metric {
        let payload = snapshotJSON(metricsJSON: """
        {"id": "probe", "capability_id": "s1.request_trace_capture", "status": "available",
         "value": \(value), "unit": "count", "window": "current", "sample_count": null,
         "quality": "validated", "scope": "probe", "observed_at": null, "reason": null}
        """)
        return try XCTUnwrap(SnapshotDecoder().decode(payload).metrics.first)
    }

    private func snapshotJSON(
        metricsJSON: String,
        globalAuthorityJSON: String? = nil
    ) -> Data {
        let authority = globalAuthorityJSON.map { ",\n          \"global_authority\": \($0)" } ?? ""
        return Data("""
        {
          "schema_version": "pulse.monitoring.snapshot.v2",
          "generated_at": "2026-07-25T07:00:00+00:00",
          "expires_at": "2099-07-25T07:00:00+00:00",
          "baseline_version": "test",
          "overall": {"runtime_health": "ok", "coverage": "full", "open_incident_count": 0, "headline": "t"},
          "capabilities": [],
          "incidents": [],
          "metrics": [\(metricsJSON)],
          "alerts": [],
          "provenance": {"producer": {"name": "t", "version": "1"}, "source_revisions": {}, "sources": []}
          \(authority)
        }
        """.utf8)
    }
}
