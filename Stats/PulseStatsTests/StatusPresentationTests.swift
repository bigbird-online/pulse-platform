import XCTest
@testable import PulseStats

final class StatusPresentationTests: XCTestCase {
    func testEveryRuntimeStateHasStablePresentation() {
        let expected: [(RuntimeHealth, String, StatusPresentation.Tone)] = [
            (.ok, "checkmark.circle.fill", .green),
            (.warning, "exclamationmark.triangle.fill", .amber),
            (.critical, "xmark.octagon.fill", .red),
            (.stale, "clock.badge.exclamationmark.fill", .amber),
            (.unknown, "questionmark.circle.fill", .gray),
        ]
        for (health, symbol, tone) in expected {
            let presentation = StatusPresentation(runtimeHealth: health, coverage: .full)
            XCTAssertEqual(presentation.symbolName, symbol)
            XCTAssertEqual(presentation.tone, tone)
        }
    }

    func testCoveragePreventsFalseGreen() {
        XCTAssertEqual(StatusPresentation(runtimeHealth: .ok, coverage: .full).tone, .green)
        XCTAssertEqual(StatusPresentation(runtimeHealth: .ok, coverage: .partial).tone, .amber)
        XCTAssertEqual(StatusPresentation(runtimeHealth: .ok, coverage: .unknown).tone, .gray)
    }

    func testRuntimeAndCoverageAreBothInAccessibilityLabel() {
        let label = StatusPresentation(runtimeHealth: .ok, coverage: .partial).accessibilityLabel
        XCTAssertTrue(label.contains("runtime ok"))
        XCTAssertTrue(label.contains("coverage partial"))
    }

    func testSnapshotStatusSummaryNamesEveryUntrustedRuntimeState() {
        let cases: [(RuntimeHealth, StatusLamp, String)] = [
            (.stale, .amber, "数据已过期"),
            (.unknown, .gray, "数据未知"),
            (.critical, .red, "运行严重异常"),
            (.warning, .amber, "运行有异常"),
            (.ok, .green, "正常"),
        ]
        for (runtimeHealth, lamp, text) in cases {
            let summary = SnapshotStatusSummary(
                hasSnapshot: true, actCount: 0,
                presentation: StatusPresentation(runtimeHealth: runtimeHealth, coverage: .full)
            )
            XCTAssertEqual(summary.lamp, lamp)
            XCTAssertEqual(summary.text, text)
        }
    }

    func testActionCountStaysVisibleWithEveryRuntimeState() {
        let cases: [(RuntimeHealth, String, String?)] = [
            (.stale, "3 件要出手 · 数据过期", "数据已过期"),
            (.unknown, "3 件要出手 · 数据未知", "数据未知"),
            (.critical, "3 件要出手 · 严重异常", "运行严重异常"),
            (.warning, "3 件要出手 · 运行异常", "运行有异常"),
            (.ok, "3 件要出手", nil),
        ]
        for (runtimeHealth, text, header) in cases {
            let summary = SnapshotStatusSummary(
                hasSnapshot: true, actCount: 3,
                presentation: StatusPresentation(runtimeHealth: runtimeHealth, coverage: .full)
            )
            XCTAssertEqual(summary.lamp, .red)
            XCTAssertEqual(summary.text, text)
            XCTAssertEqual(summary.headerText, header, "runtime=\(runtimeHealth)")
        }
    }

    /// The panel header keeps the lamp and runtime words but never names the
    /// action count; that figure lives in the menu bar WI badge and its card.
    func testPanelHeaderOmitsActionCount() {
        for health in [RuntimeHealth.ok, .warning, .critical, .stale, .unknown] {
            for actCount in [0, 1, 4, 128] {
                let summary = SnapshotStatusSummary(
                    hasSnapshot: true, actCount: actCount,
                    presentation: StatusPresentation(runtimeHealth: health, coverage: .full))
                let header = summary.headerText ?? ""
                XCTAssertFalse(header.contains("要出手"), header)
                XCTAssertFalse(header.contains("·"), header)
                XCTAssertFalse(header.contains(String(actCount)) && actCount > 0, header)
            }
        }
        let empty = SnapshotStatusSummary(hasSnapshot: false, actCount: 0,
            presentation: StatusPresentation(runtimeHealth: .unknown, coverage: .unknown))
        XCTAssertEqual(empty.headerText, "没数据")
    }

    /// Coverage is the monitor's own build-out, not an action: neither the
    /// menu bar nor the panel header carries it.
    func testCoverageNeverEntersTheStatusSentence() {
        let cases: [(RuntimeHealth, Coverage, Int, StatusLamp, String)] = [
            (.ok, .unknown, 0, .green, "正常"),
            (.ok, .partial, 0, .green, "正常"),
            (.warning, .unknown, 0, .amber, "运行有异常"),
            (.critical, .partial, 0, .red, "运行严重异常"),
            (.stale, .unknown, 0, .amber, "数据已过期"),
            (.ok, .unknown, 3, .red, "3 件要出手"),
            (.warning, .unknown, 1, .red, "1 件要出手 · 运行异常"),
        ]
        for (runtimeHealth, coverage, actCount, lamp, text) in cases {
            let summary = SnapshotStatusSummary(
                hasSnapshot: true, actCount: actCount,
                presentation: StatusPresentation(runtimeHealth: runtimeHealth, coverage: coverage)
            )
            XCTAssertEqual(summary.lamp, lamp, "runtime=\(runtimeHealth) coverage=\(coverage)")
            XCTAssertEqual(summary.text, text, "runtime=\(runtimeHealth) coverage=\(coverage)")
        }
    }
}
