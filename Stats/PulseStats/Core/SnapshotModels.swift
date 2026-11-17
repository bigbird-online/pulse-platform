import Foundation

enum RuntimeHealth: String, Codable, CaseIterable, Sendable {
    case ok
    case warning
    case critical
    case unknown
    case stale
}

enum Coverage: String, Codable, CaseIterable, Sendable {
    case full
    case partial
    case unknown
}

enum ImplementationStatus: String, Codable, Sendable {
    case activeValidated = "active_validated"
    case activePartial = "active_partial"
    case retired
    case notImplemented = "not_implemented"
}

enum CapabilityHealth: String, Codable, Sendable {
    case ok
    case warning
    case critical
    case unknown
    case stale
    case retired
    case notApplicable = "not_applicable"
}

enum AggregationRole: String, Codable, Sendable {
    case runtime
    case coverageOnly = "coverage_only"
}

enum FreshnessState: String, Codable, Sendable {
    case fresh
    case stale
    case unknown
    case error
    case notApplicable = "not_applicable"
}

enum IncidentState: String, Codable, Sendable {
    case open
    case resolved
    /// Forward-compatible landing state. It is retained in the decoded
    /// snapshot for audit, but never guessed into the user-visible open list.
    case unknown
}

enum Severity: String, Codable, Sendable {
    case info
    case warning
    case critical
}

enum MetricStatus: String, Codable, Sendable {
    case available
    case warmingUp = "warming_up"
    case unknown
    case notApplicable = "not_applicable"
}

enum MetricQuality: String, Codable, Sendable {
    case validated
    case descriptive
    case insufficient
    case unavailable
}

/// Which of the three panel sections an alert belongs in (spec v3 §2).
///
/// The judgement is made **in the exporter, not here** (spec v3 §1 边界:
/// "分档在 exporter 侧完成，App 端只渲染不判读"). The app never re-derives a
/// tier from severity or label — that is exactly the two-sided drift the spec
/// set out to end ("同一件事在两个地方是两个说法").
enum AlertTier: String, Codable, Sendable, CaseIterable {
    /// 要你出手 — cannot fix itself *and* has already hit a real person or job.
    case act
    /// 我在盯 — something is off but the system handles it. **The default.**
    case watch
    /// 已经过去 — already handled, kept for the record.
    case done

    /// Spec v3 §2: "默认档是 `watch` 不是 `act`。不在任何白名单里的一律落
    /// `watch`。" A producer that forgets the field, or ships a tier this
    /// build does not know, must not be able to push anything into `act`
    /// (that would be crying wolf), nor bury it in `done` (that would be
    /// losing it). `watch` is the only safe landing spot in both directions.
    static let fallback: AlertTier = .watch
}

struct PulseSnapshot: Codable, Equatable, Sendable {
    static let supportedSchemaVersion = "pulse.monitoring.snapshot.v2"

