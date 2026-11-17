import AppKit
import SwiftUI
import XCTest
import zlib
@testable import PulseStats

final class CostDashboardTests: XCTestCase {
    private func document(_ edit: ((inout [String: Any]) -> Void)? = nil) throws -> CostDashboard {
        let source = try JSONSerialization.jsonObject(with: fixtureData("cost-dashboard-v1")) as! [String: Any]
        var object = source["cost_dashboard"] as! [String: Any]
        edit?(&object)
        var base = try JSONSerialization.jsonObject(with: fixtureData("ok-full")) as! [String: Any]
        base["cost_dashboard"] = object
        return try XCTUnwrap(SnapshotDecoder().decode(JSONSerialization.data(withJSONObject: base)).costDashboard)
    }

    func testRealPythonQueryCarrierDecodesAndDoesNotMakePartialComplete() throws {
        let doc = try document()
        let view = try XCTUnwrap(doc.view(period: .month, dimension: .agent, at: doc.generatedAt))
        XCTAssertEqual(view.sourceSchema, "computecost_collaboration_query/v1")
        XCTAssertNil(view.summary.cost.machineCashUsd)
        XCTAssertTrue(view.summary.cost.isPartial)
        XCTAssertNotNil(view.summary.cost.amount)
        XCTAssertEqual(Set(doc.views.map(\.watermark)).count, 1)
        XCTAssertEqual(doc.views.count, 9)
        XCTAssertTrue(doc.views.allSatisfy { $0.query.scope == "activity" })
    }

    func testEffectiveUsageAndAttributionDiagnosticsAreNotSnapshotFreshness() throws {
        let doc = try document { object in
            var views = object["views"] as! [[String: Any]]
            for index in views.indices {
                var summary = views[index]["summary"] as! [String: Any]
                var usage = summary["usage"] as! [String: Any]
                usage["last_usage_at"] = "2026-09-12T04:00:00Z"
                usage["unassigned_event_count"] = 7
                usage["unassigned_known_total_tokens"] = 12345
                usage["source_details"] = [[
                    "label": "tcloud.runtime.pi", "kind": "pi", "status": "partial",
                    "reason": "native_files_incomplete", "files_seen": 3, "files_read": 3,
                    "files_invalid": 1, "excluded_directories": 2,
                    "latest_usage_at": "2026-09-12T04:00:00Z",
                    "errors": ["incomplete_final_record": 1], "source_ref": "source-test"
                ]]
                summary["usage"] = usage
                views[index]["summary"] = summary
            }
            object["views"] = views
        }
        let usage = try XCTUnwrap(doc.view(period: .month, dimension: .wi, at: doc.generatedAt)?.summary.usage)
        XCTAssertEqual(usage.unassignedEventCount, 7)
        XCTAssertEqual(usage.unassignedKnownTotalTokens, 12345)
        XCTAssertNotEqual(usage.lastUsageAt, doc.generatedAt)
        let source = try XCTUnwrap(usage.sourceDetails?.first)
        XCTAssertEqual(source.label, "tcloud.runtime.pi")
        XCTAssertTrue(source.hasReadFailure)
        XCTAssertEqual(source.filesInvalid, 1)
    }

    /// Usage-source gaps qualify cost, so the notice and its expandable
    /// detail live in the 「产出与成本」 card, not under the dashboard tiles.
    @MainActor
    func testUsageSourceGapNoticeSitsInTheCostCard() async throws {
        let doc = try document { object in
            var views = object["views"] as! [[String: Any]]
            for index in views.indices {
                var summary = views[index]["summary"] as! [String: Any]
                var usage = summary["usage"] as! [String: Any]
                usage["source_details"] = [[
                    "label": "tcloud.runtime.pi", "kind": "pi", "status": "partial",
                    "reason": "native_files_incomplete", "files_seen": 3, "files_read": 3,
                    "files_invalid": 1, "excluded_directories": 0,
                    "errors": ["incomplete_final_record": 1], "source_ref": "source-test"
                ]]
                summary["usage"] = usage
                views[index]["summary"] = summary
            }
            object["views"] = views
        }
        XCTAssertNotNil(doc.view(period: .today, dimension: .agent, at: doc.generatedAt))
        let host = NSHostingView(rootView: ScrollView {
            CostDashboardView(document: doc, now: doc.generatedAt, tablePreferences: nil).padding(14)
        }.frame(width: 420, height: 1200))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 1200),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host; window.orderFront(nil); defer { window.orderOut(nil) }
        try await Task.sleep(nanoseconds: 200_000_000); host.layoutSubtreeIfNeeded()
        func find(_ view: NSView, _ id: String) -> NSView? {
            if view.identifier?.rawValue == id { return view }
            return view.subviews.compactMap { find($0, id) }.first
        }
        func frame(_ id: String) throws -> NSRect {
            let view = try XCTUnwrap(find(host, id), id)
            return view.convert(view.bounds, to: host)
        }
        let notice = try frame("usage-source-failure-disclosure")
        let cost = try frame("stats-card-cost")
        let dashboard = try frame("stats-card-dashboard")
        XCTAssertTrue(cost.contains(NSPoint(x: notice.midX, y: notice.midY)), "notice \(notice) cost \(cost)")
        XCTAssertFalse(dashboard.intersects(notice), "notice \(notice) dashboard \(dashboard)")
    }

    func testSubscriptionIsUserConfirmedRateAndDayNotCashOrAllocatedCost() throws {
        let doc = try document()
        let subscription = try XCTUnwrap(doc.subscriptions.first { $0.id == "chatgpt_pro_max" })
        XCTAssertEqual(CostText.decimal(subscription.monthlyAmountUsd), Decimal(200))
        XCTAssertEqual(subscription.billingDay, 14)
        XCTAssertEqual(subscription.status, "active_user_confirmed")
        let retired = try XCTUnwrap(doc.subscriptions.first { $0.id == "claude_code" })
        XCTAssertFalse(retired.active)
        XCTAssertNil(retired.monthlyAmountUsd)
        let view = try XCTUnwrap(doc.view(period: .month, dimension: .wi, at: doc.generatedAt))
        XCTAssertNotEqual(view.summary.cost.amount, Decimal(200))
    }

    func testOraclePublicationValidityIsDisplayableAndALongerOneIsNot() throws {
        let generated = try document().generatedAt
        let iso = ISO8601DateFormatter()
        for (minutes, displayable) in [(45.0, true), (75.0, true), (76.0, false)] {
            let doc = try document { $0["expires_at"] = iso.string(from: generated.addingTimeInterval(minutes * 60)) }
            XCTAssertEqual(doc.isDisplayable(at: generated), displayable, "\(minutes) minutes")
        }
    }

    func testExpiredPublicationStaysVisibleWithItsOwnTime() throws {
        let doc = try document()
        let shown = try XCTUnwrap(doc.view(period: .month, dimension: .agent, at: doc.generatedAt))
        XCTAssertNil(doc.notice(at: doc.generatedAt, refresh: nil))
        let later = doc.expiresAt.addingTimeInterval(10 * 60)
        XCTAssertFalse(doc.isFresh(at: later))
        XCTAssertEqual(doc.view(period: .month, dimension: .agent, at: later), shown)
        let notice = try XCTUnwrap(doc.notice(at: later, refresh: nil))
        XCTAssertEqual(notice.headline, "数据截至 " + StatsFormat.clockBeijing(doc.generatedAt) + "（已过期 10 分钟）")
        XCTAssertNil(notice.day)
    }

    func testRetainedNoticeNamesRunningAndFailedRefresh() throws {
        let doc = try document()
        let later = doc.expiresAt.addingTimeInterval(5 * 60)
        let running = CostRefreshStatus(state: "running", startedAt: later.addingTimeInterval(-60), finishedAt: nil, reason: nil)
        XCTAssertTrue(try XCTUnwrap(doc.notice(at: later, refresh: running)).headline.hasSuffix("（已过期 5 分钟｜刷新中）"))
        let failed = CostRefreshStatus(state: "failed", startedAt: doc.generatedAt.addingTimeInterval(60),
                                       finishedAt: later.addingTimeInterval(-60), reason: "production_ledger_unavailable")
        XCTAssertTrue(try XCTUnwrap(doc.notice(at: later, refresh: failed)).headline
            .hasSuffix("（已过期 5 分钟｜上次刷新失败：生产账本暂不可读写（GitHub））"))
        // A failure before the shown publication says nothing about it.
        let older = CostRefreshStatus(state: "failed", startedAt: doc.generatedAt.addingTimeInterval(-600),
                                      finishedAt: doc.generatedAt.addingTimeInterval(-60), reason: "timeout")
        XCTAssertFalse(try XCTUnwrap(doc.notice(at: later, refresh: older)).headline.contains("失败"))
        XCTAssertNil(doc.notice(at: doc.generatedAt, refresh: failed), "current figures need no notice")
        let partial = CostRefreshStatus(state: "completed", startedAt: doc.generatedAt, finishedAt: doc.generatedAt,
            reason: nil, peers: [.init(id: "tcloud", status: "unreachable_or_invalid", reason: "peer_production_ledger_unavailable"),
                                 .init(id: "mac", status: "read", reason: nil)])
        XCTAssertEqual(partial.peerReason(node: "tcloud"), "远端生产账本暂不可读写（GitHub）")
        XCTAssertNil(partial.peerReason(node: "mac"))
        XCTAssertNil(running.peerReason(node: "tcloud"), "only a completed cycle describes its sources")
        // Paused by decision: named in every cycle state, never a read failure.
        let paused: [CostRefreshStatus.Peer] = [.init(id: "tcloud", status: "paused", reason: "paused_by_decision"),
                                                .init(id: "oracle-node-1", status: "read", reason: nil)]
        for state in ["running", "completed", "failed"] {
            let status = CostRefreshStatus(state: state, startedAt: doc.generatedAt, finishedAt: doc.generatedAt,
                                           reason: nil, peers: paused)
            XCTAssertEqual(status.pausedReason(node: "tcloud"), "按决定暂停采集")
            XCTAssertEqual(status.pausedPeers.map(\.id), ["tcloud"])
            XCTAssertNil(status.peerReason(node: "tcloud"))
            XCTAssertNil(status.pausedReason(node: "oracle-node-1"))
        }
    }

    func testFutureUnsupportedAndUnavailableShowNoFigures() throws {
        let doc = try document()
        let early = doc.generatedAt.addingTimeInterval(-1)
        XCTAssertNil(doc.view(period: .month, dimension: .agent, at: early))
        XCTAssertNil(doc.notice(at: early, refresh: nil))
        XCTAssertEqual(doc.unavailableReason(at: early), "成本快照时间异常")
        let futureSchema = try document { $0["schema_version"] = "pulse_stats_cost_dashboard/v99" }
        XCTAssertNil(futureSchema.view(period: .month, dimension: .agent, at: doc.expiresAt.addingTimeInterval(60)))
        XCTAssertEqual(futureSchema.unavailableReason(at: doc.generatedAt), "成本数据格式不兼容")
        // One second past the longest publisher validity (75 minutes).
        let longLived = try document { $0["expires_at"] = "2026-09-13T09:15:01Z" }
        XCTAssertNil(longLived.view(period: .month, dimension: .agent, at: doc.generatedAt))
        let empty = try document { $0["status"] = "unavailable"; $0["reason"] = "query_source_unavailable"; $0["views"] = [] }
        XCTAssertNil(empty.notice(at: doc.expiresAt.addingTimeInterval(60), refresh: nil))
        XCTAssertEqual(empty.unavailableReason(at: doc.expiresAt.addingTimeInterval(60)), "协作成本来源暂不可读")
    }

    @MainActor
    func testExpiredCardRendersRetainedFiguresUnderTheNotice() async throws {
        let doc = try document()
        let later = doc.expiresAt.addingTimeInterval(12 * 60)
        let failed = CostRefreshStatus(state: "failed", startedAt: doc.expiresAt, finishedAt: later.addingTimeInterval(-60),
                                       reason: "production_ledger_unavailable")
        let host = NSHostingView(rootView: ScrollView {
            CostDashboardView(document: doc, now: later, refresh: failed, initialPeriod: .month, tablePreferences: nil).padding(14)
        }.frame(width: 420, height: 1200))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 1200),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host; window.orderFront(nil); defer { window.orderOut(nil) }
        try await Task.sleep(nanoseconds: 200_000_000); host.layoutSubtreeIfNeeded()
        func find(_ view: NSView, _ id: String) -> NSView? {
            if view.identifier?.rawValue == id { return view }
            return view.subviews.compactMap { find($0, id) }.first
        }
        XCTAssertNotNil(find(host, "cost-data-notice"))
        XCTAssertNotNil(find(host, "collaboration-cost-amount"))
        XCTAssertNil(find(host, "collaboration-cost-unavailable"), "expiry never blanks the card")
    }

    func testPreviousDayStaysVisibleAsThatDaysData() throws {
        let doc = try document { value in
            value["generated_at"] = "2026-09-14T15:59:00Z"
            value["expires_at"] = "2026-09-14T16:20:00Z"
        }
        let afterMidnight = ISO8601DateFormatter().date(from: "2026-09-14T16:00:01Z")!
        XCTAssertTrue(doc.isFresh(at: afterMidnight))
        XCTAssertNotNil(doc.view(period: .today, dimension: .agent, at: afterMidnight))
        XCTAssertNil(doc.todayActivityView(at: afterMidnight), "yesterday is never called today")
        let notice = try XCTUnwrap(doc.notice(at: afterMidnight, refresh: nil))
        XCTAssertEqual(notice.day, "以下为 9 月 14 日数据")
        XCTAssertEqual(notice.headline, "数据截至 09-14 23:59")
        let expired = try XCTUnwrap(doc.notice(at: afterMidnight.addingTimeInterval(3600), refresh: nil))
        XCTAssertEqual(expired.text, "以下为 9 月 14 日数据 · 数据截至 09-14 23:59（已过期 40 分钟）")
    }

    @MainActor
    func testStoreKeepsShownFiguresUntilNewerContentReplacesThem() async throws {
        func payload(_ edit: ((inout [String: Any]) -> Void)?) throws -> Data {
            let source = try JSONSerialization.jsonObject(with: fixtureData("cost-dashboard-v1")) as! [String: Any]
            var object = source["cost_dashboard"] as! [String: Any]
            edit?(&object)
            var base = try JSONSerialization.jsonObject(with: fixtureData("ok-full")) as! [String: Any]
            base["cost_dashboard"] = object
            base["cost_refresh"] = ["state": "failed", "started_at": "2026-09-13T08:40:00Z",
                                    "finished_at": "2026-09-13T08:50:00Z", "reason": "production_ledger_unavailable"]
            return try JSONSerialization.data(withJSONObject: base)
        }
        let first = try document()
        let clock = CostReplayClock(first.expiresAt.addingTimeInterval(20 * 60))
        let store = SnapshotStore(transport: CostReplayTransport([
            .success(try payload(nil)),
            .success(try payload { $0["status"] = "unavailable"; $0["reason"] = "invalid_cost_snapshot"; $0["views"] = [] }),
            .success(try payload { $0["schema_version"] = "pulse_stats_cost_dashboard/v99" }),
            .success(try payload { $0["generated_at"] = "2026-09-13T09:10:00Z"; $0["expires_at"] = "2026-09-13T09:55:00Z" }),
        ]), now: { clock.now() })
        await store.refresh()
        XCTAssertEqual(store.costDashboard, first)
        XCTAssertEqual(store.costRefresh?.reason, "production_ledger_unavailable")
        XCTAssertTrue(try XCTUnwrap(store.costDashboard?.notice(at: clock.now(), refresh: store.costRefresh))
            .headline.contains("上次刷新失败"))
        await store.refresh()
        XCTAssertEqual(store.costDashboard, first, "an unavailable publication never blanks shown figures")
        await store.refresh()
        XCTAssertEqual(store.costDashboard, first, "an incompatible carrier never blanks shown figures")
        clock.set(ISO8601DateFormatter().date(from: "2026-09-13T09:15:00Z")!)
        await store.refresh()
        XCTAssertEqual(store.costDashboard?.generatedAt, ISO8601DateFormatter().date(from: "2026-09-13T09:10:00Z"))
        XCTAssertNil(store.costDashboard?.notice(at: clock.now(), refresh: store.costRefresh))
    }

    func testInvalidCashCannotBeRescuedByRowsAndUnknownNeverBecomesZero() throws {
        for value in ["NaN", "-1", "1e9", "200garbage", ""] {
            XCTAssertNil(CostText.decimal(value))
        }
        XCTAssertNil(CostText.decimal(nil))
        XCTAssertEqual(CostText.decimal("0"), Decimal.zero)
        let doc = try document { object in
            var views = object["views"] as! [[String: Any]]
            for index in views.indices {
                var summary = views[index]["summary"] as! [String: Any]
                var cost = summary["cost"] as! [String: Any]
                cost["reasons"] = ["join_conflict"]
                summary["cost"] = cost
                views[index]["summary"] = summary
            }
            object["views"] = views
        }
        XCTAssertNil(doc.view(period: .month, dimension: .agent, at: doc.generatedAt))
    }


    func testAmbiguousWorkItemIsPendingVerificationNotCalledConflict() {
        let cost = QueryMachineCost(machineCashUsd: nil, partialMachineCashUsd: "1.25",
            cashBasis: "machine_standard", reasons: ["attribution_unknown"], unpriced: [])
        let wi = CostQueryGroup(key: ["wi": "ambiguous"], cost: cost)
        XCTAssertEqual(wi.title, "WI 归属待核验")
        XCTAssertEqual(CostTableColumn.wi.text(wi), "待核验")
        let agent = CostQueryGroup(key: ["agent": "ambiguous"], cost: cost)
        XCTAssertEqual(agent.title, "归属冲突")
    }

    func testBrokenOptionalCostDoesNotBreakMainSnapshotDecode() throws {
        var object = try JSONSerialization.jsonObject(with: fixtureData("ok-full")) as! [String: Any]
        object["cost_dashboard"] = ["schema_version": 3, "views": "broken"]
        let changed = try SnapshotDecoder().decode(try JSONSerialization.data(withJSONObject: object))
        let original = try SnapshotDecoder().decode(try fixtureData("ok-full"))
        XCTAssertNil(changed.costDashboard)
        XCTAssertEqual(changed.incidents, original.incidents)
        XCTAssertEqual(changed.alerts, original.alerts)
        XCTAssertEqual(changed.metrics, original.metrics)
    }

    func testProviderCashHasItsOwnCoverageAndNeverBecomesCompleteMonth() throws {
        let doc = try document()
        let provider = try XCTUnwrap(doc.openrouter)
        XCTAssertEqual(provider.coverage, "returned_rows_only")
        XCTAssertEqual(provider.amount(at: doc.generatedAt), Decimal(string: "2.950298"))
        XCTAssertNil(provider.amount(at: doc.generatedAt.addingTimeInterval(86400)))
    }

    @MainActor
    func testActualNarrowViewsRenderBothTabsWithoutLegacyBill() async throws {
        let doc = try document()
        for tab in CostTab.allCases {
            let hosting = NSHostingView(rootView: CostDashboardView(document: doc, now: doc.generatedAt, initialTab: tab))
            hosting.frame = NSRect(x: 0, y: 0, width: 392, height: 480)
            hosting.layoutSubtreeIfNeeded()
            try await Task.sleep(nanoseconds: 200_000_000)
            hosting.layoutSubtreeIfNeeded()
            var identifiers = Set<String>()
            func visit(_ view: NSView) {
                if let identifier = view.identifier?.rawValue { identifiers.insert(identifier) }
                view.subviews.forEach(visit)
            }
            visit(hosting)
            XCTAssertTrue(identifiers.contains("cost-dashboard"))
            XCTAssertFalse(identifiers.contains("legacy-monthly-cost-title"))
            XCTAssertFalse(identifiers.contains("compute-cost-shadow-title"))
            XCTAssertTrue(identifiers.contains(tab == .collaboration ? "collaboration-cost-amount" : "procurement-subscription"))
            if let output = Bundle.main.object(forInfoDictionaryKey: "PulseTestRenderDirectory") as? String,
               output.hasPrefix("/"),
               let bitmap = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) {
                hosting.cacheDisplay(in: hosting.bounds, to: bitmap)
                if let data = bitmap.representation(using: .png, properties: [:]) {
                    try FileManager.default.createDirectory(atPath: output, withIntermediateDirectories: true)
                    try data.write(to: URL(fileURLWithPath: output).appendingPathComponent("\(tab.rawValue)-synthetic.png"))
                }
            }
        }
    }
}

// Inputs are generated by the original Python query/publisher/exporter in CI.
// The test App never reads a production cache, native log or SSH endpoint.
private struct CostConsumerReplay {
    let object: [String: Any]
    let payload: Data
    let snapshot: PulseSnapshot
    let document: CostDashboard

    init(_ object: [String: Any]) throws {
        self.object = object
        payload = try JSONSerialization.data(withJSONObject: XCTUnwrap(object["snapshot"]))
        snapshot = try SnapshotDecoder().decode(payload)
        document = try XCTUnwrap(snapshot.costDashboard)
    }
}

private actor CostReplayTransport: SnapshotTransport {
    private var steps: [Result<Data, ErrorBox>]
    init(_ steps: [Result<Data, ErrorBox>]) { self.steps = steps }
    func fetch() async throws -> Data {
        guard !steps.isEmpty else { throw ErrorBox(message: "replay_exhausted") }
        return try steps.removeFirst().get()
    }
}

private final class CostReplayClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Date
    init(_ value: Date) { self.value = value }
    func now() -> Date { lock.lock(); defer { lock.unlock() }; return value }
    func set(_ value: Date) { lock.lock(); defer { lock.unlock() }; self.value = value }
}

