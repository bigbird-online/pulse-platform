import AppKit
import SwiftUI

/// Section container matching the mockup's grouped-card rhythm.
struct PanelSection<Content: View>: View {
    let title: String
    var caption: String?
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(title)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                if let caption {
                    Text(caption)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A "dot · label · value · note" line, the panel's workhorse row.
struct StatRow: View {
    let lamp: StatusLamp?
    let label: String
    let value: String
    var note: String?

    var body: some View {
        HStack(spacing: 8) {
            if let lamp {
                LampDot(lamp: lamp, size: 7)
            }
            Text(label)
                .font(.callout)
            Spacer(minLength: 8)
            Text(value)
                .font(.callout)
                .monospacedDigit()
            if let note {
                Text(note)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .frame(minWidth: 62, alignment: .trailing)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// The "要你操心的" block. Plain words only — see `Concern`.
struct ConcernList: View {
    let concerns: [Concern]
    let hasSnapshot: Bool

    var body: some View {
        if !hasSnapshot {
            Text("还没拿到数据，说不好有没有事")
                .font(.callout)
                .foregroundStyle(.secondary)
        } else if concerns.isEmpty {
            HStack(spacing: 8) {
                LampDot(lamp: .green, size: 7)
                Text("没事")
                    .font(.callout)
            }
        } else {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(concerns) { concern in
                    HStack(alignment: .top, spacing: 8) {
                        LampDot(lamp: concern.lamp, size: 7)
                            .padding(.top, 5)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(concern.title)
                                .font(.callout)
                            if let detail = concern.detail {
                                Text(detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
    }
}

/// Two side-by-side figures with a caption each (input vs output).
struct DualFigure: View {
    let leftTitle: String
    let leftValue: String
    let leftCaption: String
    let rightTitle: String
    let rightValue: String
    let rightCaption: String

    var body: some View {
        HStack(spacing: 8) {
            figure(leftTitle, leftValue, leftCaption, tint: .primary)
            figure(rightTitle, rightValue, rightCaption, tint: .teal)
        }
    }

    private func figure(_ title: String, _ value: String, _ caption: String, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(title)
                .font(.caption2)
                .foregroundStyle(.tertiary)
            Text(value)
                .font(.title3.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(tint)
            Text(caption)
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
    }
}

/// The four small tiles under the input/output pair.
struct QuadStats: View {
    struct Item: Identifiable, Equatable {
        let id: String
        let value: String
        let caption: String
        var isBad: Bool = false
    }

    let items: [Item]

    var body: some View {
        HStack(spacing: 6) {
            ForEach(items) { item in
                VStack(spacing: 1) {
                    Text(item.value)
                        .font(.body.weight(.semibold))
                        .monospacedDigit()
                        .foregroundStyle(item.isBad ? Color.red : Color.primary)
                    Text(item.caption)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 6)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 7))
            }
        }
    }
}

/// Horizontal quota / share bar.
struct MeterBar: View {
    let fraction: Double?
    let lamp: StatusLamp

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                if let fraction {
                    Capsule()
                        .fill(lamp.color)
                        .frame(width: max(0, min(1, fraction)) * geometry.size.width)
                }
            }
        }
        .frame(height: 4)
    }
}

/// Sparkline for the 24-hour output curve.
///
/// Draws nothing and says so when `values` is `nil` — this is the v2-B seam.
/// Until history lands, the shape here is fed by the snapshot's own hourly
/// buckets; an absent series must read as "没有数据", never as a flat zero line.
struct Sparkline: View {
    let values: [Double]?
    let emptyText: String

    var body: some View {
        Group {
            if let values, values.count > 1, let peak = values.max(), peak > 0 {
                GeometryReader { geometry in
                    let width = geometry.size.width
                    let height = geometry.size.height
                    let step = width / CGFloat(values.count - 1)
                    let points = values.enumerated().map { index, value in
                        CGPoint(
                            x: CGFloat(index) * step,
                            y: height - CGFloat(value / peak) * (height - 2) - 1
                        )
                    }
                    ZStack {
                        Path { path in
                            path.move(to: CGPoint(x: 0, y: height))
                            points.forEach { path.addLine(to: $0) }
                            path.addLine(to: CGPoint(x: width, y: height))
                            path.closeSubpath()
                        }
                        .fill(
                            LinearGradient(
                                colors: [Color.teal.opacity(0.45), Color.teal.opacity(0)],
                                startPoint: .top,
                                endPoint: .bottom
                            )
                        )
                        Path { path in
                            guard let first = points.first else { return }
                            path.move(to: first)
                            points.dropFirst().forEach { path.addLine(to: $0) }
                        }
                        .stroke(Color.teal, lineWidth: 1.4)
                    }
                }
                .frame(height: 42)
            } else {
                RoundedRectangle(cornerRadius: 6)
                    .fill(.quaternary.opacity(0.4))
                    .frame(height: 42)
                    .overlay(
                        Text(emptyText)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    )
            }
        }
    }
}

/// One row of the "谁在烧" leaderboard.
struct UsageBarRow: View {
    let name: String
    let value: String
    let trailing: String
    let fraction: Double
    var isSub: Bool = false
    var badge: String?

    var body: some View {
        HStack(spacing: 7) {
            HStack(spacing: 3) {
                Text(name)
                    .font(isSub ? .caption2 : .caption)
                    .foregroundStyle(isSub ? .tertiary : .secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                if let badge {
                    Text(badge)
                        .font(.system(size: 9))
                        .foregroundStyle(.orange)
                        .padding(.horizontal, 3)
                        .overlay(
                            RoundedRectangle(cornerRadius: 3)
                                .stroke(Color.orange.opacity(0.4), lineWidth: 0.5)
                        )
                }
            }
            .frame(width: isSub ? 104 : 92, alignment: .leading)

            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 3).fill(.quaternary.opacity(0.4))
                    RoundedRectangle(cornerRadius: 3)
                        .fill(
                            isSub
                                ? AnyShapeStyle(Color.secondary.opacity(0.35))
                                : AnyShapeStyle(
                                    LinearGradient(
                                        colors: [.blue, .teal],
                                        startPoint: .leading,
                                        endPoint: .trailing
                                    )
                                )
                        )
                        .frame(width: max(2, min(1, fraction) * geometry.size.width))
                }
            }
            .frame(height: 13)

            Text(value)
                .font(.caption)
                .monospacedDigit()
                .frame(width: 54, alignment: .trailing)
            Text(trailing)
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
                .frame(width: 34, alignment: .trailing)
        }
        .accessibilityElement(children: .combine)
    }
}

/// Footer shared by both panels.
struct PanelFooter: View {
    let store: SnapshotStore
    let statusText: String

    var body: some View {
        HStack {
            Text(statusText)
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer()
            Button("刷新") {
                Task { await store.refresh() }
            }
            .buttonStyle(.link)
            .font(.caption)
            .keyboardShortcut("r")
            Button("退出") {
                NSApplication.shared.terminate(nil)
            }
            .buttonStyle(.link)
            .font(.caption)
        }
    }
}

/// Human-readable description of where the numbers came from, for the footer.
///
/// Plain words: this line is visible to 大鸟, so it says "更新于 …" rather than
/// naming transport states.
enum PanelStatusText {
    static func refreshFailure(_ state: TransportState) -> String? {
        switch state {
        case .cached(_, _, .transportFailure): "本次读取失败，以下保留上次数据"
        case .cached(_, _, .incompatibleSchema): "本次数据格式不兼容，以下保留上次数据"
        case .failed: "本次读取失败，暂无可显示的数据"
        case .incompatibleSchema: "本次数据格式不兼容，暂无可显示的数据"
        case .idle, .loading, .live: nil
        }
    }

    static func describe(_ state: TransportState) -> String {
        switch state {
        case .idle:
            "还没取数"
        case .loading:
            "正在取数…"
        case let .live(generatedAt, cacheError):
            "\(StatsFormat.clockBeijing(generatedAt)) 更新（北京）"
                + (cacheError == nil ? "" : " · 这次没存下来")
        case let .cached(generatedAt, expired, _):
            "\(StatsFormat.clockBeijing(generatedAt)) 的旧数据（北京）"
                + (expired ? " · 已经过期" : "")
        case .failed:
            "这次没取到数"
        case .incompatibleSchema:
            "数据格式对不上，没敢用"
        }
    }
}


/// Shared with the unchanged incident/action/in-flight section headings.
struct StatsSectionHeading: View {
    let lamp: StatusLamp
    let title: String
    var count: String? = nil
    var hint: String = ""
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            LampDot(lamp: lamp, size: 7)
            Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(.primary)
            if let count { Text(count).font(.caption).foregroundStyle(.secondary) }
            Spacer()
        }
        .help(hint)
    }
}


/// All five cards share one reading rhythm; colour belongs to status, not titles.
struct StatsCard<Content: View>: View {
    @ViewBuilder let content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 10) { content }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(Color(nsColor: .controlBackgroundColor).opacity(0.7), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color(nsColor: .separatorColor).opacity(0.35)))
    }
}

/// One disclosure behaviour for every nested section, including single Text
/// children. SwiftUI's default intrinsic-width content otherwise centres them.
struct StatsDisclosureStyle: DisclosureGroupStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Button { configuration.isExpanded.toggle() } label: {
                HStack(alignment: .center, spacing: 6) {
                    Image(systemName: configuration.isExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                        .frame(width: 10)
                    configuration.label.frame(maxWidth: .infinity, alignment: .leading)
                        .multilineTextAlignment(.leading)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(configuration.isExpanded ? "已展开" : "已收起")
            if configuration.isExpanded {
                VStack(alignment: .leading, spacing: 6) { configuration.content }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .multilineTextAlignment(.leading)
                    .padding(.leading, 16)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct StatsDetailRow: View {
    let label: String
    let value: String
    init(_ label: String, _ value: String) { self.label = label; self.value = value }
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Text(label).foregroundStyle(.secondary).frame(width: 76, alignment: .leading)
            Text(value).frame(maxWidth: .infinity, alignment: .leading)
        }
        .multilineTextAlignment(.leading)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