    let schemaVersion: String
    let generatedAt: Date
    let expiresAt: Date
    let baselineVersion: String
    let overall: OverallStatus
    let capabilities: [Capability]
    let incidents: [Incident]
    let metrics: [Metric]
    let alerts: [Alert]
    /// Spec v3 §6. Additive within `…snapshot.v2`, so the schema version did
    /// not change and a producer that predates v3 still validates — hence the
    /// whole key has to be optional, not just its members.
    let alertTiers: AlertTiers
    let provenance: Provenance
    /// Additive read-only projection from the canonical four-surface carrier.
    /// Older v2 snapshots legitimately omit it; absence is unknown evidence,
    /// never implicit local or global validation.
    let globalAuthority: GlobalAuthorityProjection?
    let costDashboard: CostDashboard?
    /// The cost publisher's latest cycle; describes, never replaces, the carrier.
    let costRefresh: CostRefreshStatus?

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try container.decode(String.self, forKey: .schemaVersion)
        generatedAt = try container.decode(Date.self, forKey: .generatedAt)
        expiresAt = try container.decode(Date.self, forKey: .expiresAt)
        baselineVersion = try container.decode(String.self, forKey: .baselineVersion)
        overall = try container.decode(OverallStatus.self, forKey: .overall)
        capabilities = try container.decode([Capability].self, forKey: .capabilities)
        incidents = try container.decode([Incident].self, forKey: .incidents)
        metrics = try container.decode([Metric].self, forKey: .metrics)
        alerts = try container.decode([Alert].self, forKey: .alerts)
        alertTiers = try container.decodeIfPresent(AlertTiers.self, forKey: .alertTiers) ?? .zero
        provenance = try container.decode(Provenance.self, forKey: .provenance)
        // A bad optional cost carrier must not blank incidents/actions/inventory.
        costDashboard = try? container.decodeIfPresent(CostDashboardCarrier.self, forKey: .costDashboard)?.dashboard
        costRefresh = try? container.decodeIfPresent(CostRefreshStatus.self, forKey: .costRefresh)
        globalAuthority = try container.decodeIfPresent(
            GlobalAuthorityProjection.self,
            forKey: .globalAuthority
        )
    }

    init(
        schemaVersion: String,
        generatedAt: Date,
        expiresAt: Date,
        baselineVersion: String,
        overall: OverallStatus,
        capabilities: [Capability],
        incidents: [Incident],
        metrics: [Metric],
        alerts: [Alert],
        alertTiers: AlertTiers = .zero,
        provenance: Provenance,
        globalAuthority: GlobalAuthorityProjection? = nil,
        costDashboard: CostDashboard? = nil,
        costRefresh: CostRefreshStatus? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.generatedAt = generatedAt
        self.expiresAt = expiresAt
        self.baselineVersion = baselineVersion
        self.overall = overall
        self.capabilities = capabilities
        self.incidents = incidents
        self.metrics = metrics
        self.alerts = alerts
        self.alertTiers = alertTiers
        self.provenance = provenance
        self.globalAuthority = globalAuthority
        self.costDashboard = costDashboard
        self.costRefresh = costRefresh
    }
}

struct GlobalAuthorityProjection: Codable, Equatable, Sendable {
    let schemaVersion: String
    let status: String
    let scope: String
    let authorityRevision: String?
    let requiredEndpointCount: Int?
    let materializedEndpointCount: Int?
    let materializationDigest: String?
    let freshness: String
    let degradedReasons: [String]
}

struct OverallStatus: Codable, Equatable, Sendable {
    let runtimeHealth: RuntimeHealth
    let coverage: Coverage
    let openIncidentCount: Int
    let headline: String
}

struct Freshness: Codable, Equatable, Sendable {
    let state: FreshnessState
    let observedAt: Date?
    let maxAgeSeconds: Int?
}

struct Capability: Codable, Equatable, Identifiable, Sendable {
    let id: String
    let implementationStatus: ImplementationStatus
    let health: CapabilityHealth
    let aggregationRole: AggregationRole
    let scope: String
    let reason: String
    let freshness: Freshness
    let evidence: [String]
}

struct Incident: Codable, Equatable, Identifiable, Sendable {
    var id: String { episodeID }

    let incidentKey: String
    let episodeID: String
    let state: IncidentState
    let severity: Severity
    let capabilityID: String
    let scope: String
    let title: String
    let whatHappened: String
    let impact: String
    let suggestedAction: String
    let openedAt: Date
    /// Optional by design: a missing/invalid display timestamp must render as
    /// "未知", never as `distantPast` or another fabricated instant.
    let updatedAt: Date?
    let resolvedAt: Date?
    let occurrenceCount: Int
    let reopenCount: Int
    let evidence: [String]
    let recoveryEvidence: [String]