extension CostDashboardTests {
    private func replays() throws -> [String: CostConsumerReplay] {
        // Existing CI build setting; replay inputs live beside renders.
        guard let renders = Bundle.main.object(forInfoDictionaryKey: "PulseTestRenderDirectory") as? String,
              renders.hasPrefix("/") else {
            throw XCTSkip("Run Stats macOS CI or prepare replay beside PULSE_STATS_RENDER_DIR")
        }
        let root = URL(fileURLWithPath: renders).deletingLastPathComponent().appendingPathComponent("consumer-replay")
        let index = try JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("index.json"))) as! [[String: Any]]
        XCTAssertGreaterThanOrEqual(index.count, 8)
        var result: [String: CostConsumerReplay] = [:]
        for row in index {
            let name = try XCTUnwrap(row["name"] as? String)
            let data = try Data(contentsOf: root.appendingPathComponent(name + ".json"))
            result[name] = try CostConsumerReplay(JSONSerialization.jsonObject(with: data) as! [String: Any])
        }
        return result
    }

    func testStageObjectsVersionsUnitsAndLateLinkFromRealPythonChain() throws {
        let inputs = try replays()
        let doc = try XCTUnwrap(inputs["stage-outputs"]?.document)
        let view = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt))
        let raw = try XCTUnwrap(view.stages)
        let rawOutputs = try XCTUnwrap(raw.outputs)
        XCTAssertTrue(rawOutputs.accepted.isValid(outputs: rawOutputs.items, recordedAt: view.recordedAt), String(describing: rawOutputs.accepted))
        XCTAssertTrue(rawOutputs.items.allSatisfy { $0.isValid })
        XCTAssertEqual(Set(raw.diagnostics!.keys), Set(["membership_conflicts", "revision_conflicts", "shared_refs", "unknown_refs", "unresolved_link_targets", "time_unallocated_refs"]))
        XCTAssertTrue(CostStageGroup.validPartition(rawOutputs.cumulative.groups, items: rawOutputs.items))
        let stage = try XCTUnwrap(view.validStages)
        let outputs = try XCTUnwrap(stage.outputs)
        XCTAssertEqual(stage.status, "recorded_subset")
        XCTAssertFalse(stage.executionAuthority)
        XCTAssertEqual(outputs.items.count, 5)
        XCTAssertEqual(outputs.cumulative.objectCount, 2)
        XCTAssertEqual(outputs.formed.count, 3)
        XCTAssertTrue(outputs.items.allSatisfy { $0.wi == nil })
        let improvement = try XCTUnwrap(outputs.formed.first { $0.contribution == "improved" })
        XCTAssertEqual(improvement.quantity, "1") // Two versions, not two new capabilities.
        XCTAssertEqual(improvement.objectCount, 1)
        XCTAssertEqual(improvement.factRefs.count, 2)
        XCTAssertEqual(outputs.formed.first { $0.contribution == "reused" }?.contributionText, "复用（不计新增）")
        let article = try XCTUnwrap(outputs.formed.first { $0.category == "article" })
        XCTAssertNil(article.quantity)
        XCTAssertEqual(article.quantityText, "数量未知 · paragraph")
        XCTAssertEqual(outputs.accepted.status, "unavailable")
        XCTAssertTrue(view.groups.isEmpty)
        XCTAssertNil(view.summary.cost.amount)
        let linkedDoc = try XCTUnwrap(inputs["stage-linked"]?.document)
        let linked = try XCTUnwrap(linkedDoc.activityView(period: .today, at: linkedDoc.generatedAt))
        XCTAssertGreaterThan(linked.watermark, view.watermark)
        XCTAssertEqual(linked.validStages?.outputs?.formed, outputs.formed)
        XCTAssertTrue(linked.validStages!.outputs!.items.allSatisfy { $0.wi == "wi_20261008_550" })
        let conflictDoc = try XCTUnwrap(inputs["stage-conflict"]?.document)
        let conflict = try XCTUnwrap(conflictDoc.activityView(period: .today, at: conflictDoc.generatedAt)?.validStages)
        XCTAssertEqual(conflict.status, "conflict")
        XCTAssertTrue(conflict.outputs!.items.contains { $0.wiText == "归属冲突" })
        XCTAssertTrue(outputs.items.allSatisfy { $0.wi == nil }) // Old watermark unchanged.
    }

    func testExactAcceptedVersionRequiresFinalWitnessAtTheSameWatermark() throws {
        let doc = try XCTUnwrap(replays()["stage-outputs"]?.document)
        let view = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt))
        let outputs = try XCTUnwrap(view.validStages?.outputs)
        let group = try XCTUnwrap(outputs.formed.first { $0.contribution == "reused" })
        let selected = outputs.items.filter { group.factRefs.contains($0.factRef) }
        let at = try XCTUnwrap(view.recordedAt)
        func accepted(_ mutation: String = "") -> CostStageAccepted {
            let facts = selected.map { output in
                CostStageAcceptance(outputFactRef: output.factRef, objectRef: output.objectRef,
                    versionRef: mutation == "version" ? "ccf1_" + String(repeating: "f", count: 64) : output.versionRef,
                    candidateRef: "ccf1_" + String(repeating: "a", count: 64),
                    verificationRef: mutation == "missing" ? nil : "ccf1_" + String(repeating: "b", count: 64),
                    waiverRef: nil, humanReviewRef: "ccf1_" + String(repeating: "c", count: 64),
                    acceptedAt: mutation == "future" ? at.addingTimeInterval(1) : at,
                    scope: mutation == "scope" ? "intermediate" : "final")
            }
            return CostStageAccepted(status: "recorded_subset", reason: nil,
                objectCount: group.objectCount, groups: [group], items: facts)
        }
        XCTAssertTrue(accepted().isValid(outputs: outputs.items, recordedAt: at))
        for mutation in ["version", "missing", "future", "scope"] {
            XCTAssertFalse(accepted(mutation).isValid(outputs: outputs.items, recordedAt: at), mutation)
        }
    }

    func testStageInvalidAuthorityAndAcceptanceCannotBecomeValidOutput() throws {
        let input = try XCTUnwrap(replays()["stage-outputs"])
        for mutation in ["authority", "acceptance", "duplicate", "period", "quantity"] {
            var object = input.object
            var snapshot = object["snapshot"] as! [String: Any]
            var doc = snapshot["cost_dashboard"] as! [String: Any]
            var views = doc["views"] as! [[String: Any]]
            for index in views.indices {
                var stage = views[index]["stages"] as! [String: Any]
                var outputs = stage["outputs"] as! [String: Any]
                if mutation == "authority" { stage["execution_authority"] = true }
                if mutation == "acceptance" { outputs["accepted"] = ["status": "accepted"] }
                var items = outputs["items"] as! [[String: Any]]
                if mutation == "duplicate" { items.append(items[0]) }
                if mutation == "period" { items[0]["occurred_at"] = "2027-01-01T01:00:00Z" }
                if mutation == "quantity" { items[0]["quantity"] = "1garbage" }
                outputs["items"] = items; stage["outputs"] = outputs; views[index]["stages"] = stage
            }
            doc["views"] = views; snapshot["cost_dashboard"] = doc; object["snapshot"] = snapshot
            let changed = try CostConsumerReplay(object).document
            let view = try XCTUnwrap(changed.activityView(period: .today, at: changed.generatedAt))
            XCTAssertNil(view.validStages, mutation)
        }
        let old = try document()
        XCTAssertTrue(old.views.allSatisfy { $0.stages == nil && $0.validStages == nil })
    }

    @MainActor
    func testActualDashboardHasStageOutputEntryWithoutMachineUsage() async throws {
        let doc = try XCTUnwrap(replays()["stage-outputs"]?.document)
        let hosting = NSHostingView(rootView: CostDashboardView(document: doc, now: doc.generatedAt))
        hosting.frame = NSRect(x: 0, y: 0, width: 392, height: 900)
        hosting.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        hosting.layoutSubtreeIfNeeded()
        var found = false
        func visit(_ view: NSView) {
            if view.identifier?.rawValue == "stage-output-section" { found = true }
            view.subviews.forEach(visit)
        }
        visit(hosting)
        XCTAssertTrue(found)
    }

    private func assertCost(_ actual: QueryMachineCost, _ expected: [String: Any], _ context: String) {
        XCTAssertEqual(actual.machineCashUsd, expected["machine_cash_usd"] as? String, context)
        XCTAssertEqual(actual.partialMachineCashUsd, expected["partial_machine_cash_usd"] as? String, context)
        XCTAssertEqual(actual.cashBasis, expected["cash_basis"] as? String, context)
        XCTAssertEqual(actual.reasons, expected["reasons"] as? [String], context)
        let unpriced = expected["unpriced"] as! [[String: String]]
        XCTAssertEqual(actual.unpriced.map(\.factRef), unpriced.map { $0["fact_ref"]! }, context)
        XCTAssertEqual(actual.unpriced.map(\.reason), unpriced.map { $0["reason"]! }, context)
        XCTAssertEqual(actual.amount, CostText.decimal(actual.machineCashUsd ?? actual.partialMachineCashUsd), context)
    }

    func testPreparedQueryPublisherExporterSwiftParityForEveryGroup() throws {
        let inputs = try replays()
        for (name, replay) in inputs {
            let doc = replay.document
            XCTAssertEqual(doc.status, replay.object["expected_status"] as? String, name)
            let expected = replay.object["expected"] as! [[String: Any]]
            XCTAssertEqual(doc.views.count, expected.count, name)
            for row in expected {
                let period = try XCTUnwrap(CostPeriod(rawValue: row["period"] as! String))
                let dimension = row["dimension"] as! String
                let resolved = dimension == "activity"
                    ? doc.activityView(period: period, at: doc.generatedAt)
                    : doc.view(period: period, dimension: try XCTUnwrap(CostDimension(rawValue: dimension)), at: doc.generatedAt)
                let view = try XCTUnwrap(resolved, name)
                XCTAssertEqual(view.watermark, row["watermark"] as? Int, name)
                XCTAssertEqual(view.reportSha256, row["report_sha256"] as? String, name)
                let query = row["query"] as! [String: Any]
                XCTAssertEqual(view.query.fromDate, query["from_date"] as? String, name)
                XCTAssertEqual(view.query.toDate, query["to_date"] as? String, name)
                XCTAssertEqual(view.query.timezone, query["timezone"] as? String, name)
                XCTAssertEqual(view.query.groupBy, query["group_by"] as? [String], name)
                let summary = row["summary"] as! [String: Any]
                assertCost(view.summary.cost, summary["cost"] as! [String: Any], name)
                let usage = summary["usage"] as! [String: Any]
                XCTAssertEqual(view.summary.usage.eventCount, usage["event_count"] as? Int, name)
                XCTAssertEqual(view.summary.usage.knownTotalTokens, usage["known_total_tokens"] as? Int, name)
                let groups = row["groups"] as! [[String: Any]]
                XCTAssertEqual(view.groups.count, groups.count, name)
                for (group, original) in zip(view.groups, groups) {
                    XCTAssertEqual(group.key, original["key"] as? [String: String], name)
                    assertCost(group.cost, original["cost"] as! [String: Any], name)
                }
            }
        }
        let many = try XCTUnwrap(inputs["partial-many-groups"]?.document)
        XCTAssertEqual(many.view(period: .month, dimension: .wi, at: many.generatedAt)?.groups.count, 36)
        let conflict = try XCTUnwrap(inputs["conflict-with-priced-groups"]?.document)
        let conflicted = try XCTUnwrap(conflict.view(period: .month, dimension: .agent, at: conflict.generatedAt))
        XCTAssertNil(conflicted.summary.cost.amount)
        XCTAssertTrue(conflicted.groups.contains { $0.cost.amount != nil })
        let zero = try XCTUnwrap(inputs["known-zero"]?.document)
        XCTAssertEqual(zero.view(period: .month, dimension: .wi, at: zero.generatedAt)?.summary.cost.amount, .zero)
        let exact = try XCTUnwrap(inputs["complete"]?.document)
        XCTAssertEqual(exact.view(period: .month, dimension: .wi, at: exact.generatedAt)?.summary.cost.exactAmount,
                       "123.123456789012345678")
    }

    @MainActor
    func testPreparedCostTwoRefreshesFailureRecoveryAndStoreRestart() async throws {
        let inputs = try replays()
        let first = try XCTUnwrap(inputs["partial-many-groups"])
        let next = try XCTUnwrap(inputs["next-watermark"])
        let clock = CostReplayClock(first.document.generatedAt)
        let cache = MemorySnapshotCache()
        let store = SnapshotStore(transport: CostReplayTransport([
            .success(first.payload), .failure(ErrorBox(message: "synthetic_transport_failure")), .success(next.payload)
        ]), cache: cache, now: { clock.now() })
        await store.refresh()
        XCTAssertEqual(store.snapshot?.costDashboard, first.document)
        clock.set(first.document.expiresAt.addingTimeInterval(5))
        await store.refresh()
        if case .cached(_, let expired, _) = store.transportState { XCTAssertTrue(expired) }
        else { XCTFail("transport failure must retain an explicitly cached snapshot") }
        XCTAssertNotNil(store.costDashboard?.view(period: .month, dimension: .wi, at: clock.now()))
        XCTAssertNotNil(store.costDashboard?.notice(at: clock.now(), refresh: nil))
        // The next source publication has a later watermark and remains fresh.
        await store.refresh()
        XCTAssertEqual(store.snapshot?.costDashboard, next.document)
        XCTAssertNotNil(store.snapshot?.costDashboard?.view(period: .month, dimension: .wi, at: clock.now()))
        let restarted = SnapshotStore(transport: CostReplayTransport([
            .failure(ErrorBox(message: "synthetic_offline_restart")), .success(next.payload)
        ]), cache: cache, now: { clock.now() })
        await restarted.refresh()
        XCTAssertEqual(restarted.snapshot?.costDashboard, next.document)
        await restarted.refresh()
        XCTAssertEqual(restarted.snapshot?.costDashboard, next.document)
        XCTAssertEqual(restarted.snapshot?.incidents, first.snapshot.incidents)
        XCTAssertEqual(restarted.snapshot?.alerts, first.snapshot.alerts)
        XCTAssertEqual(restarted.snapshot?.metrics, next.snapshot.metrics)
        let changedIDs: Set<String> = ["usage.today.input_tokens", "usage.today.input_tokens_uncached",
            "usage.today.output_tokens", "usage.today.cache_read_tokens", "usage.today.cache_creation_tokens",
            "usage.by_agent", "usage.today.source_note"]
        XCTAssertEqual(restarted.snapshot?.metrics.filter { !changedIDs.contains($0.id) },
                       first.snapshot.metrics.filter { !changedIDs.contains($0.id) })
        let firstTokens = SnapshotMetrics(first.snapshot).int(MetricID.outputTokens)
        let nextTokens = SnapshotMetrics(next.snapshot).int(MetricID.outputTokens)
        XCTAssertNotEqual(firstTokens, nextTokens)
        XCTAssertEqual(SnapshotMetrics(try XCTUnwrap(restarted.snapshot)).int(MetricID.outputTokens), nextTokens)
    }

    @MainActor
    func testPreparedRealQueryAndLargeGroupNativeViews() async throws {
        let inputs = try replays()
        for name in ["partial-many-groups", "real-pi-query"] where inputs[name] != nil {
            let doc = try XCTUnwrap(inputs[name]?.document)
            for tab in CostTab.allCases {
                let view = NSHostingView(rootView: CostDashboardView(document: doc, now: doc.generatedAt, initialTab: tab))
                view.frame = NSRect(x: 0, y: 0, width: 392, height: 480)
                view.layoutSubtreeIfNeeded()
                try await Task.sleep(nanoseconds: 200_000_000)
                view.layoutSubtreeIfNeeded()
                var ids = Set<String>()
                func walk(_ node: NSView) {
                    if let id = node.identifier?.rawValue { ids.insert(id) }
                    node.subviews.forEach(walk)
                }
                walk(view)
                XCTAssertTrue(ids.contains(tab == .collaboration ? "collaboration-cost-amount" : "procurement-subscription"))
                XCTAssertFalse(ids.contains("legacy-monthly-cost-title"))
                let bitmap = try XCTUnwrap(view.bitmapImageRepForCachingDisplay(in: view.bounds))
                view.cacheDisplay(in: view.bounds, to: bitmap)
                let png = try XCTUnwrap(bitmap.representation(using: .png, properties: [:]))
                let output = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: "PulseTestRenderDirectory") as? String)
                try FileManager.default.createDirectory(atPath: output, withIntermediateDirectories: true)
                try png.write(to: URL(fileURLWithPath: output).appendingPathComponent("consumer-\(name)-\(tab.rawValue).png"))
            }
        }
    }
}


extension CostDashboardTests {
    func testFiveIdentityDimensionsAndSeparateTaskCoverageReachSwift() throws {
        let doc = try XCTUnwrap(replays()["five-dimensions"]?.document)
        XCTAssertEqual(doc.views.count, 18)
        for period in CostPeriod.allCases {
            for dimension in CostDimension.allCases {
                let view = try XCTUnwrap(doc.view(period: period, dimension: dimension, at: doc.generatedAt))
                XCTAssertEqual(view.summary.usage.agentUnassignedEventCount, 0)
                XCTAssertEqual(view.summary.usage.agentUnassignedPricedUsd, "0")
                XCTAssertEqual(view.summary.usage.wiUnassignedEventCount, 1)
                XCTAssertNotNil(CostText.decimal(view.summary.usage.wiUnassignedPricedUsd))
                XCTAssertEqual(view.query.groupBy, [dimension.rawValue])
            }
        }
        let agents = try XCTUnwrap(doc.view(period: .today, dimension: .agent, at: doc.generatedAt))
        XCTAssertEqual(Set(agents.groups.map(\.title)), ["Pulse", "Aki"])
        let tasks = try XCTUnwrap(doc.view(period: .today, dimension: .wi, at: doc.generatedAt))
        XCTAssertTrue(tasks.groups.contains { $0.title == "WI 未关联" })
        XCTAssertFalse(agents.groups.contains { $0.title == "Agent 未识别" })
        let entry = try XCTUnwrap(doc.view(period: .today, dimension: .entrypoint, at: doc.generatedAt))
        XCTAssertEqual(Set(entry.groups.map(\.title)), ["Codex App", "飞书"])
    }

    @MainActor
    func testFiveDimensionNarrowViewShowsSeparateMissingAgentAndWI() async throws {
        let doc = try XCTUnwrap(replays()["five-dimensions"]?.document)
        let hosting = NSHostingView(rootView: CostDashboardView(document: doc, now: doc.generatedAt,
            initialEvidenceExpanded: true))
        hosting.frame = NSRect(x: 0, y: 0, width: 392, height: 560)
        hosting.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        hosting.layoutSubtreeIfNeeded()
        var identifiers = Set<String>()
        func visit(_ view: NSView) {
            if let identifier = view.identifier?.rawValue { identifiers.insert(identifier) }
            view.subviews.forEach(visit)
        }
        visit(hosting)
        XCTAssertFalse(identifiers.contains("cost-agent-unassigned"), "Zero missing Agents is not an exception to repeat")
        XCTAssertTrue(identifiers.contains("cost-wi-unassigned"))
        if let output = Bundle.main.object(forInfoDictionaryKey: "PulseTestRenderDirectory") as? String,
           output.hasPrefix("/"), let bitmap = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) {
            hosting.cacheDisplay(in: hosting.bounds, to: bitmap)
            if let data = bitmap.representation(using: .png, properties: [:]) {
                try FileManager.default.createDirectory(atPath: output, withIntermediateDirectories: true)
                try data.write(to: URL(fileURLWithPath: output).appendingPathComponent("five-dimensions-synthetic.png"))
            }
        }
    }
}

