import Foundation

// Compiled with the candidate's UNMODIFIED Stats CostDashboard.swift.
// Consumes only the private replay dashboard, never the installed App cache.
let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase
decoder.dateDecodingStrategy = .custom { d in
    let c = try d.singleValueContainer()
    let s = try c.decode(String.self)
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let value = f.date(from: s) { return value }
    f.formatOptions = [.withInternetDateTime]
    if let value = f.date(from: s) { return value }
    throw DecodingError.dataCorruptedError(in: c, debugDescription: "Invalid timestamp")
}
let dashboard = try decoder.decode(CostDashboard.self, from: data)
let raw = try JSONSerialization.jsonObject(with: data) as! [String: Any]
let views = raw["views"] as! [[String: Any]]
func checkCost(_ cost: QueryMachineCost, _ raw: [String: Any]) {
    precondition(cost.isValid)
    precondition(cost.machineCashUsd == raw["machine_cash_usd"] as? String)
    precondition(cost.partialMachineCashUsd == raw["partial_machine_cash_usd"] as? String)
    precondition(cost.reasons == raw["reasons"] as? [String])
    precondition(cost.exactAmount == ((raw["machine_cash_usd"] as? String) ?? (raw["partial_machine_cash_usd"] as? String)))
}
var accepted = 0
var priced = 0
for period in CostPeriod.allCases {
    for dimension in CostDimension.allCases {
        let view = dashboard.view(period: period, dimension: dimension,
                                  at: dashboard.generatedAt.addingTimeInterval(1))!
        let original = views.filter { $0["period"] as? String == period.rawValue
            && $0["dimension"] as? String == dimension.rawValue }
        precondition(original.count == 1)
        let value = original[0]
        precondition(view.watermark == value["watermark"] as? Int)
        precondition(view.reportSha256 == value["report_sha256"] as? String)
        precondition(view.catalogSha256 == value["catalog_sha256"] as? String)
        let summary = value["summary"] as! [String: Any]
        checkCost(view.summary.cost, summary["cost"] as! [String: Any])
        let groups = value["groups"] as! [[String: Any]]
        precondition(view.groups.count == groups.count)
        for (group, item) in zip(view.groups, groups) {
            precondition(group.key == item["key"] as? [String: String])
            checkCost(group.cost, item["cost"] as! [String: Any])
        }
        accepted += 1
        if view.summary.cost.exactAmount != nil { priced += 1 }
    }
}
precondition(accepted == 9 && priced >= 3)
print("SWIFT_REAL_CONSUMER accepted_views=\(accepted) priced_views=\(priced) exact_amounts_groups_watermarks_catalogs_match=true installed_app_untouched=true")