    /// The reducer's identity and lifecycle evidence remain strict. Only the
    /// four fields the incident card presents are allowed to degrade locally:
    /// title, impact, suggested action and updated time. This keeps one partial
    /// record from blanking the whole panel without inventing an incident id or
    /// silently treating a future state as open.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        incidentKey = try container.decode(String.self, forKey: .incidentKey)
        episodeID = try container.decode(String.self, forKey: .episodeID)
        let rawState = try? container.decode(String.self, forKey: .state)
        state = rawState.flatMap(IncidentState.init(rawValue:)) ?? .unknown
        severity = try container.decode(Severity.self, forKey: .severity)
        capabilityID = try container.decode(String.self, forKey: .capabilityID)
        scope = try container.decode(String.self, forKey: .scope)
        title = Self.displayText(
            try? container.decode(String.self, forKey: .title),
            fallback: "未知事故"
        )
        whatHappened = try container.decode(String.self, forKey: .whatHappened)
        impact = Self.displayText(
            try? container.decode(String.self, forKey: .impact),
            fallback: "影响未知"
        )
        suggestedAction = Self.displayText(
            try? container.decode(String.self, forKey: .suggestedAction),
            fallback: "建议动作未知"
        )
        openedAt = try container.decode(Date.self, forKey: .openedAt)
        updatedAt = try? container.decode(Date.self, forKey: .updatedAt)
        resolvedAt = try container.decodeIfPresent(Date.self, forKey: .resolvedAt)
        occurrenceCount = try container.decode(Int.self, forKey: .occurrenceCount)
        reopenCount = try container.decode(Int.self, forKey: .reopenCount)
        evidence = try container.decode([String].self, forKey: .evidence)
        recoveryEvidence = try container.decode([String].self, forKey: .recoveryEvidence)
    }

    private static func displayText(_ value: String?, fallback: String) -> String {
        guard let value else { return fallback }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? fallback : trimmed
    }
}

/// A metric value as it actually arrives on the wire.
///
/// v2-A widened `value` beyond scalars: `usage.by_agent` / `usage.by_session` /
/// `harness.wi.stalled` are object arrays, `usage.hourly.output_tokens` is a
/// number array, `usage.today.inbound_types` /
/// `delivery.today.attempts_histogram` are maps, and `relay.cc.rate_limited` is
/// a bool. Decoding those into `Double?` throws, and `SnapshotDecoder` turns any
/// throw into `invalidPayload`, which rejects the *whole* snapshot. So this type
/// exists to make an unrecognised shape degrade to `.unsupported` rather than
/// take the snapshot down with it (spec §4: "v1 clients reading an unknown
/// metric should ignore it, not error").
///
/// Everything here is read-only presentation input. Nothing is ever coerced into
/// a number it isn't — an absent or wrong-shaped value stays absent so the UI can
/// show "未知" instead of a fabricated 0.
enum MetricValue: Codable, Equatable, Sendable {
    case number(Double)
    case boolean(Bool)
    case numbers([Double])
    case rows([[String: MetricScalar]])
    case counts([String: Double])
    /// A short label such as a plan name; never parsed into a number.
    case text(String)
    case null
    /// Shape we do not model. Deliberately not an error.
    case unsupported

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
            return
        }
        // Bool must precede Double: JSONDecoder will happily read `true` as 1.0.
        if let boolean = try? container.decode(Bool.self) {
            self = .boolean(boolean)
            return
        }
        if let number = try? container.decode(Double.self) {
            self = .number(number)
            return
        }
        if let numbers = try? container.decode([Double].self) {
            self = .numbers(numbers)
            return
        }
        if let rows = try? container.decode([[String: MetricScalar]].self) {
            self = .rows(rows)
            return
        }
        if let counts = try? container.decode([String: Double].self) {
            self = .counts(counts)
            return
        }
        if let text = try? container.decode(String.self) {
            self = .text(text)
            return
        }
        self = .unsupported
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case let .number(value): try container.encode(value)
        case let .boolean(value): try container.encode(value)
        case let .numbers(values): try container.encode(values)
        case let .rows(rows): try container.encode(rows)
        case let .counts(counts): try container.encode(counts)
        case let .text(text): try container.encode(text)
        // `.unsupported` round-trips as null: the cache must stay decodable, and
        // we already know we cannot render this value.
        case .null, .unsupported: try container.encodeNil()
        }
    }

    /// The scalar reading, or `nil` when this metric is not a plain number.
    /// Never substitutes a default — callers render "未知" on `nil`.
    var number: Double? {
        if case let .number(value) = self { return value }
        return nil
    }

    var boolean: Bool? {
        if case let .boolean(value) = self { return value }
        return nil
    }

    var numbers: [Double]? {
        if case let .numbers(values) = self { return values }
        return nil
    }

    var rows: [[String: MetricScalar]]? {
        if case let .rows(rows) = self { return rows }
        return nil
    }

    var counts: [String: Double]? {
        if case let .counts(counts) = self { return counts }
        return nil
    }

    var text: String? {
        if case let .text(text) = self { return text }
        return nil
    }

    var isAbsent: Bool {
        switch self {
        case .null, .unsupported: true
        default: false
        }
    }
}

