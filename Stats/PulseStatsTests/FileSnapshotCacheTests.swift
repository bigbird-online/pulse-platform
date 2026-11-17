import Foundation
import XCTest
@testable import PulseStats

@MainActor
final class FileSnapshotCacheTests: XCTestCase {
    func testSaveLoadUses0600AndLeavesNoTemporaryFile() throws {
        let (temporaryDirectory, cacheURL) = makeCacheURL()
        defer { try? FileManager.default.removeItem(at: temporaryDirectory) }
        let snapshot = try SnapshotDecoder().decode(try fixtureData("ok-full"))
        let cache = FileSnapshotCache(url: cacheURL)
        try cache.save(snapshot)

        XCTAssertEqual(try cache.load(), snapshot)
        let attributes = try FileManager.default.attributesOfItem(atPath: cacheURL.path)
        let permissions = try XCTUnwrap(attributes[.posixPermissions] as? NSNumber).intValue
        XCTAssertEqual(permissions & 0o777, 0o600)
        let directoryAttributes = try FileManager.default.attributesOfItem(
            atPath: cacheURL.deletingLastPathComponent().path
        )
        let directoryPermissions = try XCTUnwrap(
            directoryAttributes[.posixPermissions] as? NSNumber
        ).intValue
        XCTAssertEqual(directoryPermissions & 0o777, 0o700)
        let siblings = try FileManager.default.contentsOfDirectory(
            at: cacheURL.deletingLastPathComponent(),
            includingPropertiesForKeys: nil
        )
        XCTAssertEqual(siblings.map(\.lastPathComponent), ["snapshot-v2.json"])
    }

