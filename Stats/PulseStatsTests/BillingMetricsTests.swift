import AppKit
import SwiftUI
import XCTest
@testable import PulseStats

final class BillingMetricsTests: XCTestCase {
    func testValidBundleUsesIntegerCentsAndConserves() throws {
        let metrics = SnapshotMetrics(bundle(rows: [
            row("b", 1, 12_500), row("a", 1, 12_500),
        ]))
        let facts = try XCTUnwrap(metrics.billingFacts)
        let attribution = try XCTUnwrap(metrics.billingAttribution)
        XCTAssertEqual(facts.totalCents, 27_000)
        XCTAssertEqual(attribution.agents.map(\.agent), ["a", "b"])
        XCTAssertEqual(attribution.agents.reduce(0) { $0 + $1.attributedCents },
                       attribution.attributedCents)
        XCTAssertEqual(attribution.attributedCents + attribution.unattributedCents,
                       facts.totalCents)
    }

    func testMalformedFactsFailClosedAsAWhole() {
        var duplicate = componentRows
        duplicate.append(componentRows[0])
        var negative = componentRows
        negative[0]["amount_cents"] = .number(-1)
        for metrics in [
            facts(rows: duplicate), facts(rows: negative), facts(total: 26_999),
            facts(total: 27_000.5), [],
        ] {
            XCTAssertNil(SnapshotMetrics(metrics).billingFacts)
        }
    }

    func testMalformedAttributionFailsClosedAsAWhole() {
        let valid = row("pulse", 100, 25_000)
        var negative = valid
        negative["output_tokens"] = .number(-1)
        let missing = bundle(rows: [valid]).filter {
            $0.id != MetricID.billingUnattributedCents
        }
        var mixed = bundle(rows: [valid]).filter {
            $0.id != MetricID.billingByAgent
        }
        mixed.append(metric(
            MetricID.billingByAgent, .rows([valid]),
            quality: .validated, samples: 1
        ))
        var cases = [
            bundle(rows: [valid, valid]),
            bundle(rows: [negative]),
            bundle(rows: [valid], attributed: 24_999),
            bundle(
                rows: [row("pulse", 100, 25_001)],
                attributed: 25_001,
                unattributed: 1_999
            ),
            missing, mixed,
        ]
        for period in [
            [],
            [periodRow, periodRow],
            [period(["timezone": .string("UTC")])],
            [period(["month": .string("July")])],
            [period(["coverage": .string("unknown")])],
        ] {
            cases.append(bundle(rows: [valid], period: period))
        }
        for metrics in cases {
            XCTAssertNil(SnapshotMetrics(metrics).billingAttribution)
        }
    }

    func testUnknownMetricValueNeverBecomesZero() {
        let unknown = metric(
            MetricID.billingTotalCents, .number(0),
            status: .unknown, quality: .unavailable
        )
        XCTAssertNil(SnapshotMetrics([unknown]).billingFacts)
    }

    func testManagedCostShadowParsesExactBundleAndKeepsCashOutsideManagedCost() throws {
        let metrics = managedCostShadowBundle()
        let shadow = try XCTUnwrap(SnapshotMetrics(metrics).managedCostShadow)
        XCTAssertEqual(shadow.summary.managedCostUSD, Decimal(200))
        XCTAssertEqual(shadow.summary.actualOpenRouterUSD, Decimal(string: "99.99"))
        XCTAssertEqual(shadow.agents.reduce(Decimal.zero) { $0 + $1.managedCostUSD },
                       shadow.summary.managedCostUSD)
        XCTAssertEqual(shadow.agents.first?.agent, "unallocated")

        let model = RelayViewModel(
            metrics: SnapshotMetrics(metrics), hasSnapshot: true
        )
        XCTAssertEqual(model.managedCostShadow, shadow)
    }

    func testManagedCostShadowAbsenceOrDamagedBundleFallsBackToLegacyOnly() {
        XCTAssertNil(SnapshotMetrics([]).managedCostShadow)

        var damaged = managedCostShadowSummaryRow
        damaged["managed_cost_usd"] = .string("0")
        XCTAssertNil(SnapshotMetrics(managedCostShadowBundle(summary: damaged)).managedCostShadow)

        let unknown = Metric(
            id: MetricID.managedCostShadowSummary,
            capabilityID: "compute_cost.shadow",
            status: .unknown, value: .null, unit: "list", window: "month_cst",
            sampleCount: nil, quality: .unavailable, scope: "test"
        )
        XCTAssertNil(SnapshotMetrics([unknown]).managedCostShadow)
    }