/// `Int(Double)` traps on NaN, infinity, and anything beyond `Int.max`. Every
/// number here comes from a JSON file we do not control, so all conversions go
/// through this: a nonsense value becomes "unknown", it does not crash the app.
func safeInt(_ value: Double?) -> Int? {
    guard let value, value.isFinite,
          value >= Double(Int.min), value <= Double(Int.max)
    else { return nil }
    return Int(value)
}

/// One field inside a row of a list-valued metric (`usage.by_agent` etc.).
enum MetricScalar: Codable, Equatable, Sendable {
    case string(String)
    case number(Double)
    case boolean(Bool)
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
            return
        }
        if let boolean = try? container.decode(Bool.self) {
            self = .boolean(boolean)
            return
        }
        if let number = try? container.decode(Double.self) {
            self = .number(number)
            return
        }
        self = .string(try container.decode(String.self))
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case let .string(value): try container.encode(value)
        case let .number(value): try container.encode(value)
        case let .boolean(value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }

    var string: String? {
        if case let .string(value) = self { return value }
        return nil
    }

    var number: Double? {
        if case let .number(value) = self { return value }
        return nil
    }

    var bool: Bool? {
        if case let .boolean(value) = self { return value }
        return nil
    }

    var int: Int? { safeInt(number) }
}

struct Metric: Codable, Equatable, Identifiable, Sendable {
    let id: String
    let capabilityID: String
    let status: MetricStatus
    let value: MetricValue
    let unit: String
    let window: String
    let sampleCount: Int?
    let quality: MetricQuality
    let scope: String
    let observedAt: Date?
    let reason: String?

    /// `value` is absent in some fixtures; a missing key must not fail decode.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        capabilityID = try container.decode(String.self, forKey: .capabilityID)
        status = try container.decode(MetricStatus.self, forKey: .status)
        value = try container.decodeIfPresent(MetricValue.self, forKey: .value) ?? .null
        unit = try container.decode(String.self, forKey: .unit)
        window = try container.decode(String.self, forKey: .window)
        sampleCount = try container.decodeIfPresent(Int.self, forKey: .sampleCount)
        quality = try container.decode(MetricQuality.self, forKey: .quality)
        scope = try container.decode(String.self, forKey: .scope)
        observedAt = try container.decodeIfPresent(Date.self, forKey: .observedAt)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
    }

    init(
        id: String,
        capabilityID: String,
        status: MetricStatus,
        value: MetricValue,
        unit: String,
        window: String,
        sampleCount: Int? = nil,
        quality: MetricQuality,
        scope: String,
        observedAt: Date? = nil,
        reason: String? = nil
    ) {
        self.id = id
        self.capabilityID = capabilityID
        self.status = status
        self.value = value
        self.unit = unit
        self.window = window
        self.sampleCount = sampleCount
        self.quality = quality
        self.scope = scope
        self.observedAt = observedAt
        self.reason = reason
    }
}

struct Alert: Codable, Equatable, Identifiable, Sendable {
    var id: String { eventID }

    let eventID: String
    let projectionStatus: String
    let severity: Severity
    let capabilityID: String
    let title: String
    let occurredAt: Date
    let reason: String
    /// Spec v3 §6. Absent in every pre-v3 fixture and in any snapshot produced
    /// by an older exporter, so it must not be required.
    let tier: AlertTier
    /// Optional explicit relation; old cached prose is never treated as identity.
    let workItemID: String?

