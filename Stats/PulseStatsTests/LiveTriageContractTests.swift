import XCTest
@testable import PulseStats

/// The judgement that Slice 1's GO was missing.
///
/// Slice 1 was accepted on RSS, CPU, cache hits, false-green and stray
/// processes — five criteria that all measure whether the *app itself* is
/// healthy. None of them asked the only question that matters: when production
/// actually breaks, does this thing say so? (Owner, 2026-07-25: 「完全没用，
/// 这么大的事故，它居然一无所知」.) That GO has been withdrawn.
///
/// So this suite decodes a **real production snapshot**, captured 2026-07-26
/// from the live exporter, through the real decoder and the real view models.
/// Not a hand-written fixture: hand-written fixtures only ever contain the
/// shapes their author already thought of, and the whole failure above was a
/// shape nobody thought of. `exporter/stats-incident-replay.py` is the same
/// discipline on the Python side.
///
/// If the exporter's contract drifts, these tests fail here rather than
/// silently rendering an empty panel on the owner's screen.
final class LiveTriageContractTests: XCTestCase {
    private func liveSnapshot() throws -> PulseSnapshot {
        try SnapshotDecoder().decode(try fixtureData("live-v3-triage"))
    }

    func testRealProductionSnapshotDecodes() throws {
        let snapshot = try liveSnapshot()
        XCTAssertFalse(snapshot.alerts.isEmpty, "a production capture always carries converged events")
        XCTAssertFalse(snapshot.capabilities.isEmpty)
    }

    /// Every alert must land in a tier, and the tiers must partition the list.
    ///
    /// Asserted as an invariant, not as today's figures. The first version of
    /// this test pinned `act == 10 / watch == 60 / done == 74`; hours later the
    /// recovery rules landed and the same capture read 5 / 65 / 70. Those
    /// numbers move with a rolling seven-day window and with whatever
    /// production is doing — pinning them means the suite goes red on refresh
    /// for reasons that have nothing to do with the contract. What must never
    /// move is that every alert is accounted for exactly once, and that a
    /// missing or unknown tier falls to `watch` rather than climbing into
    /// 要你出手.
    func testTiersPartitionEveryAlert() throws {
        let snapshot = try liveSnapshot()
        let tallied = snapshot.alerts.reduce(into: [AlertTier: Int]()) { counts, alert in
            counts[alert.tier, default: 0] += 1
        }
        let summed = (tallied[.act] ?? 0) + (tallied[.watch] ?? 0) + (tallied[.done] ?? 0)
        XCTAssertEqual(summed, snapshot.alerts.count, "every alert belongs to exactly one tier")
        XCTAssertFalse(snapshot.alerts.isEmpty)
    }

    /// The producer reports its own counts; the app must not disagree with them.
    /// Two numbers for one fact is how a panel starts lying quietly.
    func testProducerTierCountsMatchTheAlertsThemselves() throws {
        let snapshot = try liveSnapshot()
        let model = TriageViewModel(snapshot: snapshot)

        XCTAssertEqual(model.act.count, snapshot.alertTiers.act)
        XCTAssertEqual(model.watch.count, snapshot.alertTiers.watch)
        XCTAssertEqual(model.done.count, snapshot.alertTiers.done)
    }

    /// Reducer-backed incidents used to decode successfully and then disappear:
    /// no view-model collection consumed them. The production capture keeps
    /// this as a conservation invariant — every reported open incident has one
    /// current-state card source, while resolved history stays out.
    func testEveryReportedOpenIncidentHasOneVisibleCardSource() throws {
        let snapshot = try liveSnapshot()
        let model = TriageViewModel(snapshot: snapshot)

        XCTAssertEqual(model.openIncidents.count, snapshot.overall.openIncidentCount)
        XCTAssertEqual(
            Set(model.openIncidents.map(\.episodeID)).count,
            model.openIncidents.count,
            "one episode must not render twice"
        )
        XCTAssertTrue(model.openIncidents.allSatisfy { $0.state == .open })
        for incident in model.openIncidents {
            XCTAssertFalse(incident.title.isEmpty)
            XCTAssertFalse(incident.impact.isEmpty)
            XCTAssertFalse(incident.suggestedAction.isEmpty)
        }
    }