    func testManagedCostShadowRejectsOverflowingIntegerConservationWithoutTrap() {
        var damaged = managedCostShadowSummaryRow
        damaged["event_count"] = .number(1)
        damaged["priced_event_count"] = .number(5_000_000_000_000_000_000)
        damaged["unpriced_event_count"] = .number(5_000_000_000_000_000_000)
        XCTAssertNil(
            SnapshotMetrics(managedCostShadowBundle(summary: damaged)).managedCostShadow
        )
    }


    @MainActor
    func testRetiredLegacyAndShadowBillsNeverRenderInNewDashboard() async throws {
        let base = try SnapshotDecoder().decode(try fixtureData("ok-full"))
        let oldMetrics = bundle(rows: [row("pulse", 100, 25_000)])
        for values in [base.metrics + oldMetrics, base.metrics + oldMetrics + managedCostShadowBundle()] {
            let identifiers = await renderedViewIdentifiers(snapshot: replacingMetrics(in: base, with: values))
            XCTAssertTrue(identifiers.contains("cost-dashboard"))
            XCTAssertFalse(identifiers.contains("compute-cost-shadow-title"))
            XCTAssertFalse(identifiers.contains("legacy-monthly-cost-title"))
        }
    }

    private func replacingMetrics(in base: PulseSnapshot, with metrics: [Metric]) -> PulseSnapshot {
        PulseSnapshot(
            schemaVersion: base.schemaVersion,
            generatedAt: base.generatedAt,
            expiresAt: base.expiresAt,
            baselineVersion: base.baselineVersion,
            overall: base.overall,
            capabilities: base.capabilities,
            incidents: base.incidents,
            metrics: metrics,
            alerts: base.alerts,
            alertTiers: base.alertTiers,
            provenance: base.provenance
        )
    }

    @MainActor
    private func renderedViewIdentifiers(snapshot: PulseSnapshot) async -> Set<String> {
        let store = SnapshotStore(
            transport: DataSnapshotTransport(
                result: .failure(ErrorBox(message: "s2-render-uat-offline"))
            ),
            cache: MemorySnapshotCache(snapshot: snapshot),
            now: { snapshot.generatedAt.addingTimeInterval(30) }
        )
        await store.refresh()

        let hosting = NSHostingView(rootView: StatsPopoverView(store: store))
        hosting.frame = NSRect(x: 0, y: 0, width: 420, height: 580)
        hosting.layoutSubtreeIfNeeded()
        try? await Task.sleep(nanoseconds: 200_000_000)
        hosting.layoutSubtreeIfNeeded()
        return collectViewIdentifiers(from: hosting)
    }

    @MainActor
    private func collectViewIdentifiers(from root: NSView) -> Set<String> {
        var identifiers = Set<String>()

        func visit(_ view: NSView) {
            if let identifier = view.identifier?.rawValue, !identifier.isEmpty {
                identifiers.insert(identifier)
            }
            for subview in view.subviews {
                visit(subview)
            }
        }

        visit(root)
        return identifiers
    }

    private var componentRows: [[String: MetricScalar]] {
        [
            [
                "component_id": .string("claude_code"),
                "amount_cents": .number(25_000),
                "currency": .string("USD"),
                "attribution": .string("cc_mtd_output_share"),
            ],
            [
                "component_id": .string("codex"),
                "amount_cents": .number(2_000),
                "currency": .string("USD"),
                "attribution": .string("unattributed_no_agent_source"),
            ],
        ]
    }