    /// Hand-written so an absent **or unrecognised** `tier` degrades to
    /// `watch` instead of failing the decode.
    ///
    /// Two separate hazards, one landing spot:
    ///
    /// - **Absent** (old exporter, old cached snapshot on disk): a plain
    ///   `decode` would throw, and `SnapshotDecoder` turns any throw into
    ///   `invalidPayload`, which rejects the *whole* snapshot. One missing
    ///   field would blank the entire panel — the same failure mode
    ///   `MetricValue` was written to avoid.
    /// - **Unknown value** (a future exporter adds a fourth tier): a plain
    ///   `decode` on a `RawRepresentable` enum throws too, so a forward-
    ///   compatible producer would take this build down.
    ///
    /// `decodeIfPresent` returns `nil` for both cases here — for the unknown
    /// value it is `try?` that swallows the raw-value mismatch — and both land
    /// on `AlertTier.fallback`. See that constant for why `watch` and not
    /// either extreme.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        eventID = try container.decode(String.self, forKey: .eventID)
        projectionStatus = try container.decode(String.self, forKey: .projectionStatus)
        severity = try container.decode(Severity.self, forKey: .severity)
        capabilityID = try container.decode(String.self, forKey: .capabilityID)
        title = try container.decode(String.self, forKey: .title)
        occurredAt = try container.decode(Date.self, forKey: .occurredAt)
        reason = try container.decode(String.self, forKey: .reason)
        workItemID = try? container.decodeIfPresent(String.self, forKey: .workItemID)
        tier = (try? container.decodeIfPresent(AlertTier.self, forKey: .tier)).flatMap { $0 } ?? AlertTier.fallback
    }

    init(
        eventID: String,
        projectionStatus: String,
        severity: Severity,
        capabilityID: String,
        title: String,
        occurredAt: Date,
        reason: String,
        tier: AlertTier = AlertTier.fallback,
        workItemID: String? = nil
    ) {
        self.eventID = eventID
        self.projectionStatus = projectionStatus
        self.severity = severity
        self.capabilityID = capabilityID
        self.title = title
        self.occurredAt = occurredAt
        self.reason = reason
        self.tier = tier
        self.workItemID = workItemID
    }
}

/// The producer's own count per tier (spec v3 §6: "App 端不自己数").
///
/// Kept as reported rather than recomputed from `alerts`, deliberately: if the
/// two ever disagree, the honest reading is that the producer truncated or
/// filtered something, and silently papering over that with a local `count`
/// would hide exactly the kind of loss §5 forbids. The panel headline reads
/// these; the section bodies iterate the array.
struct AlertTiers: Codable, Equatable, Sendable {
    let act: Int
    let watch: Int
    let done: Int

    static let zero = AlertTiers(act: 0, watch: 0, done: 0)

    init(act: Int, watch: Int, done: Int) {
        self.act = act
        self.watch = watch
        self.done = done
    }

    /// Each counter defaults to 0 on its own. A pre-v3 snapshot has none of
    /// them, and 0 is the truthful reading there: this producer never claimed
    /// anything needs acting on. Unlike a metric, an absent counter here has
    /// no "未知" rendering to fall back to — the menu bar has to print
    /// something — and "0 件要你出手" from a producer that does not do triage
    /// is the quiet answer, which is the right default per §2.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        act = try container.decodeIfPresent(Int.self, forKey: .act) ?? 0
        watch = try container.decodeIfPresent(Int.self, forKey: .watch) ?? 0
        done = try container.decodeIfPresent(Int.self, forKey: .done) ?? 0
    }
}

struct Producer: Codable, Equatable, Sendable {
    let name: String
    let version: String
}

struct Source: Codable, Equatable, Identifiable, Sendable {
    let id: String
    let freshness: FreshnessState
    let observedAt: Date?
    let errorCode: String?
}

struct Provenance: Codable, Equatable, Sendable {
    let producer: Producer
    let sourceRevisions: [String: String]
    let sources: [Source]
}
