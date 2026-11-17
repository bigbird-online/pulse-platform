import Foundation

private func billingInteger(_ scalar: MetricScalar?) -> Int? {
    guard let number = scalar?.number,
          number.isFinite, number >= 0, number.rounded() == number
    else { return nil }
    return safeInt(number)
}

private func exactBillingSum(_ values: [Int]) -> Int? {
    var total = 0
    for value in values {
        let (next, overflow) = total.addingReportingOverflow(value)
        guard !overflow else { return nil }
        total = next
    }
    return total
}

struct BillingComponent: Equatable, Identifiable, Sendable {
    let componentID: String
    let amountCents: Int
    let currency: String
    let attribution: String

    var id: String { componentID }

    init?(row: [String: MetricScalar]) {
        guard Set(row.keys) == [
            "component_id", "amount_cents", "currency", "attribution",
        ],
        let componentID = row["component_id"]?.string,
        !componentID.isEmpty,
        componentID == componentID.trimmingCharacters(in: .whitespacesAndNewlines),
        let amountCents = billingInteger(row["amount_cents"]),
        row["currency"]?.string == "USD",
        let attribution = row["attribution"]?.string,
        ["cc_mtd_output_share", "unattributed_no_agent_source"].contains(attribution)
        else { return nil }
        self.componentID = componentID
        self.amountCents = amountCents
        currency = "USD"
        self.attribution = attribution
    }
}

enum BillingCoverage: String, Equatable, Sendable {
    case full
    case partial
}

struct BillingPeriod: Equatable, Sendable {
    let month: String
    let daysCovered: Int
    let daysExpected: Int
    let coverage: BillingCoverage

    init?(row: [String: MetricScalar]) {
        guard Set(row.keys) == [
            "month", "timezone", "days_covered", "days_expected", "coverage",
        ],
        let month = row["month"]?.string,
        month.range(
            of: #"^\d{4}-(0[1-9]|1[0-2])$"#,
            options: .regularExpression
        ) != nil,
        row["timezone"]?.string == "Asia/Shanghai",
        let daysCovered = billingInteger(row["days_covered"]),
        let daysExpected = billingInteger(row["days_expected"]),
        daysExpected > 0, daysCovered <= daysExpected,
        let rawCoverage = row["coverage"]?.string,
        let coverage = BillingCoverage(rawValue: rawCoverage),
        (coverage == .full && daysCovered == daysExpected)
            || (coverage == .partial && daysCovered < daysExpected)
        else { return nil }
        self.month = month
        self.daysCovered = daysCovered
        self.daysExpected = daysExpected
        self.coverage = coverage
    }
}

struct AgentBilling: Equatable, Identifiable, Sendable {
    let agent: String
    let outputTokens: Int
    let attributedCents: Int

    var id: String { agent }

    init?(row: [String: MetricScalar]) {
        guard Set(row.keys) == ["agent", "output_tokens", "attributed_cents"],
              let agent = row["agent"]?.string,
              !agent.isEmpty,
              agent == agent.trimmingCharacters(in: .whitespacesAndNewlines),
              let outputTokens = billingInteger(row["output_tokens"]),
              let attributedCents = billingInteger(row["attributed_cents"])
        else { return nil }
        self.agent = agent
        self.outputTokens = outputTokens
        self.attributedCents = attributedCents
    }
}

struct BillingFacts: Equatable, Sendable {
    let components: [BillingComponent]
    let totalCents: Int
}

struct BillingAttribution: Equatable, Sendable {
    let period: BillingPeriod
    let agents: [AgentBilling]
    let attributedCents: Int
    let unattributedCents: Int
    let quality: MetricQuality
    let reason: String?
}

