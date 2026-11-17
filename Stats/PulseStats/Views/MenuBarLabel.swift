import AppKit
import SwiftUI

extension StatusLamp {
    /// Quota tint for the menu bar: spec §3 asks for ">90% 黄，>95% 红" and
    /// nothing else raising its voice. A healthy figure stays `.primary` — the
    /// same weight as every other system item — so the eye skips it, which is
    /// the state that holds most of the time.
    var menuBarTint: Color {
        switch self {
        case .green: .primary
        case .amber: .orange
        case .red: .red
        case .gray: .secondary
        }
    }

    var color: Color {
        switch self {
        case .green: .green
        case .amber: .orange
        case .red: .red
        case .gray: .secondary
        }
    }

    /// The lamp as a small tinted dot for the menu bar title.
    ///
    /// Three attempts, recorded because each failed differently in the status
    /// item title — a context that silently drops what it cannot draw:
    ///
    /// 1. `HStack { Circle() … }` — shaped views are dropped entirely; the bar
    ///    showed a bare glyph and no figures at all.
    /// 2. Emoji circles (🟢🟡🔴) — these render, but at full text size, so the
    ///    lamp shouted louder than the number it annotates.
    /// 3. `Text(Image(systemName:))` — the symbol vanished. The attachment does
    ///    not survive into the title, so the lamp disappeared completely.
    ///
    /// 4. A tinted Unicode dot — the dot rendered, but the tint did not. The
    ///    status item title draws in the system colour so the menu bar stays
    ///    legible in light and dark; per-run colour does not survive.
    ///
    /// So the lamp cannot rely on colour at all. It carries its state in the
    /// glyph instead: hollow when there is nothing to do, solid when something
    /// wants attention, ringed when it is bad, dashed when unknown. Colour is
    /// still applied — where it survives it reinforces the shape; where it does
    /// not, the shape alone is already unambiguous.
    var symbol: Text {
        Text(glyph)
            .font(.system(size: MenuBarMetrics.lampFontSize))
            .foregroundStyle(color)
    }

    /// The lamp plus its figure, with no stray space when the lamp is silent.
    func labelled(_ figure: String) -> Text {
        glyph.isEmpty ? Text(figure) : symbol + Text(" \(figure)")
    }

    /// Silence when things are fine; a mark only when they are not.
    ///
    /// Shape had to carry the whole signal once colour turned out not to
    /// survive into the status item title, and a ring-vs-disc distinction is
    /// too weak at 10pt to read at a glance — every lamp looked like the same
    /// pale circle (owner, 2026-07-25: "灯都是白色").
    ///
    /// So the green state prints nothing at all. A bare figure *is* the
    /// "nothing to do" signal, and the eye is drawn to the row that has gained
    /// a mark rather than having to compare four similar dots. This also keeps
    /// the quiet case narrowest, which is the case that is true most of the
    /// time.
    var glyph: String {
        switch self {
        case .green: ""    // nothing to say
        case .amber: "•"   // worth a look
        case .red: "!"     // acting up
        case .gray: "?"    // unknown
        }
    }
}

/// The coloured dot that precedes every menu bar figure.
struct LampDot: View {
    let lamp: StatusLamp
    var size: CGFloat = 6

    var body: some View {
        Circle()
            .fill(lamp.color)
            .frame(width: size, height: size)
    }
}


/// One native image keeps both lines inside the actual menu item; unlike a
/// SwiftUI stack, neither line can be lost during status-item label extraction.
struct PulseMenuBarLabel: View {
    let model: MenuBarViewModel
    static func title(for model: MenuBarViewModel) -> Text { Text(model.menuAccessibilityLabel) }
    var body: some View {
        Image(nsImage: MenuBarArtwork.image(for: model))
            .renderingMode(.template)
            .help(model.menuAccessibilityLabel)
            .accessibilityLabel(model.menuAccessibilityLabel)
    }
}