extension CostDashboardTests {
    func testActivitySourceExcludesIdleCohortAndNeverFakesJointRows() throws {
        let doc = try XCTUnwrap(replays()["activity-today"]?.document)
        let table = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt))
        XCTAssertEqual(table.groups.count, 1)
        XCTAssertEqual(table.groups.first?.key["agent"], "hestia")
        XCTAssertEqual(table.groups.first?.key["wi"], "wi_20260826_825")
        XCTAssertEqual(table.summary.cost.amount, Decimal(string: "0.53"))
        XCTAssertEqual(doc.activityView(period: .week, at: doc.generatedAt)?.groups.count, 2)
        XCTAssertFalse(table.groups.contains { $0.key["agent"] == "pulse" || $0.key["wi"] == "wi_20260820_568" })
        XCTAssertEqual(doc.activityView(period: .today, at: doc.expiresAt), table)
        let old = try document()
        XCTAssertNil(old.activityView(period: .today, at: old.generatedAt))
        let empty = try XCTUnwrap(replays()["activity-empty"]?.document)
        XCTAssertEqual(empty.activityView(period: .today, at: empty.generatedAt)?.groups.count, 0)
    }

    func testWiColumnDistinguishesCronNoneUnknownAndCanonicalWi() throws {
        let doc = try XCTUnwrap(replays()["activity-today"]?.document)
        let base = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt)?.groups.first)
        let labels = ["cron", "none", "unassigned", "wi_20260901_001"]
        let rows = labels.map { label -> CostQueryGroup in
            var key = base.key; key["wi"] = label
            return CostQueryGroup(key: key, cost: base.cost, usage: base.usage)
        }
        XCTAssertEqual(rows.prefix(3).map { CostTableColumn.wi.text($0) }, ["cron", "无", "未关联"])
        for (filter, expected) in [("cron", "cron"), ("无", "none"), ("未关联", "unassigned")] {
            var selection = CostTableSelection(); selection.filters[.wi] = filter
            XCTAssertEqual(selection.rows(rows).map { $0.key["wi"]! }, [expected])
        }
        XCTAssertEqual(CostTableSelection().rows(rows).count, 4)
    }

    func testPublishedWorkItemNamesReachTheRowsThatNameThem() throws {
        let plain = try document()
        let wiView = try XCTUnwrap(plain.views.first { $0.dimension == "wi" && $0.groups.contains { ($0.key["wi"] ?? "").hasPrefix("wi_") } })
        let wi = try XCTUnwrap(wiView.groups.first { ($0.key["wi"] ?? "").hasPrefix("wi_") }?.key["wi"])
        let doc = try document { object in
            object["work_items"] = [["id": wi, "title": "无界工作环境", "summary": NSNull(), "status": "completed"]]
        }
        XCTAssertEqual(doc.workItems?.map(\.id), [wi])
        let named = try XCTUnwrap(doc.views.first { $0.dimension == "wi" }).naming(doc.workItems ?? [])
        XCTAssertEqual(named.groups.first { $0.key["wi"] == wi }?.workItem?.title, "无界工作环境")
        XCTAssertTrue(named.groups.filter { $0.key["wi"] != wi }.allSatisfy { $0.workItem == nil })
        XCTAssertNil(plain.workItems)
    }

    func testWorkItemProjectPageOpensOnlyItsOwnLocalPage() throws {
        let plain = try document()
        let wiView = try XCTUnwrap(plain.views.first { $0.dimension == "wi" && $0.groups.contains { ($0.key["wi"] ?? "").hasPrefix("wi_") } })
        let wi = try XCTUnwrap(wiView.groups.first { ($0.key["wi"] ?? "").hasPrefix("wi_") }?.key["wi"])
        let page = "/Users/me/.local/share/pulse/tools/scl-core/views/\(wi).html"
        let doc = try document { object in
            object["work_items"] = [["id": wi, "title": "项目页", "summary": NSNull(), "status": "active", "project_page": page]]
        }
        let item = try XCTUnwrap(doc.workItems?.first)
        XCTAssertEqual(item.projectPage, page)
        XCTAssertEqual(item.projectPageURL, URL(fileURLWithPath: page))
        let named = try XCTUnwrap(doc.views.first { $0.dimension == "wi" }).naming(doc.workItems ?? [])
        XCTAssertEqual(named.groups.first { $0.key["wi"] == wi }?.workItem?.projectPageURL, URL(fileURLWithPath: page))
        for bad in ["relative/\(wi).html", "/x/../\(wi).html", "/x/wi_20990101_999.html", "/x/\(wi).txt"] {
            XCTAssertNil(CostWorkItem(id: wi, title: "t", summary: nil, status: nil, projectPage: bad).projectPageURL, bad)
        }
        XCTAssertNil(CostWorkItem(id: wi, title: "t", summary: nil, status: nil).projectPageURL)
    }

    func testWiColumnNamesScheduledJobsAndMarksHolderSupplement() throws {
        let json = #"{"key":{"wi":"cron:hestia-triage"},"cost":{"machine_cash_usd":null,"partial_machine_cash_usd":"1.5","cash_basis":"openrouter_standard_token_cash","reasons":[],"unpriced":[]},"usage":{"tokens":{},"event_count":3,"known_total_tokens":30},"attribution":{"sources":{"declared":{"event_count":2,"priced_machine_cash_usd":"1"},"holder_supplement":{"event_count":1,"priced_machine_cash_usd":"0.5"}},"job":{"id":"hestia-triage","name":"Oikos 30 分钟异常分流"}}}"#
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let base = try decoder.decode(CostQueryGroup.self, from: Data(json.utf8))
        let attribution = try XCTUnwrap(base.attribution)
        XCTAssertEqual(attribution.sources["holder_supplement"]?.eventCount, 1)
        XCTAssertEqual(attribution.summary, "声明 2 条 · 持有补充 1 条")
        func row(_ wi: String, _ attribution: CostAttribution?) -> CostQueryGroup {
            var key = base.key; key["wi"] = wi
            return CostQueryGroup(key: key, cost: base.cost, usage: base.usage, attribution: attribution)
        }
        let job = row("cron:hestia-triage", attribution)
        XCTAssertEqual(CostTableColumn.wi.text(job), "自动任务：Oikos 30 分钟异常分流")
        XCTAssertEqual(job.outcomeText, "—")
        XCTAssertEqual(CostTableColumn.wi.text(row("cron:other-job", attribution)), "自动任务：other-job")
        // The holder supplement is a colored mark, not a word in the cell.
        var named = row("wi_20260901_001", attribution)
        XCTAssertEqual(CostTableColumn.wi.text(named), "wi_20260901_001")
        named.workItem = CostWorkItem(id: "wi_20260901_001", title: "无界工作环境", summary: "让 Agent 在任何节点工作。", status: "active")
        // The cell is the ID only; the name and purpose open with the row.
        XCTAssertEqual(CostTableColumn.wi.text(named), "wi_20260901_001")
        XCTAssertTrue(named.hasHolderSupplement)
        XCTAssertEqual(named.holderSupplementCount, 1)
        XCTAssertEqual(named.workItem?.statusText, "进行中")
        XCTAssertEqual(CostTableColumn.wi.text(row("wi_20260901_001", nil)), "wi_20260901_001")
        XCTAssertEqual(CostTableColumn.wi.text(row("ambiguous", attribution)), "待核验")
        var selection = CostTableSelection(); selection.filters[.wi] = "自动任务"
        XCTAssertEqual(selection.rows([job, row("cron", nil)]).count, 1)
    }

    func testNoInteractiveEntrypointLabelKeepsCronAndUnknownDistinct() throws {
        let doc = try XCTUnwrap(replays()["activity-today"]?.document)
        let base = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt)?.groups.first)
        let origins = ["automation", "feishu", "telegram", "codex_app", "cli", "unassigned"]
        let rows = origins.map { origin -> CostQueryGroup in
            var key = base.key; key["wi"] = "cron"; key["entrypoint"] = origin
            return CostQueryGroup(key: key, cost: base.cost, usage: base.usage)
        }
        XCTAssertEqual(CostTableColumn.entrypoint.text(rows[0]), "无")
        XCTAssertEqual(CostTableColumn.wi.text(rows[0]), "cron")
        XCTAssertEqual(Array(rows[1...4]).map { CostTableColumn.entrypoint.text($0) },
                       ["飞书", "Telegram", "Codex App", "CLI"])
        XCTAssertNotEqual(CostTableColumn.entrypoint.text(rows[5]), "无")
        var selection = CostTableSelection(); selection.filters[.entrypoint] = "无"
        let filtered = selection.rows(rows)
        XCTAssertEqual(filtered.count, 1)
        XCTAssertEqual(filtered.first?.key["entrypoint"], "automation")
        XCTAssertEqual(filtered.first?.cost.amount, base.cost.amount)
        XCTAssertEqual(CostTableSelection().rows(rows).count, origins.count)
    }

    @MainActor
    func testWholePopoverStaysCompactWhileTableScrollsAndResizes() async throws {
        let inputs = try replays()
        for name in ["activity-today", "real-activity-table"] where inputs[name] != nil {
            let replay = try XCTUnwrap(inputs[name])
            let replayTime = replay.document.generatedAt
            let store = SnapshotStore(transport: CostReplayTransport([.success(replay.payload)]),
                cache: MemorySnapshotCache(snapshot: replay.snapshot), now: { replayTime })
            let browser = CostBrowserState()
            let hosting = NSHostingView(rootView: StatsPopoverView(store: store, tablePreferences: nil, costBrowser: browser))
            let window = NSWindow(contentRect: NSRect(x:0,y:0,width:420,height:580),
                styleMask:[.titled],backing:.buffered,defer:false)
            window.contentView = hosting; window.orderFront(nil)
            defer { window.orderOut(nil) }
            await store.refresh()
            try await Task.sleep(nanoseconds:200_000_000)
            hosting.layoutSubtreeIfNeeded()
            XCTAssertEqual(hosting.fittingSize.width, 420, accuracy:1)
            XCTAssertEqual(hosting.fittingSize.height, 580, accuracy:1)
            func find(_ node:NSView) -> CostGridContainer? {
                if let grid = node as? CostGridContainer { return grid }
                return node.subviews.compactMap(find).first
            }
            XCTAssertNil(find(hosting), "The popover must not embed a second scrolling table")
            browser.openDetails(store: store, preferences: nil)
            let detailWindow = try XCTUnwrap(browser.window)
            defer { detailWindow.close() }
            try await Task.sleep(nanoseconds:100_000_000)
            let detailHost = try XCTUnwrap(detailWindow.contentView)
            detailHost.layoutSubtreeIfNeeded()
            let grid = try XCTUnwrap(find(detailHost))
            let scroll = try XCTUnwrap(grid.table.enclosingScrollView)
            XCTAssertLessThanOrEqual(scroll.bounds.width, detailHost.bounds.width)
            XCTAssertTrue(scroll.hasHorizontalScroller)
            XCTAssertTrue(scroll.autohidesScrollers)
            XCTAssertEqual(grid.table.numberOfColumns,9)
            grid.scrollToVisible(grid.bounds)
            hosting.layoutSubtreeIfNeeded()
            grid.table.tableColumns[1].width = 1300
            hosting.layoutSubtreeIfNeeded()
            grid.table.scrollColumnToVisible(6)
            try await Task.sleep(nanoseconds:100_000_000)
            hosting.layoutSubtreeIfNeeded()
            XCTAssertFalse(try XCTUnwrap(scroll.horizontalScroller).isHidden, "Overflowing columns must keep a visible horizontal control")
            XCTAssertGreaterThan(scroll.contentView.bounds.minX,0)
            XCTAssertTrue(scroll.contentView.bounds.intersects(grid.table.rect(ofColumn:6)))
            let headerClip = try XCTUnwrap(grid.filters.superview as? NSClipView)
            XCTAssertEqual(headerClip.bounds.minX,scroll.contentView.bounds.minX,accuracy:1)
            XCTAssertEqual(hosting.fittingSize.width,420,accuracy:1)
            XCTAssertEqual(window.contentView!.bounds.width,420,accuracy:1)
            grid.scrollToVisible(grid.bounds)
            hosting.layoutSubtreeIfNeeded()
            if let output = Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String, !output.isEmpty {
                let url = URL(fileURLWithPath:output).appendingPathComponent("actual-popover-420-\(name)-scrolled.png")
                if let rep = hosting.bitmapImageRepForCachingDisplay(in:hosting.bounds) {
                    hosting.cacheDisplay(in:hosting.bounds,to:rep)
                    try rep.representation(using:.png,properties:[:])?.write(to:url)
                }
            }
        }
    }

    func testTableSortsDecimalNotTextAndKeepsUnpricedActualRows() throws {
        let doc = try XCTUnwrap(replays()["activity-today"]?.document)
        let base = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt)?.groups.first)
        func row(_ amount: String?, wi: String, events: Int = 1) -> CostQueryGroup {
            var key = base.key; key["wi"] = wi
            let cost = QueryMachineCost(machineCashUsd: nil, partialMachineCashUsd: amount,
                cashBasis: "openrouter_standard_token_cash", reasons: ["source_coverage_unknown"], unpriced: [])
            return CostQueryGroup(key: key, cost: cost,
                usage: CostGroupUsage(tokens: base.usage!.tokens, eventCount: events, knownTotalTokens: nil))
        }
        let input = [row("2",wi:"two"), row(nil,wi:"unpriced"),row("10",wi:"ten"),row("0",wi:"zero"),row("100",wi:"idle",events:0)]
        var selection = CostTableSelection()
        XCTAssertEqual(selection.rows(input).map { $0.key["wi"]! }, ["ten","two","zero","unpriced"])
        selection.ascending = true
        XCTAssertEqual(selection.rows(input).map { $0.key["wi"]! }, ["zero","two","ten","unpriced"])
        selection.filters[.amount] = ">=2"
        XCTAssertEqual(selection.rows(input).map { $0.key["wi"]! }, ["two","ten"])
        selection.filters[.amount] = "0..2"
        XCTAssertEqual(selection.rows(input).count, 2)
        selection.filters[.amount] = "未计价"
        XCTAssertEqual(selection.rows(input).first?.key["wi"], "unpriced")
        selection.filters = [.agent: "HESTIA", .wi: "ten", .node: "ownermac"]
        XCTAssertEqual(selection.rows(input).count, 1)
        selection.filters[.agent] = "Pulse"
        XCTAssertEqual(selection.rows(input).count, 0)
    }

    @MainActor
    func testNativeTableColumnOrderFiltersResizeAndPeriodReset() async throws {
        let doc = try XCTUnwrap(replays()["activity-today"]?.document)
        let rows = try XCTUnwrap(doc.activityView(period: .week, at: doc.generatedAt)?.groups)
        let suite = "stats-table-test-" + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let coordinator = CostActivityTable.Coordinator(preferences: defaults)
        let view = CostGridContainer(frame: NSRect(x:0,y:0,width:940,height:290))
        coordinator.connect(view); coordinator.update(rows,scope:"week")
        let window = NSWindow(contentRect:view.frame,styleMask:[.titled],backing:.buffered,defer:false)
        window.contentView = view; window.orderFront(nil)
        defer { window.orderOut(nil) }
        view.layoutSubtreeIfNeeded(); view.filters.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds:100_000_000)
        XCTAssertEqual(view.table.tableColumns.map(\.title), ["租户","Agent","WI","产出","金额","入口","节点","Runtime","模型"])
        XCTAssertTrue(view.table.allowsColumnResizing)
        XCTAssertTrue(view.table.enclosingScrollView!.hasHorizontalScroller)
        let field = try XCTUnwrap(view.filters.subviews.first { $0.identifier?.rawValue == "cost-filter-agent" } as? NSTextField)
        field.stringValue = "Hestia"
        view.filters.controlTextDidChange(Notification(name:NSControl.textDidChangeNotification,object:field))
        XCTAssertEqual(coordinator.visible.count, 1)
        XCTAssertEqual(coordinator.visible.first?.key["agent"], "hestia")
        coordinator.update(rows,scope:"week")
        XCTAssertEqual(coordinator.visible.count, 1, "Natural refresh retains filters")
        coordinator.update(rows,scope:"today")
        XCTAssertEqual(coordinator.visible.count, 2, "Changing period clears the previous filter")
        let column = view.table.tableColumns[1]
        column.width = 281
        coordinator.tableViewColumnDidResize(Notification(name:NSTableView.columnDidResizeNotification,object:view.table))
        let restored = CostGridContainer(frame:view.frame)
        let next = CostActivityTable.Coordinator(preferences:defaults); next.connect(restored)
        XCTAssertEqual(restored.table.tableColumns[1].width, 281, accuracy:1)
        view.table.sortDescriptors = [NSSortDescriptor(key:"agent",ascending:true)]
        XCTAssertEqual(coordinator.selection.sortColumn, .agent)
        XCTAssertTrue(coordinator.selection.ascending)
    }

    @MainActor
    func testActualEightColumnViewsAndCollapsedEvidenceRender() async throws {
        let inputs = try replays()
        var names = ["activity-today", "activity-many", "activity-empty"]
        if inputs["real-activity-table"] != nil { names.append("real-activity-table") }
        for name in names {
            let doc = try XCTUnwrap(inputs[name]?.document)
            for width in [CGFloat(940), CGFloat(392)] {
                let hosting = NSHostingView(rootView: CostDashboardView(document:doc,now:doc.generatedAt,
                    initialPeriod:name == "activity-many" ? .week : .today).padding(16)
                    .background(Color(nsColor: .windowBackgroundColor)).preferredColorScheme(.dark))
                hosting.frame = NSRect(x:0,y:0,width:width,height:570)
                let window = NSWindow(contentRect:hosting.frame,styleMask:[.titled],backing:.buffered,defer:false)
                window.appearance = NSAppearance(named:.darkAqua)
                window.contentView = hosting; window.orderFront(nil)
                hosting.layoutSubtreeIfNeeded()
                try await Task.sleep(nanoseconds:200_000_000)
                hosting.layoutSubtreeIfNeeded()
                var identifiers = Set<String>(); var tables: [NSTableView] = []
                func visit(_ node: NSView) {
                    if let id = node.identifier?.rawValue { identifiers.insert(id) }
                    if let table = node as? NSTableView { tables.append(table) }
                    node.subviews.forEach(visit)
                }
                visit(hosting)
                XCTAssertTrue(identifiers.contains("cost-joint-activity"))
                XCTAssertTrue(identifiers.contains("cost-evidence-disclosure"))
                XCTAssertFalse(identifiers.contains("cost-partial-evidence"), "Diagnostics are collapsed by default")
                XCTAssertTrue(tables.isEmpty,"Preview has no nested table scroll")
                let visibleRows=identifiers.filter { $0.hasPrefix("cost-preview-row-") }.count
                if name == "activity-today" { XCTAssertEqual(visibleRows, 1) }
                if name == "activity-empty" { XCTAssertEqual(visibleRows, 0) }
                if name == "activity-many" { XCTAssertEqual(visibleRows, 5) }
                let bitmap = try XCTUnwrap(hosting.bitmapImageRepForCachingDisplay(in:hosting.bounds))
                hosting.cacheDisplay(in:hosting.bounds,to:bitmap)
                let data = try XCTUnwrap(bitmap.representation(using:.png,properties:[:]))
                let output = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String)
                try FileManager.default.createDirectory(atPath:output,withIntermediateDirectories:true)
                try data.write(to:URL(fileURLWithPath:output).appendingPathComponent("\(name)-\(Int(width)).png"))
                window.orderOut(nil)
            }
            // The exact eight-column checks remain, now in the actual full window.
            let sampledAt=doc.generatedAt
            let store=SnapshotStore(transport:CostReplayTransport([.success(try XCTUnwrap(inputs[name]).payload)]),
                cache:MemorySnapshotCache(),now:{sampledAt})
            await store.refresh()
            let browser=CostBrowserState(period:name == "activity-many" ? .week : .today)
            browser.openDetails(store:store,preferences:nil)
            let full=try XCTUnwrap(browser.window?.contentView)
            try await Task.sleep(nanoseconds:100_000_000);full.layoutSubtreeIfNeeded()
            func table(_ v:NSView)->NSTableView? { (v as? NSTableView) ?? v.subviews.compactMap(table).first }
            let actual=try XCTUnwrap(table(full))
            XCTAssertEqual(actual.numberOfColumns,9)
            if name == "activity-today" { XCTAssertEqual(actual.numberOfRows,1) }
            if name == "activity-empty" { XCTAssertEqual(actual.numberOfRows,0) }
            if name == "activity-many" { XCTAssertGreaterThan(actual.numberOfRows,5) }
            browser.window?.close()
        }
        let doc = try XCTUnwrap(inputs["activity-today"]?.document)
        let expanded = NSHostingView(rootView:CostDashboardView(document:doc,now:doc.generatedAt,initialEvidenceExpanded:true))
        expanded.frame = NSRect(x:0,y:0,width:940,height:1100)
        expanded.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds:150_000_000)
        var found = false
        func walk(_ node:NSView) { if node.identifier?.rawValue == "cost-partial-evidence" { found = true }; node.subviews.forEach(walk) }
        walk(expanded); XCTAssertTrue(found, "Partial evidence remains available on expansion")
    }
}

extension CostDashboardTests {
    func testPendingHostUsageIsNotAReadFailureOrCompleteCoverage() {
        func detail(kind: String = "relay_host", status: String = "partial", reason: String? = "host_usage_pending",
                    invalid: Int = 0, errors: [String: Int] = [:]) -> CostSourceDetail {
            CostSourceDetail(label: "fixture", kind: kind, status: status, reason: reason,
                filesSeen: 1, filesRead: 1, filesInvalid: invalid, excludedDirectories: 0,
                latestUsageAt: nil, errors: errors, sourceRef: "fixture")
        }
        XCTAssertTrue(detail().hasPendingUsage)
        XCTAssertFalse(detail().hasReadFailure)
        XCTAssertEqual(detail().status, "partial")
        for failed in [detail(kind: "pi"), detail(status: "unreachable"), detail(reason: nil),
                       detail(invalid: 1), detail(errors: ["source_unreadable": 1])] {
            XCTAssertTrue(failed.hasReadFailure)
            XCTAssertFalse(failed.hasPendingUsage)
        }
        XCTAssertEqual(CostText.reason("host_usage_pending"), "执行尚未结束，完整用量待回报")
    }
}


extension CostDashboardTests {
    func testOutcomeFromHarnessReachesGridWithoutTokenOrReviewCounting() throws {
        let doc = try XCTUnwrap(replays()["outcome-dashboard"]?.document)
        let view = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt))
        XCTAssertEqual(view.validOutcomes?.count, 2)
        XCTAssertEqual(view.activeAgentCount, 2)
        XCTAssertEqual(view.groups.count, 2)
        XCTAssertEqual(view.presentationRows.count, 3)
        XCTAssertEqual(view.summary.usage.eventCount, 2)
        XCTAssertEqual(view.groups.map(\.outcomeCount), [1, 1])
        XCTAssertEqual(Set(view.groups.flatMap { $0.outcomes?.refs ?? [] }).count, 1)
        let outputOnly = try XCTUnwrap(view.outcomeOnlyGroups?.first)
        XCTAssertEqual(outputOnly.outcomeCount, 1)
        XCTAssertEqual(outputOnly.usage?.eventCount, 0)
        XCTAssertNil(outputOnly.cost.amount)
        XCTAssertEqual(CostTableColumn.amount.text(outputOnly), "未提供")
        XCTAssertEqual(view.validOutcomes?.items.first?.statusText, "已接受")
        XCTAssertFalse(view.presentationRows.contains { $0.key["wi"] == "wi_20260820_568" })
        // The output column is the kernel-merged PR count from the real publisher, beside the Outcomes.
        XCTAssertEqual(view.validPullRequests?.count, 2)
        XCTAssertEqual(view.presentationRows.map(\.outputText), ["2 个 PR", "2 个 PR", "0 个 PR"])
        XCTAssertEqual(view.presentationRows.first?.pullRequests?.item?.pullRequests.map(\.reviewText),
                       ["审核 92 分 · 1 轮", "审核 88 分 · 2 轮"])
        XCTAssertEqual(doc.pullRequestAnomalies?.headline, "近 7 天 未经内核合并 1 个 · 直接提交 main 1 个")
        let old = try document()
        XCTAssertNil(old.view(period: .today, dimension: .agent, at: old.generatedAt)?.outcomes)
        XCTAssertNotNil(old.view(period: .today, dimension: .agent, at: old.generatedAt)?.summary.cost.amount)
    }

    func testOutcomeColumnSortsNumbersAndUnknownDoesNotBecomeZero() throws {
        let doc = try XCTUnwrap(replays()["outcome-dashboard"]?.document)
        let base = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt)?.groups.first)
        let counts: [Int?] = [2, 10, 0, nil]
        let rows = counts.enumerated().map { i, count -> CostQueryGroup in
            var row = base
            var key = base.key; key["wi"] = "wi_20260916_00\(i)"
            row = CostQueryGroup(key: key, cost: base.cost, usage: base.usage)
            // The output column is the WI's kernel-merged PR count.
            row.pullRequests = CostRowPullRequests(count: count, item: nil)
            return row
        }
        var selection = CostTableSelection(); selection.sortColumn = .outcome
        XCTAssertEqual(selection.rows(rows).map(\.outputCount), [10,2,0,nil])
        selection.ascending = true
        XCTAssertEqual(selection.rows(rows).map(\.outputCount), [0,2,10,nil])
        selection.filters[.outcome] = ">=2"
        XCTAssertEqual(selection.rows(rows).map(\.outputCount), [2,10])
        selection.filters[.outcome] = "0 个 PR"
        XCTAssertEqual(selection.rows(rows).map(\.outputCount), [0])
        selection.filters[.outcome] = "未提供"
        XCTAssertEqual(selection.rows(rows).count, 1)
        XCTAssertNil(selection.rows(rows).first?.outputCount)
        XCTAssertEqual(rows.map(\.outputText), ["2 个 PR", "10 个 PR", "0 个 PR", "未提供"])
    }

    @MainActor
    func testOutcomeGridHeadingsDetailAndFixedWidth() async throws {
        let replay = try XCTUnwrap(replays()["outcome-dashboard"])
        let doc = replay.document
        let store = SnapshotStore(transport: CostReplayTransport([.success(replay.payload)]),
            cache: MemorySnapshotCache(snapshot: replay.snapshot), now: { doc.generatedAt })
        let browser = CostBrowserState()
        let hosting = NSHostingView(rootView: StatsPopoverView(store: store, tablePreferences: nil, costBrowser: browser))
        let window = NSWindow(contentRect: NSRect(x:0,y:0,width:420,height:580),
            styleMask:[.titled],backing:.buffered,defer:false)
        window.contentView = hosting; window.orderFront(nil)
        defer { window.orderOut(nil) }
        await store.refresh()
        try await Task.sleep(nanoseconds:200_000_000)
        hosting.layoutSubtreeIfNeeded()
        func find<T: NSView>(_ root: NSView, _ type: T.Type) -> T? {
            if let value = root as? T { return value }
            return root.subviews.compactMap { find($0, type) }.first
        }
        func ids(_ root: NSView) -> Set<String> {
            Set(([root.identifier?.rawValue].compactMap { $0 }) + root.subviews.flatMap { Array(ids($0)) })
        }
        func render(_ suffix: String) throws {
            guard let folder = Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,
                  !folder.isEmpty, let rep = hosting.bitmapImageRepForCachingDisplay(in:hosting.bounds) else { return }
            try FileManager.default.createDirectory(atPath: folder,withIntermediateDirectories:true)
            hosting.cacheDisplay(in:hosting.bounds,to:rep)
            try rep.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder).appendingPathComponent("outcome-dashboard-420-\(suffix).png"))
        }
        XCTAssertEqual(hosting.fittingSize.width,420,accuracy:1)
        XCTAssertEqual(hosting.fittingSize.height,580,accuracy:1)
        let anchors = ids(hosting)
        for name in ["dashboard-section-heading","outcome-cost-section-heading","dashboard-wi-created","dashboard-merged-prs","global-authority-status","dashboard-codex-quota","dashboard-cc-quota"] {
            XCTAssertTrue(anchors.contains(name),name)
        }
        XCTAssertFalse(anchors.contains("outcome-row-detail"))
        XCTAssertNil(find(hosting, CostGridContainer.self))
        XCTAssertTrue(anchors.contains("cost-compact-preview"))
        XCTAssertEqual(anchors.filter { $0.hasPrefix("cost-preview-row-") }.count,3)
        try render("overview")
        let rows = try XCTUnwrap(doc.activityView(period:.today,at:doc.generatedAt)).presentationRows
        let row = try XCTUnwrap(browser.selection.rows(rows).first)
        browser.select(row)
        try await Task.sleep(nanoseconds:150_000_000)
        hosting.layoutSubtreeIfNeeded()
        XCTAssertTrue(ids(hosting).contains("outcome-row-detail"))
        let selected = browser.selectedRowID
        browser.reconcile(rows)
        XCTAssertEqual(browser.selectedRowID,selected,"A normal refresh preserves the expanded stable row")
        func named(_ node: NSView, _ id: String) -> NSView? {
            if node.identifier?.rawValue == id { return node }
            return node.subviews.compactMap { named($0,id) }.first
        }
        let details = try XCTUnwrap(named(hosting,"outcome-row-detail"))
        details.scrollToVisible(details.bounds)
        hosting.layoutSubtreeIfNeeded()
        try render("detail")
        XCTAssertEqual(hosting.fittingSize.width,420,accuracy:1)
        browser.select(row)
        try await Task.sleep(nanoseconds:100_000_000)
        XCTAssertNil(browser.selectedRowID)
    }
}


