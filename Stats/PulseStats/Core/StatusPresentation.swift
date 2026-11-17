import SwiftUI

struct StatusPresentation: Equatable, Sendable {
    let runtimeHealth: RuntimeHealth
    let coverage: Coverage

    /// The data is old only because the overnight quiet window paused fetching.
    ///
    /// Set by `SnapshotStore` and only ever when the last fetch *succeeded* —
    /// see `RefreshPolicy.staleIsExplainedByQuietWindow`. A failed or unreachable
    /// transport never sets it, so this can soften the wording without giving
    /// a real outage anywhere to hide. It is a presentation fact, not a health
    /// one, which is why it rides here rather than becoming a `RuntimeHealth`
    /// case: the runtime is not in a sixth state, we merely know why the clock
    /// looks the way it does.
    let pausedOvernight: Bool

    init(
        runtimeHealth: RuntimeHealth,
        coverage: Coverage,
        pausedOvernight: Bool = false
    ) {
        self.runtimeHealth = runtimeHealth
        self.coverage = coverage
        // Only staleness can be excused by the night. A snapshot that was
        // fetched fine but reports `critical` is still critical at 03:00.
        self.pausedOvernight = pausedOvernight && runtimeHealth == .stale
    }

    var symbolName: String {
        switch runtimeHealth {
        case .ok: "checkmark.circle.fill"
        case .warning: "exclamationmark.triangle.fill"
        case .critical: "xmark.octagon.fill"
        // Paused overnight is a sleeping clock, not an alarming one.
        case .stale: pausedOvernight ? "moon.zzz.fill" : "clock.badge.exclamationmark.fill"
        case .unknown: "questionmark.circle.fill"
        }
    }

    enum Tone: Equatable, Sendable {
        case green
        case amber
        case red
        case gray
    }

    var tone: Tone {
        switch runtimeHealth {
        case .critical: .red
        // Amber means "look at this". A pause the owner himself asked for has
        // nothing to look at, so it drops to gray — visible, not alarming.
        case .stale: pausedOvernight ? .gray : .amber
        case .warning: .amber
        case .unknown: .gray
        case .ok:
            switch coverage {
            case .full: .green
            case .partial: .amber
            case .unknown: .gray
            }
        }
    }

    var color: Color {
        switch tone {
        case .green: .green
        case .amber: .orange
        case .red: .red
        case .gray: .gray
        }
    }

    var coverageSymbolName: String {
        switch coverage {
        case .full: "circle.fill"
        case .partial: "circle.lefthalf.filled"
        case .unknown: "circle.dotted"
        }
    }

    var accessibilityLabel: String {
        "PULSE runtime \(runtimeHealth.rawValue), coverage \(coverage.rawValue)"
    }
}

/// The one user-facing status shared by the menu bar and panel header.
///
/// It answers whether anything wants the owner now: the action count and the
/// runtime axis. An action count is never hidden; it stays red and appends a
/// non-ok runtime reason. Monitoring coverage is the monitor's own build-out,
/// not an action, so it stays in the panel's data-and-coverage section and in
/// `StatusPresentation.accessibilityLabel` instead of this sentence.
///
/// The panel header shows the same lamp with `headerText`, which carries only
/// the runtime axis: the action count already sits in the menu bar WI badge
/// and the 「要你出手」 card. A red lamp with no runtime issue has no words.
struct SnapshotStatusSummary: Equatable, Sendable {
    let lamp: StatusLamp
    let text: String
    let headerText: String?

    private struct Issue {
        let lamp: StatusLamp
        let text: String
        let actionSuffix: String
    }

    init(
        hasSnapshot: Bool,
        actCount: Int,
        presentation: StatusPresentation
    ) {
        guard hasSnapshot else {
            lamp = .gray
            text = "没数据"
            headerText = text
            return
        }

        let runtimeIssue: Issue?
        switch presentation.runtimeHealth {
        case .stale:
            // 夜间静默期(20:00-08:00)不取数是大鸟点名要的省电,不是故障。
            // 早上八点那一眼看到的必然是昨晚 19:xx 的快照——旧了十几个小时,
            // 却一个字都不该写成告警。天天误报的黄灯会被学会无视,真出事那次
            // 就跟着一起被无视了,所以这里换成灰灯 + 说清楚"暂停"而非"过期"。
            //
            // 只有「上次取数成功、纯粹因为没取而旧」才走到这一档:取数失败
            // (transportState 为 .failed / .cached+transportFailure)时
            // SnapshotStore 不会置 pausedOvernight,黄灯照常。判据在
            // RefreshPolicy.staleIsExplainedByQuietWindow,可单测。
            runtimeIssue = presentation.pausedOvernight
                ? Issue(lamp: .gray, text: "夜间暂停刷新", actionSuffix: "夜间暂停")
                : Issue(lamp: .amber, text: "数据已过期", actionSuffix: "数据过期")
        case .unknown:
            runtimeIssue = Issue(lamp: .gray, text: "数据未知", actionSuffix: "数据未知")
        case .critical:
            runtimeIssue = Issue(lamp: .red, text: "运行严重异常", actionSuffix: "严重异常")
        case .warning:
            runtimeIssue = Issue(lamp: .amber, text: "运行有异常", actionSuffix: "运行异常")
        case .ok:
            runtimeIssue = nil
        }

        if actCount > 0 {
            lamp = .red
            text = "\(actCount) 件要出手" + (runtimeIssue.map { " · " + $0.actionSuffix } ?? "")
            headerText = runtimeIssue?.text
        } else if let runtimeIssue {
            lamp = runtimeIssue.lamp
            text = runtimeIssue.text
            headerText = text
        } else {
            lamp = .green
            text = "正常"
            headerText = text
        }
    }
}