enum MenuBarMetrics {
    static let fontSize: CGFloat = 12
    static let lampFontSize: CGFloat = 12
    static let itemPadding: CGFloat = 10
}

/// Label over a larger percentage, as in the user's Stats reference. The
/// monochrome template follows the system appearance, including selection.
enum MenuBarArtwork {
    static var labelFont: NSFont { NSFont.systemFont(ofSize: 7, weight: .regular) }
    static var valueFont: NSFont { NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .semibold) }
    static let height: CGFloat = 22
    static let badgeGap: CGFloat = 2
    static func quotaWidth(_ value: String) -> CGFloat {
        max(38, ceil((value as NSString).size(withAttributes: [.font: valueFont]).width) + 6)
    }
    static func image(for model: MenuBarViewModel) -> NSImage {
        let quotaValue = model.menuQuotaText
        let quotaBadgeWidth = quotaWidth(quotaValue)
        let ccValue = model.menuCCText
        let ccWidth = quotaWidth(ccValue)
        let grokValue = model.menuGrokText
        let grokWidth = quotaWidth(grokValue)
        let wiValue = model.menuWIText
        // Deliberately reuse quotaWidth so CODEX, CC, GROK and WI have the same
        // visual footprint instead of making the counter look like a status suffix.
        let wiWidth = quotaWidth(wiValue)
        let badge = model.menuBadge
        let badgeWidth: CGFloat = badge.isEmpty ? 0 : max(14, ceil((badge as NSString).size(withAttributes: [.font: valueFont]).width)+6)
        let ccX = quotaBadgeWidth + badgeGap
        let grokX = ccX + ccWidth + badgeGap
        let wiX = grokX + grokWidth + badgeGap
        let badgeX = wiX + wiWidth
        let image = NSImage(size: NSSize(width: badgeX+badgeWidth, height: height), flipped: false) { _ in
            let center = NSMutableParagraphStyle(); center.alignment = .center
            let label: [NSAttributedString.Key: Any] = [.font: labelFont, .foregroundColor: NSColor.black, .paragraphStyle: center]
            let figure: [NSAttributedString.Key: Any] = [.font: valueFont, .foregroundColor: NSColor.black, .paragraphStyle: center]
            ("CODEX" as NSString).draw(in: NSRect(x: 0, y: 14, width: quotaBadgeWidth, height: 8), withAttributes: label)
            (quotaValue as NSString).draw(in: NSRect(x: 0, y: 0, width: quotaBadgeWidth, height: 16), withAttributes: figure)
            ("CC" as NSString).draw(in: NSRect(x: ccX, y: 14, width: ccWidth, height: 8), withAttributes: label)
            (ccValue as NSString).draw(in: NSRect(x: ccX, y: 0, width: ccWidth, height: 16), withAttributes: figure)
            ("GROK" as NSString).draw(in: NSRect(x: grokX, y: 14, width: grokWidth, height: 8), withAttributes: label)
            (grokValue as NSString).draw(in: NSRect(x: grokX, y: 0, width: grokWidth, height: 16), withAttributes: figure)
            ("WI" as NSString).draw(in: NSRect(x: wiX, y: 14, width: wiWidth, height: 8), withAttributes: label)
            (wiValue as NSString).draw(in: NSRect(x: wiX, y: 0, width: wiWidth, height: 16), withAttributes: figure)
            if !badge.isEmpty {
                (badge as NSString).draw(in: NSRect(x: badgeX, y: 3, width: badgeWidth, height: 16), withAttributes: figure)
            }
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = model.menuAccessibilityLabel
        return image
    }
}

enum MenuBarWidth {
    /// Four equal slots (CODEX, CC, GROK, WI) at three-digit figures plus the marker.
    static let budget: CGFloat = 210
    static func merged(_ model: MenuBarViewModel) -> CGFloat {
        MenuBarArtwork.image(for: model).size.width + MenuBarMetrics.itemPadding
    }
}