    func testInvalidCacheIsRejectedAndNeverDecodedAsSnapshot() throws {
        let (temporaryDirectory, cacheURL) = makeCacheURL()
        defer { try? FileManager.default.removeItem(at: temporaryDirectory) }
        try FileManager.default.createDirectory(
            at: cacheURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data("not-json".utf8).write(to: cacheURL)
        let cache = FileSnapshotCache(url: cacheURL)
        XCTAssertThrowsError(try cache.load()) { error in
            XCTAssertEqual(error.localizedDescription, "cache_invalid")
        }
    }

    func testClearIsIdempotent() throws {
        let (temporaryDirectory, cacheURL) = makeCacheURL()
        defer { try? FileManager.default.removeItem(at: temporaryDirectory) }
        let cache = FileSnapshotCache(url: cacheURL)
        let snapshot = try SnapshotDecoder().decode(try fixtureData("warning-partial"))
        try cache.save(snapshot)
        try cache.clear()
        try cache.clear()
        XCTAssertNil(try cache.load())
    }

    func testCostTokenFactsSurviveActualCacheSaveAndRestartWithoutInventingZeros() throws {
        let (temporaryDirectory, cacheURL) = makeCacheURL()
        defer { try? FileManager.default.removeItem(at: temporaryDirectory) }
        var object = try JSONSerialization.jsonObject(with: fixtureData("cost-dashboard-v1")) as! [String: Any]
        var cost = object["cost_dashboard"] as! [String: Any]
        var views = cost["views"] as! [[String: Any]]
        let tokens: [String: Any] = [
            "uncached_input": ["known": 12345, "unknown_event_count": 1],
            "cache_read": ["known": 900000, "unknown_event_count": 0],
            "cache_write": ["known": NSNull(), "unknown_event_count": 2],
            "output": ["known": 0, "unknown_event_count": 0],
        ]
        for index in views.indices {
            var summary = views[index]["summary"] as! [String: Any]
            var usage = summary["usage"] as! [String: Any]
            usage["tokens"] = tokens
            summary["usage"] = usage
            views[index]["summary"] = summary
            var groups = views[index]["groups"] as! [[String: Any]]
            if groups.isEmpty {
                groups = [["key": [views[index]["dimension"] as! String: "unassigned"], "cost": summary["cost"]!]]
            }
            for groupIndex in groups.indices {
                groups[groupIndex]["usage"] = ["tokens": tokens, "event_count": 2, "known_total_tokens": 912345]
            }
            views[index]["groups"] = groups
        }
        cost["views"] = views
        object["cost_dashboard"] = cost
        let snapshot = try SnapshotDecoder().decode(JSONSerialization.data(withJSONObject: object))
        try FileSnapshotCache(url: cacheURL).save(snapshot)
        let reloaded = try XCTUnwrap(FileSnapshotCache(url: cacheURL).load())
        let decodedViews = try XCTUnwrap(reloaded.costDashboard?.views)
        XCTAssertEqual(decodedViews.count, 9)
        for view in decodedViews {
            XCTAssertEqual(view.summary.usage.tokens?["uncached_input"]?.known, 12345)
            XCTAssertEqual(view.summary.usage.tokens?["uncached_input"]?.unknownEventCount, 1)
            XCTAssertNil(view.summary.usage.tokens?["cache_write"]?.known)
            XCTAssertEqual(view.summary.usage.tokens?["cache_write"]?.unknownEventCount, 2)
            XCTAssertEqual(view.summary.usage.tokens?["output"]?.known, 0)
            for group in view.groups {
                XCTAssertEqual(group.usage?.tokens, view.summary.usage.tokens)
                XCTAssertEqual(group.usage?.eventCount, 2)
                XCTAssertEqual(group.usage?.knownTotalTokens, 912345)
            }
        }
        let cached = try JSONSerialization.jsonObject(with: Data(contentsOf: cacheURL)) as! [String: Any]
        let cachedViews = (cached["cost_dashboard"] as! [String: Any])["views"] as! [[String: Any]]
        for view in cachedViews {
            let summary = view["summary"] as! [String: Any]
            let usage = summary["usage"] as! [String: Any]
            XCTAssertEqual(usage["tokens"] as? NSDictionary, tokens as NSDictionary)
            for group in view["groups"] as! [[String: Any]] {
                XCTAssertEqual((group["usage"] as? [String: Any])?["tokens"] as? NSDictionary, tokens as NSDictionary)
            }
        }
        XCTAssertEqual(reloaded.incidents, snapshot.incidents)
        XCTAssertEqual(reloaded.metrics, snapshot.metrics)
    }

    func testLegacyCostWithoutTokenBreakdownRemainsAbsentAfterCacheRoundtrip() throws {
        let (temporaryDirectory, cacheURL) = makeCacheURL()
        defer { try? FileManager.default.removeItem(at: temporaryDirectory) }
        var object = try JSONSerialization.jsonObject(with: fixtureData("cost-dashboard-v1")) as! [String: Any]
        var cost = object["cost_dashboard"] as! [String: Any]
        var views = cost["views"] as! [[String: Any]]
        for index in views.indices {
            var summary = views[index]["summary"] as! [String: Any]
            var usage = summary["usage"] as! [String: Any]
            usage.removeValue(forKey: "tokens")
            summary["usage"] = usage
            views[index]["summary"] = summary
            var groups = views[index]["groups"] as! [[String: Any]]
            for groupIndex in groups.indices { groups[groupIndex].removeValue(forKey: "usage") }
            views[index]["groups"] = groups
        }
        cost["views"] = views; object["cost_dashboard"] = cost
        let snapshot = try SnapshotDecoder().decode(JSONSerialization.data(withJSONObject: object))
        try FileSnapshotCache(url: cacheURL).save(snapshot)
        let reloaded = try XCTUnwrap(FileSnapshotCache(url: cacheURL).load())
        for view in try XCTUnwrap(reloaded.costDashboard?.views) {
            XCTAssertNil(view.summary.usage.tokens)
            XCTAssertTrue(view.groups.allSatisfy { $0.usage == nil })
        }
    }

    func testTransportPreservesValidatedResponseBytesIncludingAdditiveFields() async throws {
        let (temporaryDirectory, cacheURL) = makeCacheURL()
        defer { try? FileManager.default.removeItem(at: temporaryDirectory) }
        var object = try JSONSerialization.jsonObject(with: fixtureData("cost-dashboard-v1")) as! [String: Any]
        var cost = object["cost_dashboard"] as! [String: Any]
        cost["generated_at"] = "2026-09-15T01:32:41.394079+00:00"
        cost["expires_at"] = "2026-09-15T02:17:41.394079+00:00"
        cost["additive_source_evidence"] = ["missing": NSNull(), "identity": "opaque-test-only"]
        object["cost_dashboard"] = cost
        let payload = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        let decoded = try SnapshotDecoder().decode(payload)
        let store = SnapshotStore(transport: CacheSourceTransport(payload: payload), cache: FileSnapshotCache(url: cacheURL))
        await store.refresh()
        XCTAssertEqual(store.snapshot, decoded)
        XCTAssertEqual(try Data(contentsOf: cacheURL), payload)
        XCTAssertEqual(try FileSnapshotCache(url: cacheURL).load(), decoded)
        let attributes = try FileManager.default.attributesOfItem(atPath: cacheURL.path)
        XCTAssertEqual((attributes[.posixPermissions] as! NSNumber).intValue & 0o777, 0o600)
    }

    func testInvalidOrMismatchedSourceNeverReplacesLastGoodCache() throws {
        let (temporaryDirectory, cacheURL) = makeCacheURL()
        defer { try? FileManager.default.removeItem(at: temporaryDirectory) }
        let payload = try fixtureData("ok-full")
        let snapshot = try SnapshotDecoder().decode(payload)
        let cache = FileSnapshotCache(url: cacheURL)
        try cache.save(snapshot, source: payload)
        XCTAssertThrowsError(try cache.save(snapshot, source: Data("broken".utf8)))
        XCTAssertThrowsError(try cache.save(snapshot, source: fixtureData("warning-partial")))
        XCTAssertThrowsError(try cache.save(snapshot, source: Data(repeating: 32, count: 1_048_577)))
        XCTAssertEqual(try Data(contentsOf: cacheURL), payload)
        XCTAssertEqual(try cache.load(), snapshot)
    }

    private func makeCacheURL() -> (URL, URL) {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("pulse-stats-cache-test-\(UUID().uuidString)", isDirectory: true)
        let cache = directory
            .appendingPathComponent("PulseStats", isDirectory: true)
            .appendingPathComponent("snapshot-v2.json")
        return (directory, cache)
    }
}

private struct CacheSourceTransport: SnapshotTransport {
    let payload: Data
    func fetch() async throws -> Data { payload }
}
