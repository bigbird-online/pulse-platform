import Foundation
import XCTest
@testable import PulseStats

/// Lamp judgements, transcribed from spec §2.1. The recurring theme is that
/// "we don't know" must show gray — never a green, never a zero.
final class LampRulesTests: XCTestCase {
    // MARK: CC

    func testCCIsGreenWhenProcessesUpAndNoRecentRateLimit() {
        XCTAssertEqual(LampRules.cc(processes: 3, rateLimited: false), .green)
    }

    func testCCIsRedWhenRateLimitedInLast24h() {
        XCTAssertEqual(LampRules.cc(processes: 3, rateLimited: true), .red)
        // Red wins even if the process count is unknown — a 429 happened.
        XCTAssertEqual(LampRules.cc(processes: nil, rateLimited: true), .red)
    }

    func testCCIsGrayWhenProcessesUnavailableOrZero() {
        XCTAssertEqual(LampRules.cc(processes: nil, rateLimited: false), .gray)
        XCTAssertEqual(LampRules.cc(processes: 0, rateLimited: false), .gray)
    }

    func testCCIsGrayWhenRateLimitStateIsUnknown() {
        // Processes are up, but with no 429 evidence we cannot claim green.
        XCTAssertEqual(LampRules.cc(processes: 2, rateLimited: nil), .gray)
    }

    // MARK: quota

    func testQuotaThresholds() {
        XCTAssertEqual(LampRules.quota(usedPercent: nil), .gray)
        XCTAssertEqual(LampRules.quota(usedPercent: 89), .green)
        XCTAssertEqual(LampRules.quota(usedPercent: 90), .green)
        XCTAssertEqual(LampRules.quota(usedPercent: 95), .amber)
        XCTAssertEqual(LampRules.quota(usedPercent: 96), .red)
        XCTAssertEqual(LampRules.quota(usedPercent: 100), .red)
        XCTAssertEqual(LampRules.quota(usedPercent: 140), .red)
    }

    /// Spec §7: a missing Codex session file must show unknown, not 0%.
    func testCodexQuotaMissingIsGrayNotGreen() {
        XCTAssertEqual(LampRules.quota(usedPercent: nil), .gray)
        XCTAssertEqual(StatsFormat.percent(nil), "未知")
    }

    // MARK: work items

    func testWorkItemBatchAmberOverSevenDaysAndRedOverFourteen() {
        XCTAssertEqual(LampRules.workItemBatch(created: 60, maxStalledDays: 3), .green)
        XCTAssertEqual(LampRules.workItemBatch(created: 60, maxStalledDays: 7), .green)
        XCTAssertEqual(LampRules.workItemBatch(created: 60, maxStalledDays: 8), .amber)
        XCTAssertEqual(LampRules.workItemBatch(created: 60, maxStalledDays: 14), .amber)
        XCTAssertEqual(LampRules.workItemBatch(created: 60, maxStalledDays: 15), .red)
    }

    func testWorkItemBatchIsGrayWhenCountUnavailable() {
        XCTAssertEqual(LampRules.workItemBatch(created: nil, maxStalledDays: 3), .gray)
    }

    // MARK: completion

    /// The 0/0 case. Nothing was created today, so there is no completion rate
    /// to judge — gray, and specifically not red.
    func testZeroCreatedTodayMakesCompletionLampGray() {
        XCTAssertEqual(LampRules.completion(created: 0, completed: 0), .gray)
        XCTAssertNotEqual(LampRules.completion(created: 0, completed: 0), .red)
    }

    func testCompletionThresholds() {
        XCTAssertEqual(LampRules.completion(created: 10, completed: 10), .green)
        XCTAssertEqual(LampRules.completion(created: 20, completed: 19), .green) // 95%
        XCTAssertEqual(LampRules.completion(created: 10, completed: 9), .amber)  // 90%
        XCTAssertEqual(LampRules.completion(created: 10, completed: 8), .amber)  // 80%
        XCTAssertEqual(LampRules.completion(created: 10, completed: 7), .red)    // 70%
    }

    func testCompletionIsGrayWhenEitherSideIsUnavailable() {
        XCTAssertEqual(LampRules.completion(created: nil, completed: 3), .gray)
        XCTAssertEqual(LampRules.completion(created: 3, completed: nil), .gray)
    }

    // MARK: formatting never invents a number

    func testFormattersRenderUnknownRatherThanZero() {
        XCTAssertEqual(StatsFormat.compactCount(nil), "未知")
        XCTAssertEqual(StatsFormat.grouped(nil), "未知")
        XCTAssertEqual(StatsFormat.percent(nil), "未知")
        XCTAssertEqual(StatsFormat.duration(milliseconds: nil), "未知")
        XCTAssertEqual(StatsFormat.clockUTC(nil), "未知")
        XCTAssertNil(StatsFormat.share(nil, of: 100))
        XCTAssertNil(StatsFormat.share(10, of: 0), "no denominator means no share")
    }

    func testCompactCountMatchesTheMockupWording() {
        XCTAssertEqual(StatsFormat.compactCount(390_952), "39万")
        XCTAssertEqual(StatsFormat.compactCount(477_197), "48万")
        XCTAssertEqual(StatsFormat.compactCount(35_000), "3.5万")
        XCTAssertEqual(StatsFormat.compactCount(60), "60")
        XCTAssertEqual(StatsFormat.compactCount(0), "0", "a measured zero is still a zero")
    }

    func testDurationIsHumanReadable() {
        XCTAssertEqual(StatsFormat.duration(milliseconds: 102_396), "1分42秒")
        XCTAssertEqual(StatsFormat.duration(milliseconds: 45_000), "45秒")
    }
}