    /// The reason string must carry the escalation's own words. Before v3 every
    /// alert rendered the same sentence — "A reviewed structured escalation rule
    /// matched" — which is why a real incident could sit on screen unread.
    func testActAlertsCarryHumanReadableCause() throws {
        let model = TriageViewModel(snapshot: try liveSnapshot())
        let act = try XCTUnwrap(model.act.first)

        XCTAssertFalse(act.body.isEmpty, "the escalation's own message must survive")
        XCTAssertFalse(
            act.body.contains("A reviewed structured escalation rule matched"),
            "the placeholder sentence must never reach the panel again"
        )
        let meta = try XCTUnwrap(act.meta, "起止与次数 belong in the metadata line")
        XCTAssertFalse(meta.isEmpty)
    }

    /// Whatever is in 要你出手 must be legible enough to act on.
    ///
    /// This deliberately does *not* assert that a particular incident is in the
    /// tier. Which incidents are live changes hourly and ages out of the
    /// seven-day window; the Python side owns that question against fixed
    /// historical corpus (`Stats/exporter/stats-incident-replay.py`, exit 0 required).
    /// What this side owns is that an `act` row, whichever one it is, arrives
    /// with a subject, a cause and a timestamp — the three things without which
    /// the owner cannot act.
    func testEveryActAlertIsActionable() throws {
        let model = TriageViewModel(snapshot: try liveSnapshot())

        for alert in model.act {
            XCTAssertFalse(alert.title.isEmpty, "an act row needs a subject")
            XCTAssertFalse(alert.body.isEmpty, "an act row needs the escalation's own words: \(alert.title)")
            XCTAssertNotNil(alert.meta, "an act row needs 起止与次数: \(alert.title)")
        }
    }

    /// Menu bar reads the producer's act count directly — the one number that
    /// decides whether the owner is interrupted at all. The count itself moves;
    /// what must hold is that the bar badge and the panel never disagree, and
    /// the panel header leaves the count to that badge and its card.
    func testMenuBarStatusAgreesWithTheActTier() throws {
        let snapshot = try liveSnapshot()
        let model = TriageViewModel(snapshot: snapshot)
        let menu = MenuBarViewModel(metrics: SnapshotMetrics(snapshot), alertTiers: snapshot.alertTiers,
            hasSnapshot: true, presentation: StatusPresentation(runtimeHealth: snapshot.overall.runtimeHealth,
                                                                coverage: snapshot.overall.coverage))

        XCTAssertEqual(model.tiers.act, model.act.count)
        XCTAssertEqual(menu.menuWIText, String(model.tiers.act))
        XCTAssertFalse(model.statusText?.contains("要出手") ?? false)
        if model.tiers.act == 0 && snapshot.overall.runtimeHealth == .ok {
            XCTAssertEqual(model.statusText, "正常")
        }
    }

    /// The footer must partition the dynamic inventory into all four maturity
    /// states. Partial and retired capabilities must neither inflate validated
    /// coverage nor disappear into an unexplained arithmetic gap.
    func testCapabilityTallyDoesNotInflateCoverage() throws {
        let snapshot = try liveSnapshot()
        let model = TriageViewModel(snapshot: snapshot)
        let groups = Dictionary(grouping: snapshot.capabilities, by: \.implementationStatus)
            .mapValues { Set($0.map(\.id)) }
        let validated = groups[.activeValidated] ?? []
        let partial = groups[.activePartial] ?? []
        let notImplemented = groups[.notImplemented] ?? []
        let retired = groups[.retired] ?? []
        let union = validated
            .union(partial)
            .union(notImplemented)
            .union(retired)
        let inventory = Set(snapshot.capabilities.map(\.id))

        XCTAssertGreaterThan(model.capabilityTotal, 0)
        XCTAssertEqual(inventory.count, snapshot.capabilities.count, "capability ids must be unique")
        XCTAssertEqual(model.capabilityTotal, inventory.count)
        XCTAssertEqual(model.capabilityValidated, validated.count)
        XCTAssertEqual(model.capabilityPartial, partial.count)
        XCTAssertEqual(model.capabilityNotImplemented, notImplemented.count)
        XCTAssertEqual(model.capabilityRetired, retired.count)
        XCTAssertEqual(union, inventory, "the four groups must cover every capability id")
        XCTAssertEqual(
            model.capabilityValidated
                + model.capabilityPartial
                + model.capabilityNotImplemented
                + model.capabilityRetired,
            model.capabilityTotal,
            "the four mutually exclusive groups must conserve the dynamic total"
        )
    }
}