extension CostDashboardTests {
    func testInvalidOptionalOutcomeDoesNotEraseIndependentCost() throws {
        let replay = try XCTUnwrap(replays()["outcome-dashboard"])
        let old = try XCTUnwrap(replay.document.activityView(period:.today,at:replay.document.generatedAt))
        for broken: Any in ["malformed-extension", ["status":"complete","count":999]] {
            var snapshot = try JSONSerialization.jsonObject(with:replay.payload) as! [String:Any]
            var doc = snapshot["cost_dashboard"] as! [String:Any]
            var views = doc["views"] as! [[String:Any]]
            for index in views.indices where views[index]["dimension"] as? String == "activity" {
                views[index]["outcomes"] = broken
            }
            doc["views"] = views; snapshot["cost_dashboard"] = doc
            let decoded = try SnapshotDecoder().decode(JSONSerialization.data(withJSONObject:snapshot))
            let changed = try XCTUnwrap(decoded.costDashboard?.activityView(period:.today,at:replay.document.generatedAt))
            XCTAssertNil(changed.validOutcomes)
            XCTAssertEqual(changed.summary.cost,old.summary.cost)
            XCTAssertEqual(changed.presentationRows.count,old.groups.count)
            XCTAssertEqual(decoded.incidents,replay.snapshot.incidents)
            XCTAssertEqual(decoded.alerts,replay.snapshot.alerts)
        }
    }
}


extension CostDashboardTests {
    func testNativeOutcomeReplayRetainsNamesCountsAndIndependentCost() throws {
        guard let replay = try replays()["real-native-outcome-readback"] else {
            throw XCTSkip("Real registered-source replay is enabled by outcome_readback only")
        }
        var foundNativeOutput = false
        for period in CostPeriod.allCases {
            let view = try XCTUnwrap(replay.document.activityView(period:period,at:replay.document.generatedAt))
            let outcomes = try XCTUnwrap(view.validOutcomes, "The real Outcome carrier must decode and validate")
            XCTAssertEqual(outcomes.knownCount,Set(outcomes.items.filter(\.counted).map(\.outcomeRef)).count)
            XCTAssertNotEqual(outcomes.status,"conflict", "Do not pass a source hookup which manufactures Outcome conflicts")
            if let count = outcomes.count, count > 0 {
                foundNativeOutput = true
                XCTAssertTrue(outcomes.items.contains { $0.counted && $0.title != nil })
                XCTAssertTrue(view.presentationRows.contains { ($0.outcomeCount ?? 0) > 0 })
            }
            XCTAssertNotNil(view.summary.cost.amount, "Output availability does not discard the existing priced consumption")
            for row in view.presentationRows {
                for ref in row.outcomes?.refs ?? [] {
                    XCTAssertTrue(outcomes.items.contains { $0.outcomeRef == ref && $0.wi == row.key["wi"] })
                }
            }
        }
        XCTAssertTrue(foundNativeOutput,"At least one real period must carry recorded native Outcomes, not only synthetic data")
    }
}


extension CostDashboardTests {
    @MainActor
    func testNativeOutcomeMonthHasRenderableDataAndExpandableRow() async throws {
        guard let replay = try replays()["real-native-outcome-readback"] else {
            throw XCTSkip("Real source replay requires the explicit readback workflow")
        }
        let view = try XCTUnwrap(replay.document.activityView(period:.month,at:replay.document.generatedAt))
        XCTAssertGreaterThan(view.validOutcomes?.count ?? 0,0)
        let browser = CostBrowserState(period: .month)
        let hosting = NSHostingView(rootView: ScrollView {
            CostDashboardView(document:replay.document,now:replay.document.generatedAt,
                initialPeriod:.month,tablePreferences:nil,browser:browser).padding(14)
        }.frame(width:420,height:580))
        let window = NSWindow(contentRect:NSRect(x:0,y:0,width:420,height:580),
            styleMask:[.titled],backing:.buffered,defer:false)
        window.contentView=hosting;window.orderFront(nil);defer { window.orderOut(nil) }
        try await Task.sleep(nanoseconds:150_000_000);hosting.layoutSubtreeIfNeeded()
        func find<T:NSView>(_ root:NSView,_ type:T.Type)->T? {
            if let v=root as? T { return v }
            return root.subviews.compactMap { find($0,type) }.first
        }
        XCTAssertNil(find(hosting,CostGridContainer.self))
        let rows=browser.selection.rows(view.presentationRows)
        XCTAssertTrue(rows.contains { ($0.outcomeCount ?? 0)>0 })
        XCTAssertEqual(hosting.fittingSize.width,420,accuracy:1)
        if let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,
           !folder.isEmpty,let rep=hosting.bitmapImageRepForCachingDisplay(in:hosting.bounds) {
            hosting.cacheDisplay(in:hosting.bounds,to:rep)
            try rep.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder)
                .appendingPathComponent("real-native-outcome-month-420.png"))
        }
        let row=try XCTUnwrap(rows.first { ($0.outcomeCount ?? 0)>0 })
        browser.select(row)
        try await Task.sleep(nanoseconds:150_000_000)
        XCTAssertEqual(browser.selectedRowID,row.id)
        XCTAssertTrue(row.outcomes?.refs.contains { ref in view.validOutcomes?.items.contains { $0.outcomeRef==ref && $0.title != nil } == true } == true)
    }
}


extension CostDashboardTests {
    @MainActor
    func testQuotaDisclosureRestoresTokensAndHidesResetInCompactPanel() async throws {
        let replay = try XCTUnwrap(replays()["outcome-dashboard"])
        let doc = replay.document
        func identifiers(_ root:NSView) -> [String] {
            [root.identifier?.rawValue].compactMap { $0 } + root.subviews.flatMap(identifiers)
        }
        for expanded in [false,true] {
            let view=CostDashboardView(document:doc,now:doc.generatedAt,initialPeriod:.week,
                initialQuotaExpanded:expanded,initialCCQuotaExpanded:expanded,tablePreferences:nil)
            let host=NSHostingView(rootView:ScrollView { view.padding(14) }.frame(width:420,height:580))
            let window=NSWindow(contentRect:NSRect(x:0,y:0,width:420,height:580),styleMask:[.titled],backing:.buffered,defer:false)
            window.contentView=host;window.orderFront(nil)
            defer { window.orderOut(nil) }
            try await Task.sleep(nanoseconds:150_000_000);host.layoutSubtreeIfNeeded()
            let ids=identifiers(host)
            XCTAssertEqual(ids.filter { $0=="cost-evidence-disclosure" }.count,1)
            XCTAssertEqual(ids.contains("codex-quota-reset-detail"),expanded)
            XCTAssertEqual(ids.contains("codex-quota-today-tokens"),expanded)
            XCTAssertTrue(ids.contains("dashboard-cc-quota"))
            XCTAssertEqual(ids.contains("cc-quota-reset-detail"),expanded)
            XCTAssertEqual(ids.contains("cc-quota-unavailable"),expanded,"no CC reading renders its reason, never 0")
            XCTAssertFalse(ids.contains("cost-partial-evidence"))
            XCTAssertEqual(host.fittingSize.width,420,accuracy:1)
            if let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,
               !folder.isEmpty,let image=host.bitmapImageRepForCachingDisplay(in:host.bounds) {
                host.cacheDisplay(in:host.bounds,to:image)
                try image.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder)
                    .appendingPathComponent(expanded ? "quota-expanded-420.png" : "details-compact-420.png"))
            }
        }
    }
}


extension CostDashboardTests {
    private func automationFixture(_ transform: ((inout [[String: MetricScalar]]) -> Void)? = nil) -> [Metric] {
        let names = [("morning-news","每日早报"),("daily-companion","每日伴读"),("s1-daily","S1 日复盘"),
                     ("wi-patrol","WI 日巡"),("oikos-daily","Oikos 日检"),("s1-weekly","S1 周复盘"),
                     ("s2-weekly","S2 周复盘"),("s3-weekly","S3 周复盘"),("independent-weekly","独立周对账")]
        var rows: [[String: MetricScalar]] = names.map { id, title in
            ["id": .string(id), "title": .string(title), "owner": .string("fixture-owner"),
             "status": .string("未运行 · 待恢复"), "color": .string("gray"),
             "reason": .string("已按用户安排暂停，不修改业务开关"), "expectation": .string("本期结果按约定交付"),
             "plan": .string("0 12 * * * (Asia/Shanghai)"), "progress": .string("当前没有启用中的技术阶段"),
             "source_at": .string("2026-09-18T00:00:00Z"), "expires_at": .string("2026-09-18T00:45:00Z")]
        }
        transform?(&rows)
        return [Metric(id: "automation.tasks",capabilityID: "automation.service_results",status: .available,
                       value: .rows(rows),unit: "tasks",window: "current",quality: .descriptive,scope: "test")]
    }
    func testAutomationPauseActivationAndExpiredSourceAreNotConfused() {
        let now = OikosNode.date("2026-09-18T00:10:00Z")!
        let model = AutomationTasksViewModel(metrics: SnapshotMetrics(automationFixture()),now: now)
        XCTAssertEqual(model.tasks.count,9)
        XCTAssertTrue(model.tasks.allSatisfy { $0.status == "未运行 · 待恢复" && $0.lamp == .gray })
        let changed = automationFixture { rows in rows[1]["status"] = .string("执行失败"); rows[1]["color"] = .string("red") }
        let active = AutomationTasksViewModel(metrics: SnapshotMetrics(changed),now: now)
        XCTAssertEqual(active.tasks[1].lamp,.red)
        XCTAssertEqual(active.tasks.filter { $0.status == "未运行 · 待恢复" }.count,8)
        let expired = AutomationTasksViewModel(metrics: SnapshotMetrics(automationFixture()),now: now.addingTimeInterval(2700))
        XCTAssertEqual(expired.tasks.count,9)
        XCTAssertTrue(expired.tasks.allSatisfy { $0.lamp == .amber && $0.status == "数据待更新" })
    }
    @MainActor
    func testAutomationNativeFoldedAndExpandedAtRealPanelWidth() async throws {
        let model = AutomationTasksViewModel(metrics: SnapshotMetrics(automationFixture()),now: OikosNode.date("2026-09-18T00:10:00Z")!)
        for expanded in [Set<String>(), Set(["daily-companion"])] {
            let host = NSHostingView(rootView: ScrollView {
                AutomationTasksSection(model: model,initiallyExpanded: expanded).padding(14)
            }.frame(width:420,height:580))
            let window=NSWindow(contentRect:NSRect(x:0,y:0,width:420,height:580),styleMask:[.titled],backing:.buffered,defer:false)
            window.contentView=host;window.orderFront(nil)
            defer { window.orderOut(nil) }
            try await Task.sleep(nanoseconds:150_000_000);host.layoutSubtreeIfNeeded()
            func ids(_ view:NSView)->Set<String> {
                Set([view.identifier?.rawValue].compactMap { $0 } + view.subviews.flatMap { Array(ids($0)) })
            }
            let anchors=ids(host)
            for task in model.tasks { XCTAssertTrue(anchors.contains("automation-row-"+task.id)) }
            XCTAssertEqual(anchors.contains("automation-detail-daily-companion"),!expanded.isEmpty)
            XCTAssertFalse(anchors.contains("automation-detail-morning-news"))
            XCTAssertEqual(host.fittingSize.width,420,accuracy:1)
            XCTAssertEqual(host.fittingSize.height,580,accuracy:1)
            if let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,
               !folder.isEmpty,let rep=host.bitmapImageRepForCachingDisplay(in:host.bounds) {
                try FileManager.default.createDirectory(atPath:folder,withIntermediateDirectories:true)
                host.cacheDisplay(in:host.bounds,to:rep)
                try rep.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder).appendingPathComponent("automation-420-\(expanded.isEmpty ? "folded" : "expanded").png"))
            }
        }
    }
    private func groupedAutomationFixture() -> [Metric] {
        automationFixture { rows in
            let groups = [("content","资讯交付",0),("content","资讯交付",0),("reports","报告与复盘",3),("patrol","巡检与异常处理",1),
                          ("patrol","巡检与异常处理",1),("reports","报告与复盘",3),("reports","报告与复盘",3),("reports","报告与复盘",3),
                          ("patrol","巡检与异常处理",1)]
            for (i, group) in groups.enumerated() {
                rows[i]["category"] = .string(group.0); rows[i]["category_title"] = .string(group.1)
                rows[i]["category_rank"] = .number(Double(group.2))
                rows[i]["plan_head"] = .string("计划未定"); rows[i]["plan_sub"] = .string("未部署 · 暂停")
                rows[i]["node"] = .string("未运行")
                rows[i]["coverage"] = .string("已读 3/3 个节点的任务来源")
            }
            rows[0].merge(["status": .string("文档已送达"), "color": .string("green"), "node": .string("oracle-node-1"),
                           "plan_head": .string("每日"), "plan_sub": .string("06:15"),
                           "last_result_at": .string("2026-09-17T22:17:00Z")]) { $1 }
            rows[1].merge(["status": .string("执行中"), "color": .string("orange"), "node": .string("oracle-node-1")]) { $1 }
            rows[4].merge(["status": .string("执行失败"), "color": .string("red"), "node": .string("ownermac,tcloud"),
                           "attention": .boolean(true), "note": .string("另有 2 轮历史问题未核销")]) { $1 }
            rows.append(["id": .string("job:ownermac:ken-heartbeat"), "title": .string("Ken 心跳"), "owner": .string("ken"),
                         "status": .string("结果待核验"), "color": .string("orange"), "node": .string("ownermac"),
                         "source_at": .string("2026-09-18T00:00:00Z"), "expires_at": .string("2026-09-18T00:45:00Z")])
        }
    }
    func testAutomationGroupsByPurposeWithTwoLineColumnsAndGrayIdle() {
        let model = AutomationTasksViewModel(metrics: SnapshotMetrics(groupedAutomationFixture()), now: OikosNode.date("2026-09-18T00:10:00Z")!)
        XCTAssertEqual(model.groups.map(\.title), ["资讯交付", "巡检与异常处理", "报告与复盘", "用途待确认"])
        XCTAssertEqual(model.groups.map { $0.tasks.count }, [2, 3, 4, 1])
        XCTAssertEqual(model.purposeCount, 3)
        XCTAssertEqual(model.groups[1].summary, "3 项 · 1 失败 · 2 未运行")
        XCTAssertEqual(model.summary, "1 项失败 · 1 项待核对 · 1 项执行中 · 6 项未运行")
        XCTAssertEqual(model.coverage, "已读 3/3 个节点的任务来源")
        XCTAssertEqual(model.history.map(\.id), ["oikos-daily"])
        let morning = model.tasks[0]
        XCTAssertEqual(morning.nodeLine, "oracle-node-1")
        XCTAssertEqual(morning.lastResultLines.0, "09/18"); XCTAssertEqual(morning.lastResultLines.1, "06:17")
        XCTAssertEqual(morning.planHead, "每日"); XCTAssertEqual(morning.planSub, "06:15")
        XCTAssertEqual(model.tasks[4].nodeLine, "ownermac / tcloud")
        XCTAssertEqual(model.tasks[2].nodeLine, "节点待定")
        XCTAssertEqual(model.tasks[2].lastResultLines.0, "—")
        // Not running is gray text, never the orange of a pending check.
        XCTAssertEqual(model.tasks[2].lamp, .gray)
        XCTAssertEqual(AutomationTasksSection.textColor(.gray), .secondary)
        XCTAssertEqual(AutomationTasksSection.textColor(.amber), .orange)
        // An exporter without groups keeps every row, under「用途待确认」.
        let legacy = AutomationTasksViewModel(metrics: SnapshotMetrics(automationFixture()), now: OikosNode.date("2026-09-18T00:10:00Z")!)
        XCTAssertEqual(legacy.groups.map(\.title), ["用途待确认"]); XCTAssertEqual(legacy.groups[0].tasks.count, 9)
        XCTAssertEqual(legacy.tasks[0].planHead, "0 12 * * *"); XCTAssertEqual(legacy.tasks[0].planSub, "Asia/Shanghai")
    }
    @MainActor
    func testAutomationRefreshKeepsOpenRowsAndClosedGroups() async throws {
        final class Box { var value = AutomationDisclosure() }
        let box = Box()
        let binding = Binding(get: { box.value }, set: { box.value = $0 })
        let now = OikosNode.date("2026-09-18T00:10:00Z")!
        func render(_ metrics: [Metric], _ name: String) async throws -> Set<String> {
            let model = AutomationTasksViewModel(metrics: SnapshotMetrics(metrics), now: now)
            let host = NSHostingView(rootView: ScrollView {
                AutomationTasksSection(model: model, disclosure: binding).padding(14)
            }.frame(width: 420, height: 580))
            let window = NSWindow(contentRect: NSRect(x:0,y:0,width:420,height:580), styleMask:[.titled], backing:.buffered, defer:false)
            window.contentView = host; window.orderFront(nil); defer { window.orderOut(nil) }
            try await Task.sleep(nanoseconds: 150_000_000); host.layoutSubtreeIfNeeded()
            func ids(_ view: NSView) -> Set<String> {
                Set([view.identifier?.rawValue].compactMap { $0 } + view.subviews.flatMap { Array(ids($0)) })
            }
            XCTAssertEqual(host.fittingSize.width, 420, accuracy: 1)
            if let folder = Bundle.main.object(forInfoDictionaryKey: "PulseTestRenderDirectory") as? String,
               !folder.isEmpty, let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
                try FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
                host.cacheDisplay(in: host.bounds, to: rep)
                try rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: folder).appendingPathComponent("automation-groups-\(name).png"))
            }
            return ids(host)
        }
        var anchors = try await render(groupedAutomationFixture(), "default")
        XCTAssertTrue(anchors.isSuperset(of: ["automation-columns", "automation-summary", "automation-history",
            "automation-group-content", "automation-group-patrol", "automation-group-reports", "automation-group-ungrouped"]))
        XCTAssertTrue(anchors.contains("automation-row-morning-news"), "groups start open")
        XCTAssertFalse(anchors.contains("automation-detail-morning-news"), "rows start folded")
        box.value.expandedTasks.insert("morning-news"); box.value.collapsedGroups.insert("reports")
        var refreshed = groupedAutomationFixture()
        if case var .rows(rows) = refreshed[0].value {
            rows[0]["status"] = .string("已送达"); rows[0]["source_at"] = .string("2026-09-18T00:05:00Z")
            refreshed = [Metric(id: "automation.tasks", capabilityID: "automation.service_results", status: .available,
                                value: .rows(rows), unit: "tasks", window: "current", quality: .descriptive, scope: "test")]
        }
        anchors = try await render(refreshed, "refreshed")
        XCTAssertTrue(anchors.contains("automation-detail-morning-news"), "an opened row stays open after a refresh")
        XCTAssertFalse(anchors.contains("automation-row-s1-daily"), "a closed group stays closed after a refresh")
        XCTAssertTrue(anchors.contains("automation-group-reports"))
    }
    @MainActor
    func testOikosInsertedBetweenSummaryAndAuthorityAndInitiallyCollapsed() async throws {
        let doc=try document()
        for expanded in [false,true] {
            let host=NSHostingView(rootView:ScrollView {
                CostDashboardView(document:doc,now:doc.generatedAt,initialOikosExpanded:expanded).padding(14)
            }.frame(width:420,height:580))
            let window=NSWindow(contentRect:NSRect(x:0,y:0,width:420,height:580),styleMask:[.titled],backing:.buffered,defer:false)
            window.contentView=host;window.orderFront(nil)
            defer { window.orderOut(nil) }
            try await Task.sleep(nanoseconds:150_000_000);host.layoutSubtreeIfNeeded()
            func find(_ view:NSView,_ id:String)->NSView? {
                if view.identifier?.rawValue==id { return view }
                return view.subviews.compactMap { find($0,id) }.first
            }
            let summary=try XCTUnwrap(find(host,"dashboard-platform-state"))
            let oikos=try XCTUnwrap(find(host,"oikos-overview"))
            let authority=try XCTUnwrap(find(host,"global-authority-status"))
            let ys=[summary,oikos,authority].map { $0.convert($0.bounds,to:host).midY }
            XCTAssertTrue((ys[0]<ys[1] && ys[1]<ys[2]) || (ys[0]>ys[1] && ys[1]>ys[2]))
            XCTAssertEqual(find(host,"oikos-resource-details") != nil,expanded)
            XCTAssertEqual(host.fittingSize.width,420,accuracy:1)
        }
    }
}


extension CostDashboardTests {
    func testPresentationKeepsSubcentAndUnknownSeparateFromZero() {
        XCTAssertEqual(StatsFormat.usdDecimal(Decimal.zero), "$0.00")
        XCTAssertEqual(StatsFormat.usdDecimal(Decimal(string: "0.0000001")), "<$0.01")
        XCTAssertEqual(StatsFormat.usdDecimal(Decimal(string: "0.01")), "$0.01")
        XCTAssertEqual(StatsFormat.usdDecimal(nil), "未知")
        for status in ["unknown", "unavailable", "future-state"] {
            let source = CostSourceDetail(label: "fixture", kind: "native", status: status, reason: nil,
                filesSeen: 0, filesRead: 0, filesInvalid: 0, excludedDirectories: 0, latestUsageAt: nil, errors: [:], sourceRef: "ref")
            XCTAssertEqual(source.readStatusText, "读取情况待核验")
        }
    }