extension SnapshotMetrics {
    var billingFacts: BillingFacts? {
        guard
            let componentMetric = rawMetric(MetricID.billingComponents),
            let totalMetric = rawMetric(MetricID.billingTotalCents),
            componentMetric.status == .available,
            totalMetric.status == .available,
            componentMetric.quality == .validated,
            totalMetric.quality == .validated,
            componentMetric.window == "month_to_date_cst",
            totalMetric.window == "month_to_date_cst",
            componentMetric.unit == "list",
            totalMetric.unit == "cents",
            componentMetric.sampleCount == nil,
            totalMetric.sampleCount == nil,
            let componentRows = componentMetric.value.rows,
            !componentRows.isEmpty,
            let totalCents = billingScalarInteger(totalMetric),
            let components = decodeAll(componentRows, BillingComponent.init(row:)),
            Set(components.map(\.componentID)).count == components.count,
            exactBillingSum(components.map(\.amountCents)) == totalCents
        else { return nil }
        return BillingFacts(components: components, totalCents: totalCents)
    }

    var billingAttribution: BillingAttribution? {
        guard let facts = billingFacts else { return nil }
        let ids = [
            MetricID.billingPeriod, MetricID.billingByAgent,
            MetricID.billingAttributedCents, MetricID.billingUnattributedCents,
        ]
        let metrics = ids.compactMap(rawMetric)
        guard metrics.count == ids.count,
              metrics.allSatisfy({ $0.status == .available }),
              metrics.allSatisfy({ $0.window == "month_to_date_cst" }),
              let first = metrics.first,
              [.validated, .descriptive].contains(first.quality),
              metrics.allSatisfy({ $0.quality == first.quality }),
              let sampleCount = first.sampleCount, sampleCount >= 0,
              metrics.allSatisfy({ $0.sampleCount == sampleCount }),
              metrics[0].unit == "list", metrics[1].unit == "list",
              metrics[2].unit == "cents", metrics[3].unit == "cents",
              let periodRows = metrics[0].value.rows, periodRows.count == 1,
              let period = BillingPeriod(row: periodRows[0]),
              (period.coverage == .full && first.quality == .validated)
                || (period.coverage == .partial && first.quality == .descriptive),
              period.coverage == .full || sampleCount > 0,
              let agentRows = metrics[1].value.rows,
              let agents = decodeAll(agentRows, AgentBilling.init(row:)),
              Set(agents.map(\.agent)).count == agents.count,
              let attributed = billingScalarInteger(metrics[2]),
              let unattributed = billingScalarInteger(metrics[3]),
              exactBillingSum(agents.map(\.attributedCents)) == attributed,
              exactBillingSum([attributed, unattributed]) == facts.totalCents,
              let requiredUnattributed = exactBillingSum(
                facts.components
                    .filter {
                        $0.attribution == "unattributed_no_agent_source"
                    }
                    .map(\.amountCents)
              ),
              unattributed >= requiredUnattributed
        else { return nil }
        return BillingAttribution(
            period: period,
            agents: agents.sorted {
                $0.outputTokens == $1.outputTokens
                    ? $0.agent < $1.agent
                    : $0.outputTokens > $1.outputTokens
            },
            attributedCents: attributed,
            unattributedCents: unattributed,
            quality: first.quality,
            reason: metrics[1].reason
        )
    }

    private func billingScalarInteger(_ metric: Metric) -> Int? {
        guard case let .number(value) = metric.value else { return nil }
        return billingInteger(.number(value))
    }

    private func decodeAll<T>(
        _ rows: [[String: MetricScalar]],
        _ decode: ([String: MetricScalar]) -> T?
    ) -> [T]? {
        let decoded = rows.compactMap(decode)
        return decoded.count == rows.count ? decoded : nil
    }
}

// MARK: - ComputeCost shadow

private func managedDecimal(
    _ scalar: MetricScalar?,
    allowNegative: Bool = false
) -> Decimal? {
    guard let raw = scalar?.string,
          !raw.isEmpty,
          !raw.contains("e"), !raw.contains("E"),
          let value = Decimal(string: raw, locale: Locale(identifier: "en_US_POSIX")),
          NSDecimalNumber(decimal: value).stringValue == raw,
          allowNegative || value >= 0
    else { return nil }
    return value
}

private func matches(_ value: String, _ pattern: String) -> Bool {
    value.range(of: pattern, options: .regularExpression) != nil
}

