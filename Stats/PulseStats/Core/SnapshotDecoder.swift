import Foundation

enum SnapshotDecodeError: Error, Equatable, LocalizedError {
    case invalidJSON
    case unsupportedSchema(String)
    case invalidPayload(String)

    var errorDescription: String? {
        switch self {
        case .invalidJSON:
            "Snapshot is not valid JSON."
        case let .unsupportedSchema(version):
            "Unsupported snapshot schema: \(version)"
        case let .invalidPayload(reason):
            "Snapshot violates the v2 contract: \(reason)"
        }
    }
}

struct SnapshotDecoder: Sendable {
    private struct SchemaEnvelope: Decodable {
        let schemaVersion: String
    }

    func decode(_ data: Data) throws -> PulseSnapshot {
        let decoder = Self.makeDecoder()
        let envelope: SchemaEnvelope

        do {
            envelope = try decoder.decode(SchemaEnvelope.self, from: data)
        } catch {
            throw SnapshotDecodeError.invalidJSON
        }

        guard envelope.schemaVersion == PulseSnapshot.supportedSchemaVersion else {
            throw SnapshotDecodeError.unsupportedSchema(envelope.schemaVersion)
        }

        do {
            let snapshot = try Self.makeDecoder().decode(PulseSnapshot.self, from: data)
            guard snapshot.overall.openIncidentCount == snapshot.incidents.filter({ $0.state == .open }).count else {
                throw SnapshotDecodeError.invalidPayload("open_incident_count does not match incidents")
            }
            return snapshot
        } catch let error as SnapshotDecodeError {
            throw error
        } catch {
            throw SnapshotDecodeError.invalidPayload(String(describing: error))
        }
    }

    static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .custom { path in
            let source = path.last?.stringValue ?? ""
            let components = source.split(separator: "_").map(String.init)
            guard let first = components.first else { return SnapshotCodingKey(source) }
            let converted = components.dropFirst().reduce(first) { result, component in
                result + (component == "id" ? "ID" : component.prefix(1).uppercased() + component.dropFirst())
            }
            return SnapshotCodingKey(converted)
        }
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            if let date = Self.parseISO8601(value) {
                return date
            }
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Invalid ISO-8601 date: \(value)"
            )
        }
        return decoder
    }

    static func parseISO8601(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) {
            return date
        }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }
}

private struct SnapshotCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil

    init(_ stringValue: String) {
        self.stringValue = stringValue
    }

    init?(stringValue: String) {
        self.init(stringValue)
    }

    init?(intValue: Int) {
        return nil
    }
}
