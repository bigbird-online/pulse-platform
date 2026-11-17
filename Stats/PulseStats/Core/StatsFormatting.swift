import Foundation

/// Display formatting. Every function takes an optional and returns the literal
/// string "未知" when the input is absent — there is no path here that turns a
/// missing number into a zero.
enum StatsFormat {
    static let unknown = "未知"

    /// Compact Chinese magnitude, e.g. 390952 → "39万". Used in the menu bar
    /// where width is the binding constraint (spec: two items ≤ 160pt).
    static func compactCount(_ value: Int?) -> String {
        guard let value else { return unknown }
        let magnitude = abs(value)
        let sign = value < 0 ? "-" : ""
        if magnitude >= 100_000_000 {
            return sign + trimmed(Double(magnitude) / 100_000_000) + "亿"
        }
        if magnitude >= 10_000 {
            return sign + trimmed(Double(magnitude) / 10_000) + "万"
        }
        return sign + String(magnitude)
    }

    /// One decimal place, but only when it says something ("3.5万" vs "39万").
    ///
    /// The cutoff is 10, not 100: at 39.0952万 the decimal is noise — nobody
    /// reads "39.1万" differently from "39万", and in the menu bar it costs
    /// width that §3 spends on the status phrase instead. Below 10 the decimal
    /// still carries a third of the magnitude ("3.5万" vs "3万"), so it stays.
    private static func trimmed(_ value: Double) -> String {
        if value >= 10 { return safeInt(value.rounded()).map(String.init) ?? unknown }
        let rounded = (value * 10).rounded() / 10
        if rounded == rounded.rounded() { return safeInt(rounded).map(String.init) ?? unknown }
        return String(format: "%.1f", rounded)
    }

    /// Thousands separators for panel figures, where full precision fits.
    static func grouped(_ value: Int?) -> String {
        guard let value else { return unknown }
        return value.formatted(.number.grouping(.automatic))
    }

    static func percent(_ value: Double?) -> String {
        guard let rounded = safeInt(value?.rounded()) else { return unknown }
        return "\(rounded)%"
    }

    /// Fixed-subscription money stays integer cents end to end. Formatting by
    /// quotient/remainder avoids a Double round trip at the last mile.
    /// 估算金额，两位小数。
    ///
    /// 与 `usdCents` 并存且刻意不同：账单是整数分、精确守恒，用 `usdCents`；
    /// 这个是按占比算出来的分摊估算，本来就带小数，取整会把 $8.71 变成 $9，
    /// 一个月差出好几美元。两个函数的存在本身就在提示两种数不是一回事。
    static func usd(_ value: Double?) -> String {
        guard let value, value.isFinite else { return unknown }
        return String(format: "$%.2f", value)
    }

    static func usdCents(_ value: Int?) -> String {
        guard let value, value >= 0 else { return unknown }
        return String(format: "$%d.%02d", value / 100, value % 100)
    }

    /// ComputeCost keeps exact Decimal strings through the consumer boundary;
    /// conversion to Double happens only for this two-decimal display.
    static func usdDecimal(_ value: Decimal?) -> String {
        guard let value, !value.isNaN else { return unknown }
        // A positive sub-cent charge must never look identical to confirmed zero.
        if value > 0 && value < Decimal(string: "0.01")! { return "<$0.01" }
        return String(format: "$%.2f", NSDecimalNumber(decimal: value).doubleValue)
    }

    /// Milliseconds → human duration. p50 turn length is minutes-scale.
    static func duration(milliseconds: Double?) -> String {
        guard let milliseconds, milliseconds >= 0,
              let seconds = safeInt((milliseconds / 1000).rounded())
        else { return unknown }
        if seconds < 60 { return "\(seconds)秒" }
        let minutes = seconds / 60
        let remainder = seconds % 60
        if minutes < 60 {
            return remainder == 0 ? "\(minutes)分" : "\(minutes)分\(remainder)秒"
        }
        return "\(minutes / 60)小时\(minutes % 60)分"
    }

    static func minutesOnly(milliseconds: Double?) -> String {
        guard let milliseconds, milliseconds >= 0,
              let minutes = safeInt((milliseconds / 60000).rounded())
        else { return unknown }
        return minutes <= 0 ? "<1分" : "\(minutes)分"
    }

    /// Share of a total, e.g. output as a fraction of all tokens.
    static func share(_ part: Int?, of total: Int?) -> String? {
        guard let part, let total, total > 0 else { return nil }
        let value = Double(part) / Double(total) * 100
        return String(format: "%.1f%%", value)
    }

    /// Wall-clock in UTC, matching the aggregation window (spec §3.2 says the
    /// day boundary is UTC and the panel must say so).
    static func clockUTC(_ date: Date?) -> String {
        guard let date else { return unknown }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .gmt
        let parts = calendar.dateComponents([.month, .day, .hour, .minute], from: date)
        guard let month = parts.month, let day = parts.day,
              let hour = parts.hour, let minute = parts.minute else { return unknown }
        return String(format: "%02d-%02d %02d:%02d", month, day, hour, minute)
    }

    /// Wall-clock in 北京时间, for anything 大鸟 reads directly.
    ///
    /// Pinned to `Asia/Shanghai` rather than the machine's local zone on
    /// purpose. The machines disagree — the Linux nodes run UTC, the laptop
    /// runs UTC+8 (root `CLAUDE.md`) — so "local time" would mean two different
    /// things depending on where the app happened to launch, and the panel is
    /// read by one person with one clock. Spec v3 §4 asks for 北京时间 by name;
    /// mixed UTC/CST wording is called out there as an existing defect.
    ///
    /// `.gmt` is a deliberate last resort: if the tz database somehow lacks
    /// the identifier, showing UTC and being 8 hours off is still better than
    /// crashing, and every such string is labelled 北京时间 by its caller.
    static let displayTimeZone = TimeZone(identifier: "Asia/Shanghai") ?? .gmt

    static func clockBeijing(_ date: Date?) -> String {
        guard let date else { return unknown }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = displayTimeZone
        let parts = calendar.dateComponents([.month, .day, .hour, .minute], from: date)
        guard let month = parts.month, let day = parts.day,
              let hour = parts.hour, let minute = parts.minute else { return unknown }
        return String(format: "%02d-%02d %02d:%02d", month, day, hour, minute)
    }

    /// "40 秒前" / "3 分钟前" — how stale the numbers are, in words.
    ///
    /// Only ever counts forward. A snapshot stamped in the future means the
    /// two clocks disagree, and "-2 分钟前" would be a puzzle rather than a
    /// reading, so that case says so plainly instead.
    static func age(_ date: Date?, now: Date = Date()) -> String {
        guard let date else { return unknown }
        let seconds = now.timeIntervalSince(date)
        if seconds < 0 { return "时间对不上" }
        if seconds < 60 { return "\(Int(seconds)) 秒前" }
        let minutes = Int(seconds / 60)
        if minutes < 60 { return "\(minutes) 分钟前" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours) 小时前" }
        return "\(hours / 24) 天前"
    }

    static func timeOnlyUTC(_ date: Date?) -> String {
        guard let date else { return unknown }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .gmt
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        guard let hour = parts.hour, let minute = parts.minute else { return unknown }
        return String(format: "%02d:%02d", hour, minute)
    }
}
