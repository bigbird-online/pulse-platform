import Foundation
import XCTest
@testable import PulseStats

final class SnapshotDecoderTests: XCTestCase {
    private let decoder = SnapshotDecoder()

    func testDecodesEveryRuntimeStateAndCoverageAxis() throws {
        let cases: [(String, RuntimeHealth, Coverage)] = [
            ("ok-full", .ok, .full),
            ("warning-partial", .warning, .partial),
            ("critical-partial", .critical, .partial),
            ("stale-partial", .stale, .partial),
            ("unknown-partial", .unknown, .partial),
        ]
        for (fixture, health, coverage) in cases {
            let snapshot = try decoder.decode(try fixtureData(fixture))
            XCTAssertEqual(snapshot.overall.runtimeHealth, health, fixture)
            XCTAssertEqual(snapshot.overall.coverage, coverage, fixture)
        }
    }

    func testRejectsInvalidJSON() throws {
        XCTAssertThrowsError(try decoder.decode(try fixtureData("invalid"))) { error in
            XCTAssertEqual(error as? SnapshotDecodeError, .invalidJSON)
        }
    }

    func testRejectsUnknownMajorBeforePayloadDecode() throws {
        XCTAssertThrowsError(try decoder.decode(try fixtureData("unknown-major"))) { error in
            XCTAssertEqual(error as? SnapshotDecodeError, .unsupportedSchema("pulse.monitoring.snapshot.v3"))
        }
    }

    // MARK: alert tiers (spec v3 §6)

    /// Every pre-v3 fixture predates `tier` / `alert_tiers`, so this is the
    /// backwards-compatibility guarantee stated as a test: an old snapshot —
    /// including one already sitting in the on-disk cache — still decodes, and
    /// its alerts land on the quiet default rather than failing the payload.
    func testPreV3SnapshotDecodesWithSafeDefaults() throws {
        // `live-v2a` rather than `ok-full`: it carries 17 real alerts, so the
        // per-alert assertion below actually runs on something.
        let snapshot = try decoder.decode(try fixtureData("live-v2a"))
        XCTAssertEqual(snapshot.alertTiers, .zero)
        XCTAssertFalse(snapshot.alerts.isEmpty, "fixture must have alerts for this to mean anything")
        for alert in snapshot.alerts {
            XCTAssertEqual(alert.tier, .watch, "absent tier must fall back to watch, not act")
        }
    }

    /// An unrecognised tier — a future exporter growing a fourth档 — must not
    /// take the whole snapshot down, and must not be guessed into `act`.
    func testUnknownTierFallsBackToWatchRatherThanFailing() throws {
        let payload = try alterFirstAlertTier(in: try fixtureData("live-v2a"), to: "\"catastrophic\"")
        let snapshot = try decoder.decode(payload)
        XCTAssertEqual(snapshot.alerts.first?.tier, .watch)
    }

    /// A tier that *is* recognised must survive verbatim — the fallback must
    /// not be swallowing good values too.
    func testKnownTierIsPreserved() throws {
        let payload = try alterFirstAlertTier(in: try fixtureData("live-v2a"), to: "\"act\"")
        let snapshot = try decoder.decode(payload)
        XCTAssertEqual(snapshot.alerts.first?.tier, .act)
    }

    /// A partially populated `alert_tiers` fills the gaps with 0 instead of
    /// throwing.
    func testPartialAlertTiersDefaultsMissingCountersToZero() throws {
        let json = try JSONSerialization.jsonObject(with: try fixtureData("live-v2a"))
        var root = try XCTUnwrap(json as? [String: Any])
        root["alert_tiers"] = ["act": 3]
        let payload = try JSONSerialization.data(withJSONObject: root)
        let snapshot = try decoder.decode(payload)
        XCTAssertEqual(snapshot.alertTiers, AlertTiers(act: 3, watch: 0, done: 0))
    }

    /// Injects a `tier` onto the first alert of a fixture that has none.
    private func alterFirstAlertTier(in data: Data, to rawJSON: String) throws -> Data {
        let json = try JSONSerialization.jsonObject(with: data)
        var root = try XCTUnwrap(json as? [String: Any])
        var alerts = try XCTUnwrap(root["alerts"] as? [[String: Any]])
        try XCTSkipIf(alerts.isEmpty, "fixture has no alerts to tag")
        let value = try JSONSerialization.jsonObject(
            with: Data("[\(rawJSON)]".utf8),
            options: [.fragmentsAllowed]
        )
        alerts[0]["tier"] = try XCTUnwrap((value as? [Any])?.first)
        root["alerts"] = alerts
        return try JSONSerialization.data(withJSONObject: root)
    }
}

func fixtureData(_ name: String) throws -> Data {
    let bundle = Bundle(for: SnapshotDecoderTests.self)
    let url = try XCTUnwrap(bundle.url(forResource: name, withExtension: "json"))
    return try Data(contentsOf: url)
}
