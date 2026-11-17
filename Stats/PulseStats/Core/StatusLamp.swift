import Foundation

/// The four lamp colours used by both menu bar items.
///
/// `.gray` is load-bearing: it means "we do not know", and per Slice 1 we must
/// never dress an unknown up as a green. Anything that cannot be computed from
/// present data lands here.
enum StatusLamp: String, Equatable, Sendable, CaseIterable {
    case green
    case amber
    case red
    case gray

    var accessibilityWord: String {
        switch self {
        case .green: "正常"
        case .amber: "注意"
        case .red: "异常"
        case .gray: "未知"
        }
    }
}

/// Panel layout limits. The「在办」stuck thresholds live in the exporter
/// (`work_board.py`), so the App never recomputes a second rule.
enum PanelThresholds {
    /// 本月摊销表画几行。全队十几个 agent 全列会把面板顶穿，画前几名 +
    /// 一行「另有 N 个，合计 $X」——被截掉的钱仍然出现，不静默丢。
    static let monthlyCostRows = 6
}

/// Menu bar lamp judgements. Every rule here is transcribed from spec §2.1 —
/// none of it is invented, and each has a matching test.
enum LampRules {
    /// CC lamp: green = processes up and no recent 429; red = a weekly-limit 429
    /// within 24h; gray = process count unknown or zero.
    static func cc(processes: Int?, rateLimited: Bool?) -> StatusLamp {
        if rateLimited == true { return .red }
        guard let processes else { return .gray }
        if processes <= 0 { return .gray }
        // Processes are up but we cannot say whether a 429 happened: not green.
        guard rateLimited != nil else { return .gray }
        return .green
    }

    /// Shared CC/Codex quota lamp from v3 §3: >90% amber, >95% red.
    /// The boundaries are deliberately exclusive; exactly 90 is green and
    /// exactly 95 is amber. Missing quota reads gray, never 0%.
    static func quota(usedPercent: Double?) -> StatusLamp {
        guard let usedPercent else { return .gray }
        if usedPercent > 95 { return .red }
        if usedPercent > 90 { return .amber }
        return .green
    }

    /// wi batch lamp (week and day share the rule): a stalled item over 7 days
    /// is amber, over 14 days is red.
    static func workItemBatch(created: Int?, maxStalledDays: Int?) -> StatusLamp {
        guard created != nil else { return .gray }
        guard let maxStalledDays else { return .green }
        if maxStalledDays > 14 { return .red }
        if maxStalledDays > 7 { return .amber }
        return .green
    }

    /// Completion lamp. The 0/0 case is the whole point: with nothing created
    /// today there is no denominator, so the honest answer is gray — not red,
    /// and not a green 100%.
    static func completion(created: Int?, completed: Int?) -> StatusLamp {
        guard let created, let completed else { return .gray }
        guard created > 0 else { return .gray }
        let rate = Double(completed) / Double(created)
        if rate >= 0.95 { return .green }
        if rate < 0.80 { return .red }
        return .amber
    }
}