    private var managedCostShadowSummaryRow: [String: MetricScalar] {
        [
            "schema": .string("managed_cost_snapshot.v1"),
            "snapshot_id": .string("mcs1_" + String(repeating: "a", count: 64)),
            "settlement_status": .string("provisional"),
            "product_month": .string("2026-08"),
            "timezone": .string("Asia/Shanghai"),
            "generated_at": .string("2026-08-17T08:00:00Z"),
            "usage_event_set_digest": .string("uevset1_" + String(repeating: "b", count: 64)),
            "price_snapshot_id": .string("orprice1_" + String(repeating: "c", count: 64)),
            "price_model_id": .string("deepseek/reference"),
            "price_canonical_slug": .string("deepseek/reference-202608"),
            "price_retrieved_at": .string("2026-08-17T07:00:00Z"),
            "source_complete": .boolean(false),
            "event_count": .number(0),
            "priced_event_count": .number(0),
            "unpriced_event_count": .number(0),
            "partial_event_count": .number(0),
            "actual_openrouter_coverage": .string("partial"),
            "quality_status": .string("partial"),
            "quality_flags": .string("actual_openrouter_cash_partial,source_coverage_incomplete"),
            "gross_reference_cost_usd": .string("0"),
            "api_reference_cost_usd": .string("0"),
            "pro_reference_cost_usd": .string("0"),
            "pro_allocated_cents": .number(0),
            "unallocated_cents": .number(20_000),
            "pro_settled_cost_usd": .string("200"),
            "pro_settlement_adjustment_usd": .string("200"),
            "managed_cost_usd": .string("200"),
            "actual_openrouter_usd": .string("99.99"),
            "actual_openrouter_snapshot_digest": .string(
                "oractivity1_" + String(repeating: "d", count: 64)
            ),
            "actual_openrouter_window_start_utc_day": .string("2026-07-18"),
            "actual_openrouter_window_end_utc_day": .string("2026-08-16"),
        ]
    }

    private var managedCostAgentRows: [[String: MetricScalar]] {
        [[
            "agent": .string("unallocated"),
            "event_count": .number(0),
            "priced_event_count": .number(0),
            "unpriced_event_count": .number(0),
            "api_reference_cost_usd": .string("0"),
            "pro_reference_cost_usd": .string("0"),
            "pro_allocated_cents": .number(0),
            "unallocated_cents": .number(20_000),
            "managed_cost_usd": .string("200"),
        ]]
    }

    private func managedCostShadowBundle(
        summary: [String: MetricScalar]? = nil
    ) -> [Metric] {
        [
            Metric(
                id: MetricID.managedCostShadowSummary,
                capabilityID: "compute_cost.shadow", status: .available,
                value: .rows([summary ?? managedCostShadowSummaryRow]), unit: "list",
                window: "month_cst", sampleCount: 0, quality: .descriptive,
                scope: "test"
            ),
            Metric(
                id: MetricID.managedCostShadowByAgent,
                capabilityID: "compute_cost.shadow", status: .available,
                value: .rows(managedCostAgentRows), unit: "list",
                window: "month_cst", sampleCount: 0, quality: .descriptive,
                scope: "test"
            ),
        ]
    }

    private var periodRow: [String: MetricScalar] {
        period([:])
    }

    private func period(
        _ overrides: [String: MetricScalar]
    ) -> [String: MetricScalar] {
        var value: [String: MetricScalar] = [
            "month": .string("2026-07"),
            "timezone": .string("Asia/Shanghai"),
            "days_covered": .number(0),
            "days_expected": .number(25),
            "coverage": .string("partial"),
        ]
        value.merge(overrides) { _, latest in latest }
        return value
    }

    private func row(
        _ agent: String, _ tokens: Double, _ cents: Double
    ) -> [String: MetricScalar] {
        [
            "agent": .string(agent),
            "output_tokens": .number(tokens),
            "attributed_cents": .number(cents),
        ]
    }

    private func facts(
        rows: [[String: MetricScalar]]? = nil,
        total: Double = 27_000
    ) -> [Metric] {
        [
            metric(MetricID.billingComponents, .rows(rows ?? componentRows)),
            metric(MetricID.billingTotalCents, .number(total)),
        ]
    }

    private func bundle(
        rows: [[String: MetricScalar]],
        period: [[String: MetricScalar]]? = nil,
        attributed: Double = 25_000,
        unattributed: Double = 2_000
    ) -> [Metric] {
        facts() + [
            metric(
                MetricID.billingPeriod, .rows(period ?? [periodRow]),
                quality: .descriptive, samples: 1
            ),
            metric(
                MetricID.billingByAgent, .rows(rows),
                quality: .descriptive, samples: 1
            ),
            metric(
                MetricID.billingAttributedCents, .number(attributed),
                quality: .descriptive, samples: 1
            ),
            metric(
                MetricID.billingUnattributedCents, .number(unattributed),
                quality: .descriptive, samples: 1
            ),
        ]
    }

    private func metric(
        _ id: String, _ value: MetricValue,
        status: MetricStatus = .available,
        quality: MetricQuality = .validated,
        samples: Int? = nil
    ) -> Metric {
        Metric(
            id: id, capabilityID: "s1.request_trace_capture",
            status: status, value: value,
            unit: id.hasSuffix("_cents") ? "cents" : "list",
            window: "month_to_date_cst", sampleCount: samples,
            quality: quality, scope: "test"
        )
    }
}