    @MainActor
    func testEveryNativeColumnIsVerticallyCenteredAfterReuseAndResize() async throws {
        let replay = try XCTUnwrap(replays()["outcome-dashboard"])
        let rows = try XCTUnwrap(replay.document.activityView(period: .today, at: replay.document.generatedAt)).presentationRows
        let grid = CostGridContainer(frame: NSRect(x: 0, y: 0, width: 760, height: 350))
        let controller = CostActivityTable.Coordinator(preferences: nil)
        controller.connect(grid); controller.update(rows, scope: "today")
        let window = NSWindow(contentRect: grid.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = grid; window.orderFront(nil); defer { window.orderOut(nil) }
        try await Task.sleep(nanoseconds: 150_000_000)
        for height in [CGFloat(28), 34, 46] {
            grid.table.rowHeight = height
            grid.table.reloadData(); grid.layoutSubtreeIfNeeded()
            for index in CostTableColumn.allCases.indices {
                grid.table.scrollColumnToVisible(index)
                grid.layoutSubtreeIfNeeded()
                let cell = try XCTUnwrap(grid.table.view(atColumn: index, row: 0, makeIfNecessary: true))
                cell.layoutSubtreeIfNeeded()
                let label: NSTextField
                if CostTableColumn.allCases[index] == .agent { label = try XCTUnwrap((cell as? CostAgentCell)?.label) }
                else { label = try XCTUnwrap((cell as? CostTextCell)?.label) }
                XCTAssertFalse(label.stringValue.isEmpty)
                XCTAssertEqual(label.frame.midY, cell.bounds.midY, accuracy: 0.5, "column \(index)")
                XCTAssertEqual(label.frame.height, 18, accuracy: 0.5)
                XCTAssertGreaterThan(label.frame.width, 0)
                XCTAssertTrue(label.isSelectable)
                if CostTableColumn.allCases[index] != .agent { XCTAssertEqual(label.alignment, [.outcome,.amount].contains(CostTableColumn.allCases[index]) ? .right : .left) }
            }
        }
        XCTAssertFalse(grid.table.gridStyleMask.contains(.solidVerticalGridLineMask))
        let scroll = try XCTUnwrap(grid.table.enclosingScrollView)
        XCTAssertTrue(scroll.autohidesScrollers)
        XCTAssertLessThanOrEqual(grid.table.rect(ofColumn: 3).maxX, 760)
        let first = try XCTUnwrap(controller.visible.first)
        controller.select(first)
        let selected = controller.selectedID
        controller.update(rows, scope: "today")
        XCTAssertEqual(controller.selectedID, selected)
        XCTAssertEqual(grid.table.selectedRowIndexes.count, 1)
        controller.selection.filters[.agent] = "no-such-agent"
        controller.refresh()
        XCTAssertTrue(controller.visible.isEmpty)
        XCTAssertNil(controller.selectedID)
        controller.clearFilters()
        XCTAssertEqual(controller.visible.count, rows.filter(\.hasActivity).count)
    }

    @MainActor
    func testAllCardsAndDetailsHaveOneNarrowReadingGridInBothAppearances() async throws {
        let replay = try XCTUnwrap(replays()["outcome-dashboard"])
        let doc = replay.document
        let now = OikosNode.date("2026-09-18T00:10:00Z")!
        let tasks = AutomationTasksViewModel(metrics: SnapshotMetrics(automationFixture()), now: now)
        for dark in [false, true] {
            for expanded in [false, true] {
                let content = ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        CostDashboardView(document: doc, now: doc.generatedAt,
                            initialEvidenceExpanded: expanded, initialQuotaExpanded: expanded,
                            tablePreferences: nil)
                        AutomationTasksSection(model: tasks, initiallyExpanded: expanded ? ["daily-companion"] : [])
                    }.padding(14)
                }.frame(width: 420, height: 580)
                    .background(Color(nsColor: .windowBackgroundColor))
                    .environment(\.colorScheme, dark ? .dark : .light)
                let host = NSHostingView(rootView: content)
                let window = NSWindow(contentRect: NSRect(x:0,y:0,width:420,height:580), styleMask:[.titled],backing:.buffered,defer:false)
                window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
                window.contentView=host; window.orderFront(nil); defer { window.orderOut(nil) }
                try await Task.sleep(nanoseconds:200_000_000);host.layoutSubtreeIfNeeded()
                func find(_ view:NSView,_ id:String)->NSView? {
                    if view.identifier?.rawValue == id { return view }
                    return view.subviews.compactMap { find($0,id) }.first
                }
                let dashboard=try XCTUnwrap(find(host,"stats-card-dashboard"))
                let cost=try XCTUnwrap(find(host,"stats-card-cost"))
                let automation=try XCTUnwrap(find(host,"automation-section"))
                let boxes=[dashboard,cost,automation].map { $0.convert($0.bounds,to:host) }
                for rect in boxes {
                    XCTAssertEqual(rect.minX,14,accuracy:1)
                    XCTAssertEqual(rect.width,392,accuracy:1)
                }
                XCTAssertEqual(find(host,"cost-unassigned-scale") != nil,expanded)
                XCTAssertEqual(find(host,"cost-effective-usage-time") != nil,expanded)
                if expanded {
                    let detail=try XCTUnwrap(find(host,"automation-detail-daily-companion"))
                    let row=try XCTUnwrap(find(host,"automation-row-daily-companion"))
                    XCTAssertEqual(detail.convert(detail.bounds,to:host).minX,row.convert(row.bounds,to:host).minX+16,accuracy:1)
                }
                XCTAssertEqual(host.fittingSize.width,420,accuracy:1)
                if let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String, !folder.isEmpty {
                    func capture(_ suffix:String) throws {
                        guard let rep=host.bitmapImageRepForCachingDisplay(in:host.bounds) else { return XCTFail("no native render") }
                        host.cacheDisplay(in:host.bounds,to:rep)
                        try rep.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder)
                            .appendingPathComponent("polish-\(dark ? "dark" : "light")-\(expanded ? "expanded" : "folded")-\(suffix).png"))
                    }
                    try capture("top")
                    automation.scrollToVisible(automation.bounds)
                    host.layoutSubtreeIfNeeded(); try await Task.sleep(nanoseconds:150_000_000)
                    try capture("tasks")
                }
            }
        }
    }
}


extension CostDashboardTests {
    @MainActor
    func testRequestsHideOriginalDetailsWithoutLosingThem() async throws {
        let item = try XCTUnwrap(InFlightWorkItem(row: [
            "work_item_id": .string("wi_fixture"), "title": .string("在办事项：需要完整保留的业务目标"),
            "owner": .string("pulse"), "status": .string("in_progress"), "days": .number(365), "open_days": .number(12),
            "workflow_profile": .string("lightweight")]))
        let alert = TriagedAlert(Alert(eventID: "request", projectionStatus: "event_only", severity: .warning,
            capabilityID: "notify.action_package", title: "需要确认下一步：标题可以很长但不应撑大卡片",
            occurredAt: Date(timeIntervalSince1970: 100), reason: "pulse 名下 · 挂 12 天 · wi_20260906_779｜原始请求全文不被摘要改写。", tier: .act, workItemID: item.workItemID), workItem: item)
        for expanded in [false, true] {
            let host = NSHostingView(rootView: ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    StatsCard {
                        StatsSectionHeading(lamp: .amber, title: "要你出手", count: "1 项")
                        ActAlertCard(alert: alert, initiallyExpanded: expanded)
                    }
                }.padding(14)
            }.frame(width:420,height:580).background(Color(nsColor:.windowBackgroundColor)))
            let window=NSWindow(contentRect:NSRect(x:0,y:0,width:420,height:580),styleMask:[.titled],backing:.buffered,defer:false)
            window.appearance=NSAppearance(named:.aqua); window.contentView=host; window.orderFront(nil)
            defer { window.orderOut(nil) }
            try await Task.sleep(nanoseconds:150_000_000);host.layoutSubtreeIfNeeded()
            func find(_ view:NSView,_ id:String)->NSView? {
                if view.identifier?.rawValue == id { return view }
                return view.subviews.compactMap { find($0,id) }.first
            }
            for prefix in ["act"] {
                let id = "request"
                let row = try XCTUnwrap(find(host, prefix+"-row-"+id))
                let detail = find(host, prefix+"-detail-"+id)
                let summary = try XCTUnwrap(find(host,prefix+"-summary-"+id))
                let subtitle = try XCTUnwrap(find(host,prefix+"-title-"+id))
                let first = summary.convert(summary.bounds,to:host)
                let second = subtitle.convert(subtitle.bounds,to:host)
                XCTAssertTrue(host.isFlipped ? first.midY < second.midY : first.midY > second.midY)
                XCTAssertEqual(first.minX,second.minX,accuracy:1)
                XCTAssertEqual(detail != nil, expanded)
                let badge=try XCTUnwrap(find(host,prefix+"-status-age-"+id))
                let bounds=badge.convert(badge.bounds,to:host)
                XCTAssertGreaterThanOrEqual(bounds.minX,first.minX)
                XCTAssertLessThanOrEqual(bounds.maxX,406)
                XCTAssertNotNil(find(host,prefix+"-workflow-profile-"+id))
                if let detail {
                    XCTAssertEqual(detail.convert(detail.bounds,to:host).minX,
                        row.convert(row.bounds,to:host).minX+16, accuracy:1)
                }
            }
            XCTAssertEqual(host.fittingSize.width,420,accuracy:1)
            if let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,!folder.isEmpty,
               let rep=host.bitmapImageRepForCachingDisplay(in:host.bounds) {
                host.cacheDisplay(in:host.bounds,to:rep)
                try rep.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder)
                    .appendingPathComponent("polish-requests-\(expanded ? "expanded" : "folded").png"))
            }
        }
        XCTAssertEqual(alert.body,"原始请求全文不被摘要改写。")
        XCTAssertEqual(item.days,365)
    }
}


extension CostDashboardTests {
    func testAbsentSourceDetailsDoNotClaimThereAreZeroSources() throws {
        let doc = try document()
        var usage = try XCTUnwrap(doc.views.first).summary.usage
        usage.sourceDetails = nil
        XCTAssertEqual(usage.sourceDetailsTitle, "数据源 · 明细未提供")
        usage.sourceDetails = []
        XCTAssertEqual(usage.sourceDetailsTitle, "数据源 · 明细未提供")
        usage.sourceDetails = [CostSourceDetail(label: "fixture", kind: "native", status: "unknown", reason: nil,
            filesSeen: 0, filesRead: 0, filesInvalid: 0, excludedDirectories: 0, latestUsageAt: nil, errors: [:], sourceRef: "ref")]
        XCTAssertEqual(usage.sourceDetailsTitle, "数据源 · 1 项")
        XCTAssertEqual(usage.sourceDetails?.first?.readStatusText, "读取情况待核验")
    }
}


extension CostDashboardTests {
    @MainActor
    func testCurrentOperationIsIndependentOfReportingPeriodAndAuthority() throws {
        let now = Date()
        func context(_ health: RuntimeHealth, at: Date?, incidents: [Incident] = []) -> DashboardStatusContext {
            DashboardStatusContext(authority: GlobalAuthorityViewModel(projection: nil),
                relay: RelayViewModel(metrics: SnapshotMetrics([]), hasSnapshot: true, at: now),
                harness: HarnessViewModel(metrics: SnapshotMetrics([]), hasSnapshot: true),
                health: health, incidents: incidents, observedAt: at)
        }
        let browser = CostBrowserState()
        for period in CostPeriod.allCases {
            browser.period = period
            let status = context(.ok, at: now).currentStatus(now: now)
            XCTAssertEqual(status.value,"正常"); XCTAssertEqual(status.lamp,.green)
        }
        for health in [RuntimeHealth.unknown, .stale] {
            XCTAssertNotEqual(context(health,at:now).currentStatus(now:now).lamp,.green)
        }
        for time in [now.addingTimeInterval(1), now.addingTimeInterval(-2700)] {
            XCTAssertEqual(context(.ok,at:time).currentStatus(now:now).value,"待更新")
        }
        XCTAssertEqual(context(.critical,at:now).currentStatus(now:now).lamp,.red)
        XCTAssertEqual(context(.warning,at:now).currentStatus(now:now).lamp,.amber)
        let missing = OikosPlatformViewModel(metrics: SnapshotMetrics([]), now: now)
        XCTAssertNotEqual(context(.ok,at:now).currentStatus(now:now,resources:missing).lamp,.green)
        func event(_ state: String) throws -> Incident {
            var object: [String:Any] = ["incidentKey":"example", "episodeID":"example-1", "state":state,
                "severity":"warning", "capabilityID":"runtime", "scope":"test", "title":"observed", "whatHappened":"test",
                "impact":"test", "suggestedAction":"test", "openedAt":now.addingTimeInterval(-3600).timeIntervalSinceReferenceDate,
                "updatedAt":now.timeIntervalSinceReferenceDate, "occurrenceCount":1,"reopenCount":0,
                "evidence":["failure"],"recoveryEvidence":state == "resolved" ? ["success"] : []]
            if state == "resolved" { object["resolvedAt"] = now.addingTimeInterval(-1).timeIntervalSinceReferenceDate }
            return try JSONDecoder().decode(Incident.self,from:JSONSerialization.data(withJSONObject:object))
        }
        XCTAssertEqual(context(.ok,at:now,incidents:[try event("resolved")]).currentStatus(now:now).value,"正常")
        XCTAssertEqual(context(.ok,at:now,incidents:[try event("open")]).currentStatus(now:now).lamp,.amber)
    }

    @MainActor
    func testPreviewAndFullWindowShareSelectionAndNeverFetchOnOpen() async throws {
        let replay=try XCTUnwrap(replays()["activity-many"])
        let transport=CostCountingTransport(payload:replay.payload)
        let sampledAt=replay.document.generatedAt
        let store=SnapshotStore(transport:transport,cache:MemorySnapshotCache(),now:{sampledAt})
        await store.refresh()
        let browser=CostBrowserState(period:.week)
        let rows=try XCTUnwrap(replay.document.activityView(period:.week,at:replay.document.generatedAt)).presentationRows
        let host=NSHostingView(rootView:ScrollView {
            CostDashboardView(document:replay.document,now:replay.document.generatedAt,tablePreferences:nil,
                browser:browser,onOpenDetails:{browser.openDetails(store:store,preferences:nil)}).padding(14)
        }.frame(width:420,height:580))
        let previewWindow=NSWindow(contentRect:NSRect(x:0,y:0,width:420,height:580),styleMask:[.titled],backing:.buffered,defer:false)
        previewWindow.contentView=host;previewWindow.orderFront(nil)
        defer { previewWindow.orderOut(nil);browser.window?.close() }
        try await Task.sleep(nanoseconds:100_000_000);host.layoutSubtreeIfNeeded()
        func ids(_ v:NSView)->[String] { [v.identifier?.rawValue].compactMap{$0}+v.subviews.flatMap(ids) }
        func grid(_ v:NSView)->CostGridContainer? { (v as? CostGridContainer) ?? v.subviews.compactMap(grid).first }
        XCTAssertNil(grid(host))
        XCTAssertEqual(ids(host).filter{$0.hasPrefix("cost-preview-row-")}.count,min(5,browser.selection.rows(rows).count))
        let selected=try XCTUnwrap(browser.selection.rows(rows).first)
        browser.select(selected)
        browser.openDetails(store:store,preferences:nil)
        let window=try XCTUnwrap(browser.window);let full=try XCTUnwrap(window.contentView)
        try await Task.sleep(nanoseconds:120_000_000);full.layoutSubtreeIfNeeded()
        let table=try XCTUnwrap(grid(full));let coordinator=try XCTUnwrap(table.table.dataSource as? CostActivityTable.Coordinator)
        XCTAssertEqual(coordinator.selectedID,selected.id)
        XCTAssertEqual(coordinator.source,rows)
        XCTAssertEqual(table.table.numberOfColumns,9)
        coordinator.selection.filters[.agent]="no-match";coordinator.onQueryChange?(coordinator.selection);coordinator.refresh()
        try await Task.sleep(nanoseconds:100_000_000)
        XCTAssertEqual(browser.selection.filters[.agent],"no-match")
        XCTAssertNil(browser.selectedRowID)
        XCTAssertEqual(ids(host).filter{$0.hasPrefix("cost-preview-row-")}.count,0)
        coordinator.clearFilters()
        browser.select(selected)
        try await Task.sleep(nanoseconds:100_000_000);full.layoutSubtreeIfNeeded()
        if let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,!folder.isEmpty,
           let image=full.bitmapImageRepForCachingDisplay(in:full.bounds) {
            full.cacheDisplay(in:full.bounds,to:image)
            try image.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder).appendingPathComponent("full-details-window.png"))
        }
        window.close();browser.openDetails(store:store,preferences:nil)
        XCTAssertTrue(browser.window === window,"Reuse one window, not a new data source per click")
        browser.period = .today
        XCTAssertTrue(browser.selection.filters.isEmpty);XCTAssertNil(browser.selectedRowID)
        let fetches=await transport.calls
        XCTAssertEqual(fetches,1,"Opening/reopening the detail window must not fetch or reprice")
        XCTAssertEqual(store.snapshot?.costDashboard,replay.document)
    }

    @MainActor
    func testNativeBodyCannotPaintOverHeaderAcrossCombinedInteractions() async throws {
        let replay=try XCTUnwrap(replays()["activity-many"])
        let base=try XCTUnwrap(replay.document.activityView(period:.week,at:replay.document.generatedAt)?.presentationRows.first)
        let rows=(0..<80).map { i -> CostQueryGroup in
            var key=base.key;key["wi"]="wi_20260918_\(String(format:"%03d",i))"
            return CostQueryGroup(key:key,cost:base.cost,usage:base.usage)
        }
        for dark in [false,true] {
            let grid=CostGridContainer(frame:NSRect(x:0,y:0,width:980,height:500))
            let coordinator=CostActivityTable.Coordinator(preferences:nil);coordinator.connect(grid);coordinator.update(rows,scope:"all")
            let window=NSWindow(contentRect:grid.frame,styleMask:[.titled,.resizable],backing:.buffered,defer:false)
            window.appearance=NSAppearance(named:dark ? .darkAqua : .aqua);window.contentView=grid;window.orderFront(nil)
            defer {window.orderOut(nil)}
            try await Task.sleep(nanoseconds:100_000_000);grid.layoutSubtreeIfNeeded()
            let scroll=try XCTUnwrap(grid.table.enclosingScrollView)
            XCTAssertTrue(scroll.contentView.clipsToBounds)
            XCTAssertTrue(grid.filters.isOpaque);XCTAssertTrue(grid.filters.clipsToBounds)
            func headerImage() throws -> Data {
                grid.layoutSubtreeIfNeeded();grid.displayIfNeeded()
                let image=try XCTUnwrap(grid.bitmapImageRepForCachingDisplay(in:grid.bounds))
                grid.cacheDisplay(in:grid.bounds,to:image)
                let rect=grid.headerClip.convert(grid.headerClip.bounds,to:grid)
                let sx=CGFloat(image.pixelsWide)/grid.bounds.width, sy=CGFloat(image.pixelsHigh)/grid.bounds.height
                let crop=CGRect(x:rect.minX*sx,y:(grid.bounds.height-rect.maxY)*sy,width:rect.width*sx,height:rect.height*sy).integral
                let cg=try XCTUnwrap(image.cgImage?.cropping(to:crop))
                return try XCTUnwrap(NSBitmapImageRep(cgImage:cg).representation(using:.png,properties:[:]))
            }
            var selectedIndex = 0
            for width in [CGFloat(760),1000] {
                window.setContentSize(NSSize(width:width,height:500))
                for filters in [false,true,false] {
                    grid.setFiltersVisible(filters);grid.layoutSubtreeIfNeeded()
                    try await Task.sleep(nanoseconds:100_000_000)
                    grid.layoutSubtreeIfNeeded()
                    let header=grid.headerClip.convert(grid.headerClip.bounds,to:grid)
                    let body=scroll.contentView.convert(scroll.contentView.bounds,to:grid)
                    XCTAssertFalse(header.insetBy(dx:1,dy:1).intersects(body),"Header and body occupy disjoint viewports")
                    // Do not reset x here: scrollColumnToVisible(0) may retain
                    // a native inset. Vertical-only motion must keep that x.
                    let before=try headerImage()
                    for y in [CGFloat(80),800,2000,0] {
                        let clip=scroll.contentView
                        clip.scroll(to:clip.constrainBoundsRect(NSRect(origin:NSPoint(x:clip.bounds.minX,y:y),size:clip.bounds.size)).origin)
                        scroll.reflectScrolledClipView(clip);grid.layoutSubtreeIfNeeded()
                        let after = try headerImage()
                        if after != before, let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,!folder.isEmpty {
                            let name="header-\(dark ? "dark" : "light")-\(Int(width))-\(filters)-\(Int(y))"
                            try before.write(to:URL(fileURLWithPath:folder).appendingPathComponent(name+"-before.png"))
                            try after.write(to:URL(fileURLWithPath:folder).appendingPathComponent(name+"-after.png"))
                        }
                        XCTAssertEqual(after,before,"Body repaint must not leak into the visible header viewport")
                    }
                    grid.table.tableColumns[1].width=900
                    grid.table.scrollColumnToVisible(7);grid.layoutSubtreeIfNeeded()
                    let headerClip=try XCTUnwrap(grid.filters.superview as? NSClipView)
                    XCTAssertEqual(headerClip.bounds.minX,scroll.contentView.bounds.minX,accuracy:1)
                    let chosen = rows[selectedIndex]; selectedIndex += 1
                    coordinator.select(chosen);coordinator.update(rows,scope:"all")
                    XCTAssertEqual(coordinator.selectedID,chosen.id)
                    grid.table.tableColumns[1].width=130
                    grid.table.scrollColumnToVisible(0);grid.layoutSubtreeIfNeeded()
                }
            }
            if let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,!folder.isEmpty,
               let image=grid.bitmapImageRepForCachingDisplay(in:grid.bounds) {
                grid.cacheDisplay(in:grid.bounds,to:image)
                try image.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder).appendingPathComponent("full-window-scroll-\(dark ? "dark" : "light").png"))
            }
        }
    }
}

private actor CostCountingTransport: SnapshotTransport {
    let payload:Data
    private(set) var calls=0
    init(payload:Data){self.payload=payload}
    func fetch() async throws -> Data {calls+=1;return payload}
}