struct ManagedCostShadowSummary: Equatable, Sendable {
    let snapshotID: String
    let productMonth: String
    let priceSnapshotID: String
    let priceModelID: String
    let eventCount: Int
    let pricedEventCount: Int
    let unpricedEventCount: Int
    let partialEventCount: Int
    let sourceComplete: Bool
    let qualityStatus: String
    let qualityFlags: String
    let grossReferenceCostUSD: Decimal
    let apiReferenceCostUSD: Decimal
    let proReferenceCostUSD: Decimal
    let proAllocatedCents: Int
    let unallocatedCents: Int
    let proSettledCostUSD: Decimal
    let proSettlementAdjustmentUSD: Decimal
    let managedCostUSD: Decimal
    let actualOpenRouterUSD: Decimal?
    let actualOpenRouterCoverage: String

    init?(row: [String: MetricScalar]) {
        let required: Set<String> = [
            "schema", "snapshot_id", "settlement_status", "product_month",
            "timezone", "generated_at", "usage_event_set_digest",
            "price_snapshot_id", "price_model_id", "price_canonical_slug",
            "price_retrieved_at", "source_complete", "event_count",
            "priced_event_count", "unpriced_event_count", "partial_event_count",
            "actual_openrouter_coverage", "quality_status", "quality_flags",
            "gross_reference_cost_usd", "api_reference_cost_usd",
            "pro_reference_cost_usd", "pro_allocated_cents",
            "unallocated_cents", "pro_settled_cost_usd",
            "pro_settlement_adjustment_usd", "managed_cost_usd",
            "actual_openrouter_usd",
        ]
        let cashEvidence: Set<String> = [
            "actual_openrouter_snapshot_digest",
            "actual_openrouter_window_start_utc_day",
            "actual_openrouter_window_end_utc_day",
        ]
        guard Set(row.keys) == required || Set(row.keys) == required.union(cashEvidence),
              row["schema"]?.string == "managed_cost_snapshot.v1",
              row["settlement_status"]?.string == "provisional",
              row["timezone"]?.string == "Asia/Shanghai",
              let generatedAt = row["generated_at"]?.string,
              matches(generatedAt, #"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"#),
              let snapshotID = row["snapshot_id"]?.string,
              matches(snapshotID, #"^mcs1_[0-9a-f]{64}$"#),
              let productMonth = row["product_month"]?.string,
              matches(productMonth, #"^\d{4}-(0[1-9]|1[0-2])$"#),
              let usageDigest = row["usage_event_set_digest"]?.string,
              matches(usageDigest, #"^uevset1_[0-9a-f]{64}$"#),
              let priceSnapshotID = row["price_snapshot_id"]?.string,
              matches(priceSnapshotID, #"^orprice1_[0-9a-f]{64}$"#),
              let priceModelID = row["price_model_id"]?.string,
              !priceModelID.isEmpty,
              let priceCanonicalSlug = row["price_canonical_slug"]?.string,
              !priceCanonicalSlug.isEmpty,
              let priceRetrievedAt = row["price_retrieved_at"]?.string,
              matches(priceRetrievedAt, #"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"#),
              let eventCount = billingInteger(row["event_count"]),
              let pricedEventCount = billingInteger(row["priced_event_count"]),
              let unpricedEventCount = billingInteger(row["unpriced_event_count"]),
              let partialEventCount = billingInteger(row["partial_event_count"]),
              exactBillingSum([pricedEventCount, unpricedEventCount]) == eventCount,
              partialEventCount <= eventCount,
              let sourceComplete = row["source_complete"]?.bool,
              let qualityStatus = row["quality_status"]?.string,
              ["complete", "partial"].contains(qualityStatus),
              let qualityFlags = row["quality_flags"]?.string,
              (qualityStatus == "complete" && qualityFlags.isEmpty)
                || (qualityStatus == "partial" && !qualityFlags.isEmpty),
              let gross = managedDecimal(row["gross_reference_cost_usd"]),
              let api = managedDecimal(row["api_reference_cost_usd"]),
              let pro = managedDecimal(row["pro_reference_cost_usd"]),
              let allocated = billingInteger(row["pro_allocated_cents"]),
              let unallocated = billingInteger(row["unallocated_cents"]),
              exactBillingSum([allocated, unallocated]) == 20_000,
              let settled = managedDecimal(row["pro_settled_cost_usd"]),
              let adjustment = managedDecimal(
                row["pro_settlement_adjustment_usd"], allowNegative: true
              ),
              let managed = managedDecimal(row["managed_cost_usd"]),
              gross == api + pro,
              settled == Decimal(200),
              adjustment == settled - pro,
              managed == api + settled,
              let cashCoverage = row["actual_openrouter_coverage"]?.string,
              ["unavailable", "partial"].contains(cashCoverage)
        else { return nil }

        let actual = managedDecimal(row["actual_openrouter_usd"])
        if cashCoverage == "unavailable" {
            guard actual == nil, Set(row.keys) == required else { return nil }
        } else {
            guard actual != nil,
                  Set(row.keys) == required.union(cashEvidence),
                  let cashDigest = row["actual_openrouter_snapshot_digest"]?.string,
                  matches(cashDigest, #"^oractivity1_[0-9a-f]{64}$"#),
                  let cashStart = row["actual_openrouter_window_start_utc_day"]?.string,
                  matches(cashStart, #"^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$"#),
                  let cashEnd = row["actual_openrouter_window_end_utc_day"]?.string,
                  matches(cashEnd, #"^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$"#)
            else { return nil }
        }
        let flags: [String] = qualityFlags.isEmpty
            ? []
            : qualityFlags.split(separator: ",", omittingEmptySubsequences: false).map(String.init)
        guard flags == flags.sorted(), Set(flags).count == flags.count,
              flags.contains("source_coverage_incomplete") == !sourceComplete,
              flags.contains("event_evidence_partial") == (partialEventCount > 0),
              flags.contains("unpriced_events") == (unpricedEventCount > 0),
              flags.contains("actual_openrouter_cash_unavailable") == (actual == nil),
              flags.contains("actual_openrouter_cash_partial") == (cashCoverage == "partial")
        else { return nil }
        self.snapshotID = snapshotID
        self.productMonth = productMonth
        self.priceSnapshotID = priceSnapshotID
        self.priceModelID = priceModelID
        self.eventCount = eventCount
        self.pricedEventCount = pricedEventCount
        self.unpricedEventCount = unpricedEventCount
        self.partialEventCount = partialEventCount
        self.sourceComplete = sourceComplete
        self.qualityStatus = qualityStatus
        self.qualityFlags = qualityFlags
        grossReferenceCostUSD = gross
        apiReferenceCostUSD = api
        proReferenceCostUSD = pro
        proAllocatedCents = allocated
        unallocatedCents = unallocated
        proSettledCostUSD = settled
        proSettlementAdjustmentUSD = adjustment
        managedCostUSD = managed
        actualOpenRouterUSD = actual
        actualOpenRouterCoverage = cashCoverage
    }
}

struct ManagedCostAgent: Equatable, Identifiable, Sendable {
    let agent: String
    let eventCount: Int
    let pricedEventCount: Int
    let unpricedEventCount: Int
    let apiReferenceCostUSD: Decimal
    let proReferenceCostUSD: Decimal
    let proAllocatedCents: Int
    let unallocatedCents: Int
    let managedCostUSD: Decimal

    var id: String { agent }

    init?(row: [String: MetricScalar]) {
        guard Set(row.keys) == [
            "agent", "event_count", "priced_event_count", "unpriced_event_count",
            "api_reference_cost_usd", "pro_reference_cost_usd",
            "pro_allocated_cents", "unallocated_cents", "managed_cost_usd",
        ],
        let agent = row["agent"]?.string, !agent.isEmpty,
        let events = billingInteger(row["event_count"]),
        let priced = billingInteger(row["priced_event_count"]),
        let unpriced = billingInteger(row["unpriced_event_count"]),
        exactBillingSum([priced, unpriced]) == events,
        let api = managedDecimal(row["api_reference_cost_usd"]),
        let pro = managedDecimal(row["pro_reference_cost_usd"]),
        let allocated = billingInteger(row["pro_allocated_cents"]),
        let unallocated = billingInteger(row["unallocated_cents"]),
        let managed = managedDecimal(row["managed_cost_usd"])
        else { return nil }
        if agent == "unallocated" {
            guard events == 0, api == 0, pro == 0, allocated == 0,
                  managed == Decimal(unallocated) / Decimal(100)
            else { return nil }
        } else {
            guard unallocated == 0,
                  managed == api + Decimal(allocated) / Decimal(100)
            else { return nil }
        }
        self.agent = agent
        eventCount = events
        pricedEventCount = priced
        unpricedEventCount = unpriced
        apiReferenceCostUSD = api
        proReferenceCostUSD = pro
        proAllocatedCents = allocated
        unallocatedCents = unallocated
        managedCostUSD = managed
    }
}

struct ManagedCostShadow: Equatable, Sendable {
    let summary: ManagedCostShadowSummary
    let agents: [ManagedCostAgent]
    let metricQuality: MetricQuality

    var note: String {
        var parts = ["Shadow 试算，不替代现有账单"]
        if summary.qualityStatus == "partial" {
            parts.append("证据不完整")
        }
        if summary.unpricedEventCount > 0 {
            parts.append("有 \(summary.unpricedEventCount) 条未定价")
        }
        return parts.joined(separator: "；")
    }
}

extension SnapshotMetrics {
    var managedCostShadow: ManagedCostShadow? {
        guard
            let summaryMetric = rawMetric(MetricID.managedCostShadowSummary),
            let agentMetric = rawMetric(MetricID.managedCostShadowByAgent),
            summaryMetric.status == .available, agentMetric.status == .available,
            summaryMetric.capabilityID == "compute_cost.shadow",
            agentMetric.capabilityID == "compute_cost.shadow",
            summaryMetric.window == "month_cst", agentMetric.window == "month_cst",
            summaryMetric.unit == "list", agentMetric.unit == "list",
            summaryMetric.quality == agentMetric.quality,
            [.validated, .descriptive].contains(summaryMetric.quality),
            summaryMetric.sampleCount == agentMetric.sampleCount,
            let summaryRows = summaryMetric.value.rows, summaryRows.count == 1,
            let summary = ManagedCostShadowSummary(row: summaryRows[0]),
            summaryMetric.sampleCount == summary.eventCount,
            (summary.qualityStatus == "complete" && summaryMetric.quality == .validated)
                || (summary.qualityStatus == "partial" && summaryMetric.quality == .descriptive),
            let agentRows = agentMetric.value.rows,
            let agents = decodeManagedCostAgents(agentRows),
            Set(agents.map(\.agent)).count == agents.count,
            exactBillingSum(agents.map(\.eventCount)) == summary.eventCount,
            exactBillingSum(agents.map(\.pricedEventCount)) == summary.pricedEventCount,
            exactBillingSum(agents.map(\.unpricedEventCount)) == summary.unpricedEventCount,
            agents.reduce(Decimal.zero, { $0 + $1.apiReferenceCostUSD })
                == summary.apiReferenceCostUSD,
            agents.reduce(Decimal.zero, { $0 + $1.proReferenceCostUSD })
                == summary.proReferenceCostUSD,
            exactBillingSum(agents.map(\.proAllocatedCents))
                == summary.proAllocatedCents,
            exactBillingSum(agents.map(\.unallocatedCents))
                == summary.unallocatedCents,
            agents.reduce(Decimal.zero, { $0 + $1.managedCostUSD })
                == summary.managedCostUSD
        else { return nil }
        return ManagedCostShadow(
            summary: summary,
            agents: agents,
            metricQuality: summaryMetric.quality
        )
    }

    private func decodeManagedCostAgents(
        _ rows: [[String: MetricScalar]]
    ) -> [ManagedCostAgent]? {
        let decoded = rows.compactMap(ManagedCostAgent.init(row:))
        return decoded.count == rows.count ? decoded : nil
    }
}
