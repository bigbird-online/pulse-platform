import Foundation

/// When the menu bar is allowed to go and fetch a new snapshot.
///
/// Until 2026-07-28 there was no policy and no timer at all: `PulseStatsApp`
/// fetched once, guarded by `if store.snapshot == nil`, and never again. Since
/// a snapshot declares itself expired three minutes after it is generated, the
/// bar spent essentially its whole life showing data it already knew was
/// expired — the owner's report was that every figure was wrong, and the Codex
/// quota on screen turned out to be eight days old.
///
/// The rule the owner asked for: refresh every half hour, and stay quiet for
/// twelve hours overnight (Beijing time). This type is the whole rule, kept
/// separate from the App so it can be tested without a running scene — the
/// original omission was invisible precisely because nothing could assert on it.
enum RefreshPolicy {
    /// Half an hour, per the owner's instruction.
    ///
    /// Each tick costs one local exporter run
    /// (~6-8s). The snapshot's own three-minute TTL is deliberately *not* used
    /// as the interval: that TTL says "past here, call it stale", not "fetch
    /// this often", and honouring it literally would mean 480 exporter runs a
    /// day on a single-core machine.
    static let interval: TimeInterval = 30 * 60

    /// The overnight quiet window, in Beijing hours: 20:00 up to 08:00.
    ///
    /// Twelve hours, as asked. The owner said "夜间", which brackets the
    /// evening and the small hours rather than the morning — reading it as
    /// 00:00-12:00 would silence the bar for the whole working morning, which
    /// is the opposite of the intent. Changing the reading is a two-number edit
    /// here and nowhere else.
    static let quietStartHour = 20
    static let quietEndHour = 8

    /// Beijing time. Fixed rather than the machine's locale: the owner reads
    /// this bar in UTC+8, and the box itself runs on UTC.
    static let timeZone = TimeZone(secondsFromGMT: 8 * 3600)!

    /// Whether `date` falls inside the overnight quiet window.
    ///
    /// The window wraps midnight, so the test is a union of two ranges rather
    /// than a single `start..<end` — the bug that comparison would introduce is
    /// silent (it would simply never be quiet).
    static func isQuiet(at date: Date, calendar: Calendar = beijingCalendar) -> Bool {
        let hour = calendar.component(.hour, from: date)
        return hour >= quietStartHour || hour < quietEndHour
    }

    /// Whether enough time has passed, and we are outside the quiet window.
    ///
    /// `lastRefresh == nil` means nothing has ever loaded, which is due
    /// regardless of the hour: a bar showing "没数据" all night is worse than
    /// one exporter run, and this is also the cold-start path when the App is
    /// launched during the quiet window.
    static func shouldRefresh(
        now: Date,
        lastRefresh: Date?,
        resourceExpiresAt: Date? = nil,
        calendar: Calendar = beijingCalendar
    ) -> Bool {
        guard let lastRefresh else { return true }
        if isQuiet(at: now, calendar: calendar) { return false }
        // A last-refresh stamp in the future means the clock moved backwards;
        // treat it as due rather than waiting out the bogus interval.
        let elapsed = now.timeIntervalSince(lastRefresh)
        if elapsed < 0 || elapsed >= interval { return true }
        // Oikos and account quota can expire before the half-hour fetch. Use the
        // existing timer, at most once per five minutes after failed reads,
        // without extending evidence TTL or running any new collector.
        if let resourceExpiresAt, elapsed >= 300 {
            return now >= resourceExpiresAt.addingTimeInterval(-60)
        }
        return false
    }

    /// Whether stale data at `now` is explained by the quiet window alone.
    ///
    /// The morning-after problem: the bar stops fetching at 20:00, so the 08:00
    /// glance is looking at a 19:xx snapshot that has been "expired" for twelve
    /// hours. Nothing is broken — that staleness *is* the overnight saving the
    /// owner asked for — but the bar rendered it as an amber 数据已过期, which is
    /// the same signal it uses for a box that stopped answering. An alert that
    /// fires every single morning by design is one the owner learns to ignore,
    /// which is how a real outage gets missed.
    ///
    /// This says only "the clock explains it". It deliberately knows nothing
    /// about transport failures: the caller must still establish that the last
    /// fetch actually succeeded, because a quiet window is an excuse for *not
    /// fetching*, never for a fetch that failed. Keeping those two facts in
    /// separate places is what stops the window from swallowing real errors.
    ///
    /// `lastRefresh == nil` returns false: never having loaded at all is not
    /// something the night explains, and `shouldRefresh` fetches on that path
    /// regardless of the hour.
    static func staleIsExplainedByQuietWindow(
        now: Date,
        lastRefresh: Date?,
        calendar: Calendar = beijingCalendar
    ) -> Bool {
        guard let lastRefresh else { return false }
        guard isQuiet(at: now, calendar: calendar) else { return false }
        // A refresh stamp in the future means the clock jumped; treat the
        // staleness as unexplained rather than excusing it with a bogus span.
        guard now >= lastRefresh else { return false }
        // Only the backlog the window itself could have caused. Data older than
        // the window is long (12h) plus one interval cannot be blamed on
        // tonight's pause -- something was already wrong before 20:00, and that
        // must keep its amber.
        let quietWindowHours = Double((24 - quietStartHour) + quietEndHour)
        let explainableAge = quietWindowHours * 3600 + interval
        return now.timeIntervalSince(lastRefresh) <= explainableAge
    }

    static var beijingCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        return calendar
    }
}