extension CostDashboardTests {
    @MainActor
    func testDetachedHeaderStillHandlesActualMouseSortingAndColumnDrag() async throws {
        let doc=try XCTUnwrap(replays()["activity-today"]?.document)
        let rows=try XCTUnwrap(doc.activityView(period:.week,at:doc.generatedAt)).presentationRows
        let grid=CostGridContainer(frame:NSRect(x:0,y:0,width:940,height:400))
        let controller=CostActivityTable.Coordinator(preferences:nil);controller.connect(grid);controller.update(rows,scope:"week")
        grid.setFiltersVisible(false)
        let window=NSWindow(contentRect:grid.frame,styleMask:[.titled,.resizable],backing:.buffered,defer:false)
        window.contentView=grid;window.makeKeyAndOrderFront(nil);defer {window.orderOut(nil)}
        try await Task.sleep(nanoseconds:100_000_000);grid.layoutSubtreeIfNeeded()
        func event(_ type:NSEvent.EventType,_ point:NSPoint) throws -> NSEvent {
            try XCTUnwrap(NSEvent.mouseEvent(with:type,location:grid.filters.convert(point,to:nil),modifierFlags:[],
                timestamp:ProcessInfo.processInfo.systemUptime,windowNumber:window.windowNumber,context:nil,
                eventNumber:0,clickCount:1,pressure:type == .leftMouseUp ? 0 : 1))
        }
        let agentIndex = try XCTUnwrap(CostTableColumn.allCases.firstIndex(of: .agent))
        let first=grid.filters.headerRect(ofColumn:agentIndex)
        let centre=NSPoint(x:first.midX,y:10)
        NSApp.postEvent(try event(.leftMouseUp,centre),atStart:false)
        grid.filters.mouseDown(with:try event(.leftMouseDown,centre))
        XCTAssertEqual(controller.selection.sortColumn,.agent,"User clicking the detached heading still sorts the original table")
        let column=grid.table.tableColumns[1];let old=column.width
        let edge=NSPoint(x:grid.filters.headerRect(ofColumn:1).maxX-1,y:10)
        let dragged=NSPoint(x:edge.x+70,y:edge.y)
        NSApp.postEvent(try event(.leftMouseDragged,dragged),atStart:false)
        NSApp.postEvent(try event(.leftMouseUp,dragged),atStart:false)
        grid.filters.mouseDown(with:try event(.leftMouseDown,edge))
        grid.layoutSubtreeIfNeeded()
        XCTAssertGreaterThan(column.width,old+30,"Native edge dragging must resize, not just programmatic column assignment")
        XCTAssertFalse(grid.headerClip.frame.intersects(try XCTUnwrap(grid.table.enclosingScrollView).frame))
    }
}


extension CostDashboardTests {
    @MainActor
    func testStackedNativeMenuPreservesQuotaAndFitsBothAppearances() async throws {
        for dark in [false,true] {
            let sample = try SnapshotDecoder().decode(try fixtureData("live-v2a"))
            let model=MenuBarViewModel(metrics:SnapshotMetrics(sample),alertTiers:AlertTiers(act:12,watch:0,done:0),hasSnapshot:true,
                presentation:StatusPresentation(runtimeHealth:.ok,coverage:.unknown),requestSourceCurrent:true)
            XCTAssertEqual(model.menuQuotaText,"98%")
            XCTAssertEqual(model.menuWIText,"12")
            XCTAssertTrue(model.menuAccessibilityLabel.contains("要你出手 12 项"))
            let native = MenuBarArtwork.image(for: model)
            XCTAssertTrue(native.isTemplate)
            XCTAssertEqual(native.size.height,22)
            let item = NSStatusBar.system.statusItem(withLength: native.size.width)
            defer { NSStatusBar.system.removeStatusItem(item) }
            let button = try XCTUnwrap(item.button)
            button.image = native
            button.imagePosition = .imageOnly
            button.layoutSubtreeIfNeeded()
            XCTAssertEqual(button.image?.size.height,22)
            XCTAssertGreaterThanOrEqual(button.bounds.height,22)
            XCTAssertGreaterThanOrEqual(button.bounds.width,native.size.width)
            XCTAssertTrue(model.menuAccessibilityLabel.contains("98%"))
            let host=NSHostingView(rootView:PulseMenuBarLabel(model:model).padding(4)
                .background(Color(nsColor:.windowBackgroundColor)).environment(\.colorScheme,dark ? .dark : .light))
            let window=NSWindow(contentRect:NSRect(x:0,y:0,width:180,height:30),styleMask:[.borderless],backing:.buffered,defer:false)
            window.appearance=NSAppearance(named:dark ? .darkAqua : .aqua)
            window.contentView=host;window.orderFront(nil);defer { window.orderOut(nil) }
            try await Task.sleep(nanoseconds:100_000_000);host.layoutSubtreeIfNeeded()
            XCTAssertLessThanOrEqual(host.fittingSize.width,180)
            XCTAssertLessThanOrEqual(host.fittingSize.height,32)
            if let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,!folder.isEmpty,
               let rep=host.bitmapImageRepForCachingDisplay(in:host.bounds) {
                try FileManager.default.createDirectory(atPath:folder,withIntermediateDirectories:true)
                host.cacheDisplay(in:host.bounds,to:rep)
                try rep.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder)
                    .appendingPathComponent("menu-stacked-\(dark ? "dark" : "light").png"))
            }
        }
    }
}


extension CostDashboardTests {
    @MainActor
    func testCreationBadgesFitRealPanelInBothAppearances() async throws {
        let ages: [Double?] = [0,7,8,30,31,nil]
        let items=try ages.enumerated().map { index,age in
            try XCTUnwrap(InFlightWorkItem(row:[
                "work_item_id":.string("wi_20260919_00\(index)"),"owner":.string(index==4 ? "Long-owner-for-layout" : "pulse"),
                "title":.string("原WI标题：辅助说明保持完整，详情默认收起"),
                "status":.string(index==4 ? "awaiting_verification" : "in_progress"),
                "days":.number(999),"open_days":age.map(MetricScalar.number) ?? .null]))
        }
        let alerts=items.map { item in
            TriagedAlert(Alert(eventID:"request-"+item.id,projectionStatus:"event_only",severity:.warning,
                capabilityID:"notify.action_package",title:item.title,occurredAt:Date(timeIntervalSince1970:100),
                reason:"pulse｜原请求",tier:.act,workItemID:item.workItemID),workItem:item)
        }
        for dark in [false,true] {
            let host=NSHostingView(rootView:ScrollView {
                StatsCard {
                    StatsSectionHeading(lamp:.amber,title:"要你出手",count:"6 项")
                    ForEach(alerts) { ActAlertCard(alert:$0) }
                }.padding(14)
            }.frame(width:420,height:580).background(Color(nsColor:.windowBackgroundColor)))
            let window=NSWindow(contentRect:NSRect(x:0,y:0,width:420,height:580),styleMask:[.titled],backing:.buffered,defer:false)
            window.appearance=NSAppearance(named:dark ? .darkAqua : .aqua)
            window.contentView=host;window.orderFront(nil);defer { window.orderOut(nil) }
            try await Task.sleep(nanoseconds:150_000_000);host.layoutSubtreeIfNeeded()
            func find(_ view:NSView,_ id:String)->NSView? {
                if view.identifier?.rawValue==id { return view }
                return view.subviews.compactMap { find($0,id) }.first
            }
            for alert in alerts {
                let badge=try XCTUnwrap(find(host,"act-status-age-"+alert.id))
                let rect=badge.convert(badge.bounds,to:host)
                XCTAssertGreaterThanOrEqual(rect.minX,14);XCTAssertLessThanOrEqual(rect.maxX,406)
                XCTAssertNil(find(host,"act-detail-"+alert.id))
            }
            XCTAssertEqual(host.fittingSize.width,420,accuracy:1)
            if let folder=Bundle.main.object(forInfoDictionaryKey:"PulseTestRenderDirectory") as? String,!folder.isEmpty,
               let rep=host.bitmapImageRepForCachingDisplay(in:host.bounds) {
                host.cacheDisplay(in:host.bounds,to:rep)
                try rep.representation(using:.png,properties:[:])?.write(to:URL(fileURLWithPath:folder)
                    .appendingPathComponent("wi-age-badges-\(dark ? "dark" : "light").png"))
            }
        }
    }
}

extension CostDashboardTests {
    func testBoundedUnpricedEvidenceKeepsActualCountAndRejectsBadCounts() throws {
        let samples: [[String: Any]] = (0..<2).map { ["fact_ref": "ccf1_" + String(repeating: "a", count: 63) + String($0), "reason": "missing_or_invalid_price"] }
        var object: [String: Any] = ["machine_cash_usd": NSNull(), "partial_machine_cash_usd": "1",
            "cash_basis": "openrouter_standard_token_cash", "reasons": ["incomplete_price_or_quantity"],
            "unpriced": samples, "unpriced_count": 1500,
            "unpriced_reason_counts": ["missing_or_invalid_price": 1500],
            "unpriced_sha256": String(repeating: "b", count: 64)]
        func decode(_ value: [String: Any]) throws -> QueryMachineCost {
            let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
            return try decoder.decode(QueryMachineCost.self, from: JSONSerialization.data(withJSONObject: value))
        }
        let cost = try decode(object)
        XCTAssertTrue(cost.isValid); XCTAssertTrue(cost.isPartial)
        XCTAssertEqual(cost.unpricedEventCount, 1500); XCTAssertEqual(cost.unpriced.count, 2)
        object["unpriced_count"] = 2
        XCTAssertFalse(try decode(object).isValid)
        object["unpriced_count"] = 1500
        object["unpriced_reason_counts"] = ["other": 1500]
        XCTAssertFalse(try decode(object).isValid)
        for key in ["unpriced_count", "unpriced_reason_counts", "unpriced_sha256"] { object.removeValue(forKey: key) }
        let legacy = try decode(object)
        XCTAssertTrue(legacy.isValid); XCTAssertEqual(legacy.unpricedEventCount, 2)
    }
}


extension CostDashboardTests {
    private func subcallRow(total: Int = 6, subcallCount: Int = 6, childRuntime: String = "pi") -> CostQueryGroup {
        let call = CostRuntimeSubcall(parentScopeRef: "ccf1_" + String(repeating: "a", count: 64),
            childScopeRef: "ccf1_" + String(repeating: "b", count: 64),
            callRef: "ccf1_" + String(repeating: "c", count: 64), parentRuntime: "codex",
            childRuntime: childRuntime, basis: "reviewed_launch", eventCount: subcallCount,
            label: "更新 Relay 支持 gpt-6-sol")
        let calls = CostRuntimeSubcalls(eventCount: subcallCount, itemCount: 1, items: [call],
            sha256: String(repeating: "d", count: 64))
        let detail = CostActivityDetail(firstUsageAt: nil, lastUsageAt: nil, factRefs: [],
            factRefCount: 0, attributionRefs: [], attributionRefCount: 0, subcalls: calls)
        let cost = QueryMachineCost(machineCashUsd: nil, partialMachineCashUsd: "1.23456",
            cashBasis: "openrouter_standard_token_cash", reasons: ["attribution_unknown"], unpriced: [])
        return CostQueryGroup(key: ["agent":"pulse", "wi":"unassigned", "entrypoint":"codex_app",
            "node":"ownermac", "runtime":"pi", "model":"gpt-6-astra"], cost: cost,
            usage: CostGroupUsage(tokens: [:], eventCount: total, knownTotalTokens: 78913), detail: detail)
    }

    func testRuntimeSubcallLabelsPreserveJointGroupAndCost() {
        let row = subcallRow()
        XCTAssertEqual(row.subcallLabel, "子调用")
        XCTAssertEqual(CostTableColumn.entrypoint.text(row), "Codex App · 子调用")
        XCTAssertEqual(CostTableColumn.runtime.text(row), "Pi")
        XCTAssertEqual(CostTableColumn.wi.text(row), "未关联")
        XCTAssertEqual(row.cost.exactAmount, "1.23456")
        XCTAssertEqual(row.detail?.subcalls?.items.first?.chain, "Codex → Pi")
        XCTAssertEqual(subcallRow(total: 9).subcallLabel, "含子调用")
        XCTAssertEqual(CostTableColumn.allCases.count, 9)
    }

    func testRuntimeSubcallInvalidCountsAndRuntimeAreNotPresentedAsKnown() {
        XCTAssertEqual(subcallRow(total: 5).subcallLabel, "子调用待核验")
        XCTAssertEqual(subcallRow(subcallCount: 0).subcallLabel, "子调用待核验")
        XCTAssertEqual(subcallRow(childRuntime: "cc").subcallLabel, "子调用待核验")
        let row = subcallRow()
        let original = CostQueryGroup(key: row.key, cost: row.cost, usage: row.usage)
        XCTAssertNil(original.subcallLabel)
        XCTAssertEqual(CostTableColumn.entrypoint.text(original), "Codex App")
        XCTAssertEqual(original.cost, row.cost)
    }

    func testRuntimeSubcallJSONRoundTripAndBoundedSummary() throws {
        let row = subcallRow()
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let encoded = try encoder.encode(row)
        let decoded = try decoder.decode(CostQueryGroup.self, from: encoded)
        XCTAssertEqual(decoded, row)
        XCTAssertEqual(decoded.subcallLabel, "子调用")
        let call = try XCTUnwrap(row.detail?.subcalls?.items.first)
        let bounded = CostRuntimeSubcalls(eventCount: 12, itemCount: 2, items: [call],
            sha256: String(repeating: "d", count: 64))
        XCTAssertTrue(bounded.isValid(eventCount: 15, runtime: "pi"))
        let wrong = CostRuntimeSubcalls(eventCount: 6, itemCount: 2, items: [call],
            sha256: String(repeating: "d", count: 64))
        XCTAssertFalse(wrong.isValid(eventCount: 15, runtime: "pi"))
    }
}


extension CostDashboardTests {
    @MainActor
    func testSubcallBadgeUsesNativeSelectableCellAfterResize() async throws {
        let row = subcallRow(total: 9)
        let grid = CostGridContainer(frame: NSRect(x: 0, y: 0, width: 1120, height: 250))
        let controller = CostActivityTable.Coordinator(preferences: nil)
        controller.connect(grid); controller.update([row], scope: "subcall-test")
        let window = NSWindow(contentRect: grid.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = grid; window.orderFront(nil); defer { window.orderOut(nil) }
        try await Task.sleep(nanoseconds: 100_000_000)
        let index = try XCTUnwrap(CostTableColumn.allCases.firstIndex(of: .entrypoint))
        for height in [CGFloat(28), 34, 46] {
            grid.table.rowHeight = height; grid.table.reloadData(); grid.layoutSubtreeIfNeeded()
            grid.table.scrollColumnToVisible(index)
            let cell = try XCTUnwrap(grid.table.view(atColumn: index, row: 0, makeIfNecessary: true) as? CostTextCell)
            cell.layoutSubtreeIfNeeded()
            XCTAssertEqual(cell.label.stringValue, "Codex App")
            XCTAssertTrue(cell.label.isSelectable)
            XCTAssertEqual(cell.label.alignment, .left)
            XCTAssertEqual(cell.label.frame.height, 18, accuracy: 0.5)
            XCTAssertEqual(cell.label.frame.midY, cell.bounds.midY, accuracy: 0.5)
            let badge = try XCTUnwrap(cell.subviews.compactMap { $0 as? NSTextField }.first { $0 !== cell.label })
            XCTAssertEqual(badge.stringValue, "含子调用")
            XCTAssertFalse(badge.isHidden)
            XCTAssertGreaterThan(cell.label.frame.width, 0)
            XCTAssertLessThanOrEqual(cell.label.frame.maxX, badge.frame.minX)
        }
    }
}


extension CostDashboardTests {
    /// Rewrites today's activity rows of a replay without touching any other view.
    private func ccReplay(_ edit: (inout [String: Any]) -> Void) throws -> CostDashboard {
        var object = try XCTUnwrap(replays()["outcome-dashboard"]).object
        var snapshot = try XCTUnwrap(object["snapshot"] as? [String: Any])
        var dashboard = try XCTUnwrap(snapshot["cost_dashboard"] as? [String: Any])
        var views = try XCTUnwrap(dashboard["views"] as? [[String: Any]])
        let index = try XCTUnwrap(views.firstIndex { $0["dimension"] as? String == "activity" && $0["period"] as? String == "today" })
        edit(&views[index])
        dashboard["views"] = views; snapshot["cost_dashboard"] = dashboard; object["snapshot"] = snapshot
        return try CostConsumerReplay(object).document
    }

    @MainActor
    private func ccQuotaIDs(_ doc: CostDashboard?, now: Date, relay: RelayViewModel) async throws -> Set<String> {
        let context = DashboardStatusContext(authority: GlobalAuthorityViewModel(projection: nil), relay: relay,
            harness: HarnessViewModel(metrics: SnapshotMetrics([]), hasSnapshot: true), health: .ok, incidents: [], observedAt: now)
        let view = CostDashboardView(document: doc, now: now, initialCCQuotaExpanded: true,
                                     tablePreferences: nil, statusContext: context)
        let host = NSHostingView(rootView: ScrollView { view.padding(14) }.frame(width: 420, height: 900))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 900), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host; window.orderFront(nil)
        defer { window.orderOut(nil) }
        try await Task.sleep(nanoseconds: 150_000_000); host.layoutSubtreeIfNeeded()
        func ids(_ v: NSView) -> [String] { [v.identifier?.rawValue].compactMap { $0 } + v.subviews.flatMap(ids) }
        return Set(ids(host))
    }

    func testRuntimeUsageSumsOnlyMatchingRowsAndKeepsMissingClassesUnknown() throws {
        let doc = try ccReplay { view in
            var groups = view["groups"] as! [[String: Any]]
            var key = groups[0]["key"] as! [String: String]; key["runtime"] = "cc"; groups[0]["key"] = key
            var usage = groups[0]["usage"] as! [String: Any]
            usage["tokens"] = ["uncached_input": ["known": 7, "unknown_event_count": 0],
                               "output": ["known": NSNull(), "unknown_event_count": 1]]
            groups[0]["usage"] = usage
            view["groups"] = groups
        }
        let today = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt))
        let cc = try XCTUnwrap(today.runtimeUsage("cc"))
        XCTAssertEqual(cc.eventCount, 1)
        XCTAssertEqual(cc.tokens["uncached_input"], CostTokenQuantity(known: 7, unknownEventCount: 0))
        XCTAssertEqual(cc.tokens["output"], CostTokenQuantity(known: nil, unknownEventCount: 1), "unknown is never zero")
        XCTAssertNil(cc.tokens["cache_read"], "classes absent from every CC row are not synthesized")
        XCTAssertEqual(cc.lastUsageAt, today.groups[0].detail?.lastUsageAt)
        XCTAssertNil(today.runtimeUsage("pi"))
        let codex = try XCTUnwrap(today.runtimeUsage("codex"))
        XCTAssertEqual(codex.eventCount, 1)
    }

    @MainActor
    func testCCQuotaTodayUsageShowsCCRowsOrTruthfulAbsence() async throws {
        let base = try XCTUnwrap(replays()["outcome-dashboard"]).document
        let now = base.generatedAt
        let fiveHour = Metric(id: MetricID.ccFiveHourUsedPercent, capabilityID: "s1.request_trace_capture", status: .available,
            value: .number(3), unit: "percent", window: "5h", quality: .validated,
            scope: "CC subscription rolling 5-hour quota", observedAt: now.addingTimeInterval(-3600))
        let relay = RelayViewModel(metrics: SnapshotMetrics([fiveHour]), hasSnapshot: true, at: now)
        XCTAssertEqual(relay.ccFiveHourUsedPercent, 3)
        let stale = RelayViewModel(metrics: SnapshotMetrics([fiveHour]), hasSnapshot: true, at: now.addingTimeInterval(5 * 3600))
        XCTAssertNil(stale.ccFiveHourUsedPercent, "a reading older than the 5-hour window is not shown")

        let withCC = try ccReplay { view in
            var groups = view["groups"] as! [[String: Any]]
            var key = groups[0]["key"] as! [String: String]; key["runtime"] = "cc"; groups[0]["key"] = key
            view["groups"] = groups
        }
        var ids = try await ccQuotaIDs(withCC, now: now, relay: relay)
        XCTAssertTrue(ids.contains("cc-quota-today-tokens"))
        XCTAssertTrue(ids.contains("cc-quota-5h-detail"))
        XCTAssertFalse(ids.contains("cc-quota-today-none"))

        // Replay has only Codex rows and unproven coverage: no CC row is not zero usage.
        ids = try await ccQuotaIDs(base, now: now, relay: stale)
        XCTAssertTrue(ids.contains("cc-quota-today-none"))
        XCTAssertFalse(ids.contains("cc-quota-today-tokens"))
        XCTAssertFalse(ids.contains("cc-quota-today-zero"))
        XCTAssertFalse(ids.contains("cc-quota-5h-detail"))

        let complete = try ccReplay { view in
            var summary = view["summary"] as! [String: Any]
            var usage = summary["usage"] as! [String: Any]; usage["source_coverage"] = "complete"
            summary["usage"] = usage; view["summary"] = summary
        }
        ids = try await ccQuotaIDs(complete, now: now, relay: relay)
        XCTAssertTrue(ids.contains("cc-quota-today-zero"))

        ids = try await ccQuotaIDs(nil, now: now, relay: relay)
        XCTAssertTrue(ids.contains("cc-quota-today-unavailable"))
        XCTAssertFalse(ids.contains("cc-quota-today-tokens"))
    }
}

extension CostDashboardTests {
    private func tenantReplay(_ edit: ((inout [[String: Any]]) -> Void)? = nil) throws -> CostDashboard {
        try ccReplay { view in
            var query = view["query"] as! [String: Any]
            query["group_by"] = ["tenant", "agent", "wi", "entrypoint", "node", "runtime", "model"]
            view["query"] = query
            view["population_scope"] = "all_registered_tenants"
            var groups = view["groups"] as! [[String: Any]]
            for i in groups.indices {
                var key = groups[i]["key"] as! [String: String]; key["tenant"] = "team-a"; groups[i]["key"] = key
                groups[i]["tenant"] = ["id": "team-a", "name": "Declared team A", "binding_refs": ["ccf1_" + String(repeating: "a", count: 64)]]
            }
            edit?(&groups); view["groups"] = groups
        }
    }

    func testTenantSevenDimensionAndLegacySixDimensionBothPreserveKnownCost() throws {
        let old = try XCTUnwrap(replays()["outcome-dashboard"]?.document)
        let oldView = try XCTUnwrap(old.activityView(period: .today, at: old.generatedAt))
        let new = try tenantReplay()
        let view = try XCTUnwrap(new.activityView(period: .today, at: new.generatedAt))
        XCTAssertEqual(view.query.groupBy, ["tenant", "agent", "wi", "entrypoint", "node", "runtime", "model"])
        XCTAssertEqual(view.summary, oldView.summary)
        XCTAssertEqual(view.groups.map(\.cost), oldView.groups.map(\.cost))
        XCTAssertEqual(view.groups.map(\.usage), oldView.groups.map(\.usage))
        XCTAssertTrue(view.groups.allSatisfy { CostTableColumn.tenant.text($0) == "Declared team A" })
        XCTAssertEqual(view.populationScopeText, "全平台 · 已登记活动")
        XCTAssertTrue(oldView.groups.allSatisfy { CostTableColumn.tenant.text($0) == "租户未知" })
    }

