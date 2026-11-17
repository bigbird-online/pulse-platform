import Foundation

enum FileSnapshotCacheError: Error, LocalizedError {
    case applicationSupportUnavailable
    case invalidCache
    case readFailed
    case writeFailed
    case clearFailed

    var errorDescription: String? {
        switch self {
        case .applicationSupportUnavailable: "cache_directory_unavailable"
        case .invalidCache: "cache_invalid"
        case .readFailed: "cache_read_failed"
        case .writeFailed: "cache_write_failed"
        case .clearFailed: "cache_clear_failed"
        }
    }
}

@MainActor
final class FileSnapshotCache: SnapshotCache {
    static func defaultURL(fileManager: FileManager = .default) throws -> URL {
        guard let root = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            throw FileSnapshotCacheError.applicationSupportUnavailable
        }
        return root
            .appendingPathComponent("PulseStats", isDirectory: true)
            .appendingPathComponent("snapshot-v2.json", isDirectory: false)
    }

    private let url: URL
    private let fileManager: FileManager
    private let decoder: SnapshotDecoder

    init(
        url: URL,
        fileManager: FileManager = .default,
        decoder: SnapshotDecoder = SnapshotDecoder()
    ) {
        self.url = url
        self.fileManager = fileManager
        self.decoder = decoder
    }

    convenience init(fileManager: FileManager = .default) throws {
        try self.init(url: Self.defaultURL(fileManager: fileManager), fileManager: fileManager)
    }

    func load() throws -> PulseSnapshot? {
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        let data: Data
        do {
            data = try Data(contentsOf: url, options: [.mappedIfSafe])
        } catch {
            throw FileSnapshotCacheError.readFailed
        }
        do {
            return try decoder.decode(data)
        } catch {
            throw FileSnapshotCacheError.invalidCache
        }
    }

    func save(_ snapshot: PulseSnapshot) throws {
        let data: Data
        do {
            data = try Self.makeEncoder().encode(snapshot)
        } catch {
            throw FileSnapshotCacheError.writeFailed
        }
        try persist(data)
    }

    /// Persist exactly the response that produced this validated snapshot.
    /// Re-encoding a presentation model drops additive fields and truncates source
    /// timestamps, making a restart's evidence different from the live response.
    func save(_ snapshot: PulseSnapshot, source: Data) throws {
        guard source.count <= 1_048_576,
              let decoded = try? decoder.decode(source), decoded == snapshot else {
            throw FileSnapshotCacheError.invalidCache
        }
        try persist(source)
    }

    private func persist(_ data: Data) throws {
        do {
            let directory = url.deletingLastPathComponent()
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
            try data.write(to: url, options: [.atomic])
            try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            throw FileSnapshotCacheError.writeFailed
        }
    }

    func clear() throws {
        guard fileManager.fileExists(atPath: url.path) else { return }
        do {
            try fileManager.removeItem(at: url)
        } catch {
            throw FileSnapshotCacheError.clearFailed
        }
    }

    private static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            try container.encode(formatter.string(from: date))
        }
        return encoder
    }
}