    func testTenantDisplayRequiresMatchingIdentitySafeNameAndOpaqueProof() throws {
        let valid = try tenantReplay(); let base = try XCTUnwrap(valid.activityView(period: .today, at: valid.generatedAt)?.groups.first)
        for identity in [CostTenantIdentity(id: "other", name: "Declared team A", bindingRefs: ["ccf1_" + String(repeating: "a", count: 64)]),
                         CostTenantIdentity(id: "team-a", name: "raw:route", bindingRefs: ["ccf1_" + String(repeating: "a", count: 64)]),
                         CostTenantIdentity(id: "team-a", name: "Declared team A", bindingRefs: ["raw-id"]),
                         CostTenantIdentity(id: "team-a", name: "Declared team A", bindingRefs: [])] {
            var row = base; row.tenant = identity
            XCTAssertEqual(row.tenantText, "租户待核验")
            XCTAssertEqual(row.cost, base.cost)
            XCTAssertEqual(row.usage, base.usage)
        }
        let bad = try tenantReplay { rows in for i in rows.indices { rows[i]["tenant"] = ["id": 123] } }
        let badView = try XCTUnwrap(bad.activityView(period: .today, at: bad.generatedAt))
        XCTAssertTrue(badView.groups.allSatisfy { $0.tenantText == "租户待核验" })
        XCTAssertEqual(badView.summary.cost, valid.activityView(period: .today, at: valid.generatedAt)?.summary.cost)
    }

    func testTenantSourceAndRegistryConflictAreShownWithoutChangingCost() throws {
        let ref = "ccf1_" + String(repeating: "a", count: 64)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let registered = try decoder.decode(CostTenantIdentity.self, from: Data("""
            {"id":"team-a","name":"Declared team A","binding_refs":["\(ref)"],"sources":["registry"],"registry_conflicts":[]}
            """.utf8))
        let cost = QueryMachineCost(machineCashUsd: nil, partialMachineCashUsd: "1.25",
            cashBasis: "machine_standard", reasons: [], unpriced: [])
        var row = CostQueryGroup(key: ["tenant": "team-a", "agent": "shared"], cost: cost)
        row.tenant = registered
        XCTAssertEqual(row.tenantText, "Declared team A")
        XCTAssertEqual(row.tenantSourceText, "Agent 登记")
        var conflicted = row
        conflicted.tenant = CostTenantIdentity(id: "team-a", name: "Declared team A", bindingRefs: [ref],
                                               sources: ["session"], registryConflicts: ["team-b"])
        XCTAssertEqual(conflicted.tenantText, "Declared team A（与登记冲突）")
        XCTAssertEqual(conflicted.tenantSourceText, "会话绑定；登记为 team-b")
        XCTAssertEqual(conflicted.cost, row.cost)
        var legacy = row
        legacy.tenant = CostTenantIdentity(id: "team-a", name: "Declared team A", bindingRefs: [ref])
        XCTAssertEqual(legacy.tenantText, "Declared team A")
        XCTAssertNil(legacy.tenantSourceText)
    }

    func testTenantNamesNeverCollapseSameAgentRowsOrEraseUnknown() throws {
        let doc = try tenantReplay();let first = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt)?.groups.first)
        var secondKey = first.key;secondKey["tenant"] = "team-b"
        var second = CostQueryGroup(key: secondKey, cost: first.cost, usage: first.usage)
        second.tenant = CostTenantIdentity(id: "team-b", name: "Declared team B", bindingRefs: ["ccf1_" + String(repeating: "b", count: 64)])
        XCTAssertNotEqual(first.id, second.id)
        XCTAssertEqual(CostTableSelection().rows([first, second]).count, 2)
        var selection = CostTableSelection(); selection.filters[.tenant] = "team B"
        XCTAssertEqual(selection.rows([first, second]).map(\.id), [second.id])
        var missingKey = first.key;missingKey["tenant"] = "unassigned"
        let missing = CostQueryGroup(key: missingKey, cost: first.cost, usage: first.usage)
        XCTAssertEqual(missing.tenantText, "租户未知")
        XCTAssertEqual(CostTableSelection().rows([first, second, missing]).count, 3)
        let malformed = try tenantReplay { rows in
            var key = rows[0]["key"] as! [String: String];key.removeValue(forKey: "tenant");rows[0]["key"] = key
        }
        XCTAssertNil(malformed.activityView(period: .today, at: malformed.generatedAt))
    }

    @MainActor
    func testTenantNativeTableShowsBothIdentityColumns() throws {
        let doc = try tenantReplay();let rows = try XCTUnwrap(doc.activityView(period: .today, at: doc.generatedAt)?.groups)
        let host = NSHostingView(rootView: CostActivityTable(rows: rows, scope: "tenant-fixture", preferences: nil))
        host.frame = NSRect(x: 0, y: 0, width: 1100, height: 300);host.layoutSubtreeIfNeeded()
        func table(_ node: NSView) -> NSTableView? {
            if let value = node as? NSTableView { return value }
            return node.subviews.compactMap(table).first
        }
        let grid = try XCTUnwrap(table(host))
        XCTAssertEqual(grid.tableColumns.map { $0.identifier.rawValue }, ["tenant", "agent", "wi", "outcome", "amount", "entrypoint", "node", "runtime", "model"])
        XCTAssertEqual(grid.numberOfRows, rows.count)
        if let output = Bundle.main.object(forInfoDictionaryKey: "PulseTestRenderDirectory") as? String,
           let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
            host.cacheDisplay(in: host.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: output).appendingPathComponent("tenant-declared-synthetic-native.png"))
        }
    }
}


extension CostDashboardTests {
    @MainActor
    func testActualPublishedSevenDimensionalPythonSnapshotSurvivesCacheAndNativeTable() throws {
        // Produced by the real Python attach_cost_dashboard from the untouched water-1244 publication.
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("exporter/tests/fixtures/published-tenant-seven-snapshot-v2.json")
        let data = try Data(contentsOf: fixture)
        let snapshot = try SnapshotDecoder().decode(data)
        let doc = try XCTUnwrap(snapshot.costDashboard)
        XCTAssertEqual(doc.sha256, "8ea31f8b6d3641d0cf3d6deafad0c07adedd3c00466da839158963dd43e14a3d")
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let cacheURL = directory.appendingPathComponent("snapshot.json")
        let cache = FileSnapshotCache(url: cacheURL)
        try cache.save(snapshot, source: data)
        XCTAssertEqual(try Data(contentsOf: cacheURL), data)
        XCTAssertEqual(try cache.load(), snapshot)
        let counts: [CostPeriod: Int] = [.today: 563, .week: 1176, .month: 3918]
        for period in [CostPeriod.today, .week, .month] {
            let view = try XCTUnwrap(doc.activityView(period: period, at: doc.generatedAt))
            XCTAssertEqual(view.watermark, 1244)
            XCTAssertEqual(view.query.groupBy, ["tenant", "agent", "wi", "entrypoint", "node", "runtime", "model"])
            XCTAssertEqual(view.summary.usage.eventCount, counts[period])
            XCTAssertEqual(view.populationScopeText, "全平台 · 已登记活动")
            XCTAssertTrue(view.groups.filter { $0.key["tenant"] == "owner" }.allSatisfy { $0.tenantText == "owner" })
            XCTAssertTrue(view.groups.filter { $0.key["tenant"] == "unassigned" }.allSatisfy { $0.tenantText == "租户未知" })
            let host = NSHostingView(rootView: CostActivityTable(rows: view.groups, scope: "actual-published-\(period.rawValue)", preferences: nil))
            host.frame = NSRect(x: 0, y: 0, width: 1100, height: 300); host.layoutSubtreeIfNeeded()
            func table(_ node: NSView) -> NSTableView? {
                if let value = node as? NSTableView { return value }
                return node.subviews.compactMap(table).first
            }
            let grid = try XCTUnwrap(table(host))
            XCTAssertEqual(grid.numberOfRows, view.groups.count)
            XCTAssertEqual(grid.tableColumns.map { $0.identifier.rawValue }, ["tenant", "agent", "wi", "outcome", "amount", "entrypoint", "node", "runtime", "model"])
        }
    }
}

extension CostDashboardTests {
    private func dashboardView(_ cost: QueryMachineCost, events: Int = 3, groups: [CostQueryGroup] = [],
                               tenant: Bool = false, activeAgents: [String]? = nil) -> CostQueryView {
        let dimensions = (tenant ? ["tenant"] : []) + ["agent", "wi", "entrypoint", "node", "runtime", "model"]
        return CostQueryView(period: "today", dimension: "activity", sourceSchema: "computecost_collaboration_query/v1",
            query: CostQueryScope(fromDate: "2026-10-19", toDate: "2026-10-20", timezone: "Asia/Shanghai",
                                  scope: "activity", groupBy: dimensions, watermark: 1),
            watermark: 1, recordedAt: nil, catalogSha256: nil, catalogObservedAt: nil,
            reportSha256: String(repeating: "a", count: 64),
            summary: CostQuerySummary(cost: cost, usage: CostQueryUsage(knownTotalTokens: nil, eventCount: events,
                sourceCoverage: "unknown", wiInventoryCoverage: "unknown")),
            groups: groups, activeAgents: activeAgents)
    }

    private func cost(full: String? = nil, partial: String? = nil, priced: String?, count: Int?,
                      reasons: [String] = ["attribution_unknown"], unpriced: Int = 0) -> QueryMachineCost {
        let rows = (0..<unpriced).map { CostUnpriced(factRef: "ccf1_\($0)", reason: "missing_or_invalid_price") }
        var value = QueryMachineCost(machineCashUsd: full, partialMachineCashUsd: partial,
            cashBasis: "openrouter_standard_token_cash", reasons: full == nil ? reasons : [], unpriced: rows)
        value.pricedMachineCashUsd = priced; value.pricedEventCount = count
        return value
    }

    func testDashboardTotalUsesCompleteAmountThenEveryPricedRow() {
        XCTAssertEqual(dashboardView(cost(full: "5", priced: "5", count: 3)).totalCostAmount, Decimal(5))
        XCTAssertEqual(dashboardView(cost(partial: "4", priced: "4", count: 2, unpriced: 1)).totalCostAmount, Decimal(4))
        // join_conflict withholds the partial amount; the priced rows remain the dashboard total.
        let conflicted = dashboardView(cost(priced: "100.2786", count: 3, reasons: ["join_conflict"]))
        XCTAssertNil(conflicted.summary.cost.amount)
        XCTAssertEqual(conflicted.totalCostAmount, Decimal(string: "100.2786"))
        XCTAssertEqual(conflicted.totalCostExact, "100.2786")
        // Nothing priced: unknown, never zero.
        XCTAssertNil(dashboardView(cost(priced: nil, count: 0, unpriced: 3)).totalCostAmount)
        // Older publications without the priced pair keep the partial amount.
        XCTAssertEqual(dashboardView(cost(partial: "4", priced: nil, count: nil)).totalCostAmount, Decimal(4))
        // A priced total that contradicts the same query is not shown.
        XCTAssertNil(dashboardView(cost(partial: "4", priced: "5", count: 3)).totalCostAmount)
        XCTAssertNil(dashboardView(cost(priced: "1", count: 2, reasons: ["join_conflict"])).totalCostAmount)
        XCTAssertNil(dashboardView(cost(priced: nil, count: 3, reasons: ["join_conflict"])).totalCostAmount)
    }

    func testProofGapsAreNotAnomaliesAndKeepTheAmount() {
        var gapOnly = cost(partial: "2", priced: "2", count: 1, reasons: ["execution_link_unknown", "source_coverage_unknown"])
        gapOnly.anomalies = []; gapOnly.proofGaps = ["execution_link_unknown", "source_coverage_unknown"]
        XCTAssertTrue(gapOnly.isValid)
        XCTAssertFalse(gapOnly.hasAnomaly)
        XCTAssertEqual(gapOnly.note, "")
        XCTAssertEqual(gapOnly.proofGapNote, "部分用量尚未关联执行记录；采集范围尚未完整证明")
        XCTAssertEqual(gapOnly.amount, Decimal(2))
        var mixed = cost(partial: "3", priced: "3", count: 1, reasons: ["attribution_unknown", "source_coverage_unknown"])
        mixed.anomalies = ["attribution_unknown"]; mixed.proofGaps = ["source_coverage_unknown"]
        XCTAssertTrue(mixed.hasAnomaly)
        XCTAssertEqual(mixed.note, "部分用量尚未关联 WI")
        // Older publications without classes keep every reason as an anomaly.
        let legacy = cost(partial: "1", priced: "1", count: 1, reasons: ["source_coverage_unknown"])
        XCTAssertTrue(legacy.hasAnomaly)
        XCTAssertEqual(legacy.proofGapReasons, [])
        // Classes must partition the unchanged reasons.
        var broken = mixed; broken.proofGaps = []
        XCTAssertFalse(broken.isValid)
        var half = mixed; half.proofGaps = nil
        XCTAssertFalse(half.isValid)
        func row(_ wi: String, _ value: QueryMachineCost) -> CostQueryGroup {
            CostQueryGroup(key: ["agent": "pulse", "wi": wi, "entrypoint": "cli", "node": "ownermac", "runtime": "cc", "model": "m"],
                           cost: value, usage: CostGroupUsage(tokens: [:], eventCount: 1, knownTotalTokens: nil))
        }
        let view = dashboardView(mixed, events: 2, groups: [row("wi_a", gapOnly), row("wi_b", mixed)])
        XCTAssertEqual(view.reasonClassCounts.anomaly, 1)
        XCTAssertEqual(view.reasonClassCounts.proofGapOnly, 1)
    }

    func testActiveAgentsAcrossTenantsAreCountedOnceAndNamedInDetail() {
        let priced = cost(partial: "1", priced: "1", count: 1)
        func row(_ tenant: String, _ agent: String) -> CostQueryGroup {
            CostQueryGroup(key: ["tenant": tenant, "agent": agent, "wi": "unassigned", "entrypoint": "cli",
                                 "node": "ownermac", "runtime": "cc", "model": "m"], cost: priced,
                           usage: CostGroupUsage(tokens: [:], eventCount: 1, knownTotalTokens: nil))
        }
        let view = dashboardView(priced, events: 3, groups: [row("team-a", "pulse"), row("team-b", "pulse"), row("team-a", "aki")],
                                 tenant: true, activeAgents: ["aki", "pulse"])
        XCTAssertEqual(view.activeAgentCount, 2)
        XCTAssertEqual(view.multiTenantAgents, ["pulse"])
        let inconsistent = dashboardView(priced, groups: [row("team-a", "pulse")], tenant: true, activeAgents: ["aki"])
        XCTAssertNil(inconsistent.activeAgentCount)
    }

    func testCostSourceAttentionExcludesRetiredNodesAndIncludesUnknown() {
        func source(_ label: String, kind: String = "pi", status: String, reason: String? = nil) -> CostSourceDetail {
            CostSourceDetail(label: label, kind: kind, status: status, reason: reason, filesSeen: 0, filesRead: 0,
                filesInvalid: 0, excludedDirectories: 0, latestUsageAt: nil, errors: [:], sourceRef: label)
        }
        let rows = [source("stats-transport-vultr", kind: "receipts", status: "unreachable"),
                    source("vultr.relay_host", kind: "relay_host", status: "partial"),
                    source("vultr.harness_root", kind: "harness_root", status: "unknown"),
                    source("tcloud.harness_root", kind: "harness_root", status: "unknown", reason: "execution_inventory_not_observed"),
                    source("ownermac.codex", kind: "codex", status: "partial", reason: "source_unreadable_or_invalid"),
                    source("native-cc-1", kind: "cc", status: "unknown"),
                    source("ownermac.pi", status: "complete"),
                    source("ownermac.relay_host", kind: "relay_host", status: "partial", reason: "host_usage_pending")]
        // A read native log whose only gap is the unproven execution inventory is a proof gap, not a failure.
        XCTAssertEqual(rows.filter(\.needsAttention).map(\.label), ["ownermac.codex", "native-cc-1"])
        XCTAssertEqual(rows[3].readStatusText, "已读取 · 执行清单未证明完整")
        XCTAssertEqual(rows[0].readStatusText, "节点已退役 · 历史记录保留")
        XCTAssertEqual(rows[0].node, "vultr")
        XCTAssertNil(rows[5].node)
        XCTAssertEqual(CostNode.title("vultr"), "Vultr（已退役）")
        XCTAssertEqual(CostText.reason("execution_evidence_conflict"), "执行记录存在冲突，相关成本待核验")
        XCTAssertEqual(CostText.reason("execution_inventory_not_observed"), "尚未观测到该来源的执行清单")
    }

    @MainActor
    func testWorkItemCellsFollowThePeriodAndRefuseAnotherDay() {
        let now = Date(timeIntervalSince1970: 1_792_390_000) // 2026-10-19 13:26 CST, Monday
        func count(_ id: String, _ value: Double) -> Metric {
            Metric(id: id, capabilityID: "s2.ledger_invariants", status: .available, value: .number(value),
                   unit: "count", window: "current", quality: .validated, scope: "test", observedAt: now)
        }
        let metrics = SnapshotMetrics([
            count(MetricID.wiDayCreated, 0), count(MetricID.wiIsoWeekCreated, 0), count(MetricID.wiMonthCreated, 30),
            count(MetricID.wiDayClosed, 1), count(MetricID.wiIsoWeekClosed, 1), count(MetricID.wiMonthClosed, 23)])
        func context(at observed: Date) -> DashboardStatusContext {
            DashboardStatusContext(authority: GlobalAuthorityViewModel(projection: nil),
                relay: RelayViewModel(metrics: metrics, hasSnapshot: true, at: now),
                harness: HarnessViewModel(metrics: metrics, hasSnapshot: true),
                health: .ok, incidents: [], observedAt: observed)
        }
        let current = context(at: now.addingTimeInterval(-60))
        XCTAssertTrue(current.workItemCounts(period: .today, now: now) == (0, 1))
        XCTAssertTrue(current.workItemCounts(period: .week, now: now) == (0, 1))
        XCTAssertTrue(current.workItemCounts(period: .month, now: now) == (30, 23))
        let yesterday = context(at: now.addingTimeInterval(-14 * 3600))
        XCTAssertTrue(yesterday.workItemCounts(period: .month, now: now) == (nil, nil))
        XCTAssertTrue(context(at: now.addingTimeInterval(-2700)).workItemCounts(period: .today, now: now) == (nil, nil))
        XCTAssertEqual(current.currentStatus(now: now).note, "已监测项均正常")
    }
}


extension CostDashboardTests {
    private func workBoardFixture() -> [Metric] {
        func entry(_ id: String, _ agent: String, kind: String, line: String, status: String, stuck: Bool,
                   mainOnly: Bool = false) -> [String: MetricScalar] {
            ["kind": .string(kind), "id": .string(id), "agent": .string(agent), "line": .string(line),
             "status": .string(status), "stuck": .boolean(stuck), "main_only": .boolean(mainOnly),
             "work_item_id": .string(kind == "wi" ? id : "wi_20261015_422"), "work_item_title": .string("经营 PULSE 平台"),
             "route": .string("Hestia → Pulse"), "purpose": .string(line), "next": .string("Pulse：可接单／拒绝"),
             "timeline": .string("10-21 09:00 发出 · Hestia"), "owner": .string(agent),
             "updated_at": .string("2026-10-25T08:00:00Z"), "created_at": .string("2026-10-15T04:00:00Z")]
        }
        func agent(_ id: String, _ summary: String) -> [String: MetricScalar] {
            ["kind": .string("agent"), "id": .string("agent:" + id), "agent": .string(id),
             "name": .string(id.prefix(1).uppercased() + id.dropFirst()), "summary": .string(summary)]
        }
        let rows: [[String: MetricScalar]] = [
            ["kind": .string("board"), "id": .string("board"), "wi_total": .number(4), "contract_total": .number(2),
             "contracts_available": .boolean(true)],
            agent("pulse", "待接合同 1（5 天） · 交回待处理 1 · 停滞 WI 1"),
            entry("op_wait", "pulse", kind: "contract", line: "Hestia：GROK 周配额纳入 Stats", status: "待接 5 天", stuck: true, mainOnly: true),
            entry("op_back", "pulse", kind: "contract", line: "Hestia 交回：AGENTS.md 写明声明 WI，并且这一句足够长需要在一行里截断", status: "交回待处理 17 时", stuck: true),
            entry("wi_20261015_938", "pulse", kind: "wi", line: "PULSE 生产态标准 3：存量 WI 清零", status: "10 天没推进", stuck: true),
            entry("wi_20261015_422", "pulse", kind: "wi", line: "经营 PULSE 平台", status: "17 时前", stuck: false),
            agent("amanda", "最近 7 时前"), entry("wi_amanda", "amanda", kind: "wi", line: "重建 Agent 工作区", status: "7 时前", stuck: false),
            agent("ken", "最近 8 时前"), entry("wi_ken", "ken", kind: "wi", line: "内容生产 skill 定稿", status: "8 时前", stuck: false),
        ]
        return [Metric(id: MetricID.workBoard, capabilityID: "s2.ledger_invariants", status: .available,
                       value: .rows(rows), unit: "list", window: "current", quality: .validated, scope: "test")]
    }
    func testWorkBoardGroupsByNextMoverAndMarksRequestsOnce() {
        let model = WorkBoardViewModel(metrics: SnapshotMetrics(workBoardFixture()), actWorkItemIDs: ["wi_20261015_422"])
        XCTAssertEqual(model.countText, "WI 4 · 合同 2")
        XCTAssertEqual(model.groups?.map(\.title), ["Pulse", "Amanda · Ken"])
        XCTAssertEqual(model.groups?.first?.entries.map(\.inAct), [false, false, false, true])
        XCTAssertEqual(model.stuckCount, 3)
    }
    @MainActor
    func testWorkBoardFoldsAgentsLinesAndEvidenceAtPanelWidth() async throws {
        let model = WorkBoardViewModel(metrics: SnapshotMetrics(workBoardFixture()), actWorkItemIDs: ["wi_20261015_422"])
        for (groups, entries) in [(Set<String>(), Set<String>()), (Set(["agent:pulse"]), Set<String>()),
                                  (Set(["agent:pulse"]), Set(["op_wait"]))] {
            for dark in [false, true] {
                let host = NSHostingView(rootView: ScrollView {
                    WorkBoardSection(model: model, initiallyExpanded: groups, initiallyExpandedEntries: entries).padding(14)
                }.frame(width: 420, height: 580).background(Color(nsColor: .windowBackgroundColor))
                    .environment(\.colorScheme, dark ? .dark : .light))
                let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 580), styleMask: [.titled], backing: .buffered, defer: false)
                window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
                window.contentView = host; window.orderFront(nil)
                defer { window.orderOut(nil) }
                try await Task.sleep(nanoseconds: 150_000_000); host.layoutSubtreeIfNeeded()
                func find(_ view: NSView, _ id: String) -> NSView? {
                    if view.identifier?.rawValue == id { return view }
                    return view.subviews.compactMap { find($0, id) }.first
                }
                let section = try XCTUnwrap(find(host, "work-board-section"))
                let box = section.convert(section.bounds, to: host)
                XCTAssertEqual(box.minX, 14, accuracy: 1); XCTAssertEqual(box.width, 392, accuracy: 1)
                XCTAssertNotNil(find(host, "work-board-group-agent:pulse"))
                XCTAssertNotNil(find(host, "work-board-group-agents:amanda,ken"))
                XCTAssertEqual(find(host, "work-board-entry-op_wait") != nil, !groups.isEmpty)
                XCTAssertEqual(find(host, "work-board-detail-op_wait") != nil, !entries.isEmpty)
                XCTAssertNil(find(host, "work-board-detail-op_back"))
                XCTAssertEqual(find(host, "work-board-in-act-wi_20261015_422") != nil, !groups.isEmpty)
                XCTAssertNil(find(host, "work-board-in-act-wi_20261015_938"))
                if let line = find(host, "work-board-entry-op_back") {
                    let rect = line.convert(line.bounds, to: host)
                    XCTAssertGreaterThanOrEqual(rect.minX, 14); XCTAssertLessThanOrEqual(rect.maxX, 406)
                }
                XCTAssertEqual(host.fittingSize.width, 420, accuracy: 1)
                if let folder = Bundle.main.object(forInfoDictionaryKey: "PulseTestRenderDirectory") as? String,
                   !folder.isEmpty, let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
                    try FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
                    host.cacheDisplay(in: host.bounds, to: rep)
                    try rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: folder)
                        .appendingPathComponent("work-board-420-\(groups.count)-\(entries.count)-\(dark ? "dark" : "light").png"))
                }
            }
        }
    }
    func testWorkBoardSaysUnknownInsteadOfEmptyWhenUnreadable() {
        let model = WorkBoardViewModel(metrics: SnapshotMetrics([]))
        XCTAssertNil(model.groups)
        XCTAssertNil(model.countText)
    }
}


extension CostDashboardTests {
    /// The GROK row expands to reset and read time; without a reading it states the reason.
    @MainActor func testGrokQuotaPanelShowsResetReadTimeOrReason() async throws {
        let now = Date(timeIntervalSince1970: 1_792_569_600)
        let scope = "Grok account weekly pool; not per-bot cost"
        func metric(_ id: String, _ value: MetricValue, _ unit: String, available: Bool = true,
                    observed: Date? = nil, reason: String? = nil) -> Metric {
            Metric(id: id, capabilityID: "s1.request_trace_capture", status: available ? .available : .unknown,
                   value: value, unit: unit, window: "provider_week", quality: available ? .validated : .insufficient,
                   scope: scope, observedAt: observed ?? now.addingTimeInterval(-600), reason: reason)
        }
        let reading = SnapshotMetrics([
            metric(MetricID.grokUsedPercent, .number(87.38812), "percent"),
            metric(MetricID.grokResetsAt, .number(now.addingTimeInterval(6 * 86400).timeIntervalSince1970), "unix_seconds"),
            metric(MetricID.grokPeriodStart, .number(now.addingTimeInterval(-86400).timeIntervalSince1970), "unix_seconds"),
            metric(MetricID.grokPlanLabel, .text("Grok Bot Plan"), "text"),
        ])
        let login = SnapshotMetrics([metric(MetricID.grokUsedPercent, .null, "percent", available: false,
                                            reason: "grok_login_invalid")])
        for (name, metrics, unavailable) in [("available", reading, false), ("login-invalid", login, true)] {
            let relay = RelayViewModel(metrics: metrics, hasSnapshot: true, at: now)
            let context = DashboardStatusContext(authority: GlobalAuthorityViewModel(projection: nil), relay: relay,
                harness: HarnessViewModel(metrics: SnapshotMetrics([]), hasSnapshot: true), health: .ok, incidents: [],
                observedAt: now)
            let view = CostDashboardView(document: nil, now: now, initialGrokQuotaExpanded: true,
                                         tablePreferences: nil, statusContext: context)
            let host = NSHostingView(rootView: ScrollView { view.padding(14) }.frame(width: 420, height: 700))
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 700), styleMask: [.titled],
                                  backing: .buffered, defer: false)
            window.contentView = host; window.orderFront(nil)
            defer { window.orderOut(nil) }
            try await Task.sleep(nanoseconds: 150_000_000); host.layoutSubtreeIfNeeded()
            func ids(_ v: NSView) -> [String] { [v.identifier?.rawValue].compactMap { $0 } + v.subviews.flatMap(ids) }
            let found = Set(ids(host))
            XCTAssertTrue(found.contains("dashboard-grok-quota"), name)
            XCTAssertTrue(found.contains("grok-quota-reset-detail"), name)
            XCTAssertTrue(found.contains("grok-quota-observed-detail"), name)
            XCTAssertEqual(found.contains("grok-quota-unavailable"), unavailable, name)
            if let folder = Bundle.main.object(forInfoDictionaryKey: "PulseTestRenderDirectory") as? String,
               !folder.isEmpty, let image = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
                host.cacheDisplay(in: host.bounds, to: image)
                try image.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: folder)
                    .appendingPathComponent("grok-quota-\(name)-420.png"))
            }
        }
    }
}


extension CostDashboardTests {
    private func pullRequest(_ number: Int, _ at: String, score: Int? = 92) -> WIPullRequest {
        WIPullRequest(repository: "bigbird-online/PULSE", number: number, mergeCommit: String(repeating: "f", count: 40),
                      mergedAt: SnapshotDecoder.parseISO8601(at), score: score, reviewedRounds: 1,
                      decision: score == nil ? "human" : "review")
    }

    private func outputs(_ items: [WIPullRequestWorkItem], status: String = "complete",
                         unreadable: [String] = []) -> WIPullRequestOutputs {
        let known = Set(items.flatMap { $0.pullRequests.map(\.id) }).count
        return WIPullRequestOutputs(status: status, reason: nil, metricScope: "wi_kernel_merged_pull_requests_in_period",
            count: status == "complete" ? known : nil, knownCount: known, workItems: items,
            unreadableWorkItems: unreadable, timeUnknownCount: 0)
    }

    private func activityRow(_ wi: String, agent: String = "pulse", events: Int = 1) -> CostQueryGroup {
        let key = ["agent": agent, "wi": wi, "entrypoint": "feishu", "node": "tcloud", "runtime": "cc", "model": "m"]
        return CostQueryGroup(key: key, cost: cost(partial: events > 0 ? "3" : nil, priced: nil, count: nil),
                              usage: CostGroupUsage(tokens: [:], eventCount: events, knownTotalTokens: nil))
    }

    func testOutputColumnShowsKernelMergedPullRequestsAndUnknownIsNotZero() {
        let merged = WIPullRequestWorkItem(wi: "wi_20261019_001", count: 2, costUsd: "3", costComplete: false,
            costPerPullRequestUsd: "1.500000",
            pullRequests: [pullRequest(1417, "2026-10-19T02:00:00Z"), pullRequest(1421, "2026-10-19T03:00:00Z", score: nil)])
        let only = WIPullRequestWorkItem(wi: "wi_20261019_003", count: 1, costUsd: nil, costComplete: false,
            costPerPullRequestUsd: nil, pullRequests: [pullRequest(9, "2026-10-19T04:00:00Z")])
        var view = dashboardView(cost(partial: "6", priced: "6", count: 2), events: 2,
            groups: [activityRow("wi_20261019_001"), activityRow("wi_20261019_002"), activityRow("cron")])
        view.pullRequestOutputs = outputs([merged, only])
        view.pullRequestOnlyGroups = [activityRow("wi_20261019_003", agent: "aki", events: 0)]
        let rows = view.naming([]).presentationRows
        XCTAssertEqual(rows.map(\.outputText), ["2 个 PR", "0 个 PR", "—", "1 个 PR"])
        XCTAssertEqual(rows.first?.pullRequests?.item?.costText, "WI 本期 $3.00 · 每 PR $1.50（已计价部分，完整成本未知）")
        XCTAssertEqual(rows.first?.pullRequests?.item?.pullRequests.map(\.reviewText),
                       ["审核 92 分 · 1 轮", "无审核分 · 1 轮 · 人类确认合并"])
        XCTAssertEqual(rows.last?.pullRequests?.item?.costText, "本期无用量记录，成本未提供")
        XCTAssertNil(rows.first?.outcomeCount, "Accepted Outcomes stay a separate measure")
        // Unreadable WI records and an unavailable source are unknown, never zero.
        view.pullRequestOutputs = outputs([merged], status: "partial", unreadable: ["wi_20261019_002"])
        XCTAssertEqual(view.naming([]).presentationRows.map(\.outputText), ["2 个 PR", "未提供", "—"])
        view.pullRequestOutputs = outputs([], status: "unavailable")
        XCTAssertEqual(view.naming([]).presentationRows.map(\.outputText), ["未提供", "未提供", "—"])
        // A PR outside the period or a count that does not conserve is not used.
        view.pullRequestOutputs = outputs([WIPullRequestWorkItem(wi: "wi_20261019_001", count: 1, costUsd: nil,
            costComplete: false, costPerPullRequestUsd: nil, pullRequests: [pullRequest(1, "2026-10-20T02:00:00Z")])])
        XCTAssertNil(view.validPullRequests)
        XCTAssertEqual(view.naming([]).presentationRows.map(\.outputText), ["未提供", "未提供", "—"])
        view.pullRequestOutputs = WIPullRequestOutputs(status: "complete", reason: nil,
            metricScope: "wi_kernel_merged_pull_requests_in_period", count: 5, knownCount: 5, workItems: [merged],
            unreadableWorkItems: [], timeUnknownCount: 0)
        XCTAssertNil(view.validPullRequests)
    }

    func testChildRowsNameTheirParentAndTheParentRowCarriesTheFamilyTotal() throws {
        let parent = "wi_20261019_001", child = "wi_20261019_004"
        var view = dashboardView(cost(partial: "6", priced: "6", count: 2), events: 2,
            groups: [activityRow(parent), activityRow(child)])
        view.workItemFamilies = [CostWorkItemFamily(wi: parent, children: [child], costUsd: "6",
                                                    costComplete: false, pullRequestCount: 3)]
        let items = [CostWorkItem(id: parent, title: "父事项", summary: nil, status: "active"),
                     CostWorkItem(id: child, title: "子事项", summary: nil, status: "completed", parent: parent)]
        let rows = view.naming(items).presentationRows
        XCTAssertEqual(rows.map { CostTableColumn.wi.text($0) }, [parent, "↳ " + child])
        XCTAssertEqual(rows.last?.parentWorkItem?.title, "父事项")
        XCTAssertNil(rows.last?.family)
        XCTAssertEqual(rows.first?.family?.text, "含子 WI 合计（本 WI 与 1 个子 WI）：$6.00（已计价部分） · 3 个 PR")
        // Filtering by the child's identity still finds its row.
        var selection = CostTableSelection(); selection.filters[.wi] = child
        XCTAssertEqual(selection.rows(rows).map { $0.key["wi"] }, [child])
        // An unknown PR count stays unknown; an invalid family is not shown.
        view.workItemFamilies = [CostWorkItemFamily(wi: parent, children: [child], costUsd: nil,
                                                    costComplete: false, pullRequestCount: nil)]
        XCTAssertEqual(view.naming(items).presentationRows.first?.family?.text, "含子 WI 合计（本 WI 与 1 个子 WI）：金额未提供 · PR 未提供")
        view.workItemFamilies = [CostWorkItemFamily(wi: parent, children: [], costUsd: "1",
                                                    costComplete: true, pullRequestCount: 0)]
        XCTAssertNil(view.naming(items).presentationRows.first?.family)
    }

    func testAgentReviewedChildOutcomeIsNotShownAsHumanAcceptance() throws {
        let json = #"{"outcome_ref":"o","fact_ref":"f","wi":"wi_20261019_004","responsible":"pulse","status":"accepted","counted":true,"occurred_at":"2026-10-19T02:00:00Z","candidate_ref":"c","artifact_ref":"a","acceptance_ref":"r","review_decisions":[],"review_refs":[],"evidence_refs":[],"title":null,"summary":null,"verification_level":"self_with_controls"}"#
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .iso8601
        let item = try decoder.decode(WIOutcomeItem.self, from: Data(json.utf8))
        XCTAssertEqual(item.statusText, "主会话审核通过（非人类验收）")
        let older = try decoder.decode(WIOutcomeItem.self, from: Data(json.replacingOccurrences(
            of: #","verification_level":"self_with_controls""#, with: "").utf8))
        XCTAssertNil(older.verificationLevel)
        XCTAssertEqual(older.statusText, "已接受")
    }

    func testPullRequestAnomaliesNeverBecomeZeroWhenUnverified() throws {
        let merge: [String: Any] = ["repository": "bigbird-online/Oikos", "number": 12,
            "merged_at": "2026-09-12T01:00:00Z", "merge_commit": String(repeating: "e", count: 40), "title": "Fix"]
        let verified: [String: Any] = [
            "status": "verified", "reason": NSNull(), "observed_at": "2026-09-13T01:00:00Z",
            "since": "2026-09-06T01:00:00Z", "window_days": 7, "enforced_since": "2026-09-10", "repositories": [],
            "unrecorded_merges": ["count": 1, "items": [merge]],
            "unenforced_merges": ["count": 0, "items": []],
            "direct_commits": ["count": 0, "items": []]]
        let doc = try document { $0["pull_request_anomalies"] = verified }
        let anomalies = try XCTUnwrap(doc.pullRequestAnomalies)
        XCTAssertEqual(anomalies.headline, "近 7 天 未经内核合并 1 个 · 直接提交 main 0 个")
        XCTAssertTrue(anomalies.needsAttention)
        XCTAssertNil(anomalies.referenceLine)
        XCTAssertEqual(anomalies.unrecordedMerges?.items.first?.id, "bigbird-online/Oikos#12")
        // Without an enforcement date GitHub merges are a reference count, not an anomaly.
        var unset = verified
        unset["enforced_since"] = NSNull()
        unset["unrecorded_merges"] = ["count": 0, "items": []]
        unset["unenforced_merges"] = ["count": 1, "items": [merge]]
        let reference = try XCTUnwrap(try document { $0["pull_request_anomalies"] = unset }.pullRequestAnomalies)
        XCTAssertEqual(reference.headline, "近 7 天 直接提交 main 0 个")
        XCTAssertEqual(reference.referenceLine, "近 7 天 GitHub 直接合并 1 个（关卡尚未强制）")
        XCTAssertFalse(reference.needsAttention)
        unset["direct_commits"] = ["count": 1, "items": [["repository": "bigbird-online/PULSE",
            "sha": String(repeating: "c", count: 40), "committed_at": "2026-09-12T01:00:00Z", "title": "push", "author": "x"]]]
        XCTAssertTrue(try XCTUnwrap(try document { $0["pull_request_anomalies"] = unset }.pullRequestAnomalies).needsAttention)
        var unverified = verified
        unverified["status"] = "unverified"; unverified["reason"] = "audit_stale"
        let stale = try XCTUnwrap(try document { $0["pull_request_anomalies"] = unverified }.pullRequestAnomalies)
        XCTAssertNil(stale.mergeCount)
        XCTAssertEqual(stale.headline, "PR 产出异常未核验：GitHub 读取已过期")
        // A malformed carrier neither erases the cost document nor reads as zero.
        let broken = try document { $0["pull_request_anomalies"] = "malformed" }
        XCTAssertFalse(broken.views.isEmpty)
        XCTAssertEqual(broken.pullRequestAnomalies?.status, "unverified")
        XCTAssertFalse(broken.pullRequestAnomalies?.needsAttention ?? true)
    }

    func testUnregisteredPullRequestsAreHintsBesideThePeriodCount() throws {
        let item: [String: Any] = ["repository": "bigbird-online/Oikos", "number": 189, "merged_at": "2026-11-04T06:43:15Z",
            "title": "Publish", "head_ref": "hestia/kr1-publish", "agent_hint": "hestia"]
        let week: [String: Any] = ["from_date": "2026-11-02", "to_date": "2026-11-05", "status": "verified", "reason": NSNull(),
            "count": 3, "agents": [["agent": "hestia", "count": 2]], "unhinted_count": 1, "items": [item]]
        let month: [String: Any] = ["from_date": "2026-11-01", "to_date": "2026-11-05", "status": "unverified",
            "reason": "audit_window_short", "count": NSNull(), "agents": [], "unhinted_count": NSNull(), "items": []]
        let anomalies: [String: Any] = [
            "status": "verified", "reason": NSNull(), "observed_at": "2026-11-04T06:00:00Z", "since": "2026-10-28T06:00:00Z",
            "window_days": 7, "enforced_since": NSNull(), "repositories": [],
            "unrecorded_merges": ["count": 0, "items": []], "unenforced_merges": ["count": 3, "items": []],
            "direct_commits": ["count": 0, "items": []],
            "unregistered_pull_requests": ["week": week, "month": month]]
        let doc = try document { $0["pull_request_anomalies"] = anomalies }
        let parsed = try XCTUnwrap(doc.pullRequestAnomalies)
        let thisWeek = try XCTUnwrap(parsed.unregistered(from: "2026-11-02", to: "2026-11-05"))
        XCTAssertEqual(thisWeek.tileText, "另 3 个未登记")
        XCTAssertEqual(thisWeek.headline("本周"), "本周另有 3 个已合并 PR 未经内核登记 · 分支前缀提示（未核实）：hestia 2 · 无提示 1")
        XCTAssertEqual(thisWeek.items.first?.line, "未登记 · Oikos #189 · hestia/kr1-publish · 提示 hestia · Publish")
        // Unknown is never zero: no tile text, and the line says why.
        let thisMonth = try XCTUnwrap(parsed.unregistered(from: "2026-11-01", to: "2026-11-05"))
        XCTAssertNil(thisMonth.knownCount)
        XCTAssertNil(thisMonth.tileText)
        XCTAssertEqual(thisMonth.headline("本月"), "本月未经内核登记的已合并 PR 未核验：GitHub 读取未覆盖本期起点")
        // Hints that do not re-add are not shown as a count.
        var broken = week; broken["unhinted_count"] = 5
        var tampered = anomalies; tampered["unregistered_pull_requests"] = ["week": broken]
        let bad = try XCTUnwrap(try document { $0["pull_request_anomalies"] = tampered }.pullRequestAnomalies)
        XCTAssertNil(bad.unregistered(from: "2026-11-02", to: "2026-11-05")?.knownCount)
        XCTAssertNil(parsed.unregistered(from: "2026-11-04", to: "2026-11-05"))
        // The kernel PR count itself is untouched by the extension.
        XCTAssertEqual(doc.views.map(\.mergedPullRequestText), try document { _ in }.views.map(\.mergedPullRequestText))
    }

    // MARK: gzip transport (exporter sends it when the expanded carrier exceeds 1 MiB)

    private func gzip(_ data: Data) -> Data {
        var stream = z_stream()
        XCTAssertEqual(deflateInit2_(&stream, 9, Z_DEFLATED, 16 + MAX_WBITS, 8, Z_DEFAULT_STRATEGY,
                                     zlibVersion(), Int32(MemoryLayout<z_stream>.size)), Z_OK)
        defer { deflateEnd(&stream) }
        var input = [UInt8](data)
        var output = [UInt8](repeating: 0, count: Int(deflateBound(&stream, uLong(input.count))))
        let count = input.withUnsafeMutableBufferPointer { source in
            output.withUnsafeMutableBufferPointer { target in
                stream.next_in = source.baseAddress
                stream.avail_in = uInt(source.count)
                stream.next_out = target.baseAddress
                stream.avail_out = uInt(target.count)
                XCTAssertEqual(deflate(&stream, Z_FINISH), Z_STREAM_END)
                return target.count - Int(stream.avail_out)
            }
        }
        return Data(output.prefix(count))
    }

    private func transportSnapshot(
        _ edit: (inout [String: Any], Data) -> Void = { _, _ in }
    ) throws -> PulseSnapshot {
        let source = try JSONSerialization.jsonObject(with: fixtureData("cost-dashboard-v1")) as! [String: Any]
        let object = source["cost_dashboard"] as! [String: Any]
        let compressed = gzip(try JSONSerialization.data(withJSONObject: object))
        var carrier: [String: Any] = [:]
        for key in ["schema_version", "generated_at", "expires_at", "source_revision", "status", "sha256"] {
            carrier[key] = object[key]
        }
        carrier["compressed_dashboard"] = compressed.base64EncodedString()
        carrier["compressed_dashboard_sha256"] = CostDashboardCarrier.sha256Hex(compressed)
        edit(&carrier, compressed)
        var base = try JSONSerialization.jsonObject(with: fixtureData("ok-full")) as! [String: Any]
        base["cost_dashboard"] = carrier
        return try SnapshotDecoder().decode(JSONSerialization.data(withJSONObject: base))
    }

    private func withHash(_ carrier: inout [String: Any], _ compressed: Data) {
        carrier["compressed_dashboard"] = compressed.base64EncodedString()
        carrier["compressed_dashboard_sha256"] = CostDashboardCarrier.sha256Hex(compressed)
    }

    func testGzipTransportAndExpandedCarrierDecodeToTheSameDashboard() throws {
        let expanded = try document()
        XCTAssertEqual(try XCTUnwrap(transportSnapshot().costDashboard), expanded)
    }

    func testGzipTransportRefusesChangedHashExtraMemberExcessExpansionAndMetadata() throws {
        let cases: [(String, (inout [String: Any], Data) -> Void)] = [
            ("hash", { carrier, _ in carrier["compressed_dashboard_sha256"] = String(repeating: "0", count: 64) }),
            ("base64", { carrier, _ in carrier["compressed_dashboard"] = "%%" }),
            ("extra member", { carrier, compressed in self.withHash(&carrier, compressed + compressed) }),
            ("expansion", { carrier, _ in
                self.withHash(&carrier, self.gzip(Data(repeating: 0x20, count: CostDashboardCarrier.maxExpandedBytes + 1)))
            }),
            ("metadata", { carrier, _ in carrier["source_revision"] = String(repeating: "d", count: 40) }),
        ]
        for (name, edit) in cases {
            let snapshot = try transportSnapshot(edit)
            // A refused cost carrier leaves the rest of the snapshot intact.
            XCTAssertNil(snapshot.costDashboard, name)
            XCTAssertFalse(snapshot.capabilities.isEmpty, name)
        }
    }

    func testGunzipAcceptsPythonOutputAndBoundsExpansion() throws {
        // gzip.compress(b'{"a":1}\n', mtime=0) from CPython.
        let python = try XCTUnwrap(Data(base64Encoded: "H4sIAAAAAAAC/6tWSlSyMqzlAgBGo+h0CAAAAA=="))
        XCTAssertEqual(try CostDashboardCarrier.gunzip(python, limit: 8), Data("{\"a\":1}\n".utf8))
        XCTAssertThrowsError(try CostDashboardCarrier.gunzip(python, limit: 7)) { error in
            XCTAssertEqual(error as? CostDashboardCarrier.TransportError, .tooLarge)
        }
        XCTAssertThrowsError(try CostDashboardCarrier.gunzip(python.prefix(python.count - 1), limit: 8)) { error in
            XCTAssertEqual(error as? CostDashboardCarrier.TransportError, .stream)
        }
        var corrupted = python
        corrupted[corrupted.count - 6] ^= 1
        XCTAssertThrowsError(try CostDashboardCarrier.gunzip(corrupted, limit: 8)) { error in
            XCTAssertEqual(error as? CostDashboardCarrier.TransportError, .stream)
        }
    }
}
