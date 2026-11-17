import Foundation
import Darwin

protocol SnapshotTransport: Sendable {
    func fetch() async throws -> Data
}

/// An explicit inspection never falls back to the production transport or cache.
enum AppSnapshotSource: Equatable, Sendable {
    case live
    case inspection(String)
    case invalidInspection

    init(arguments: [String]) {
        let positions = arguments.indices.filter {
            arguments[$0] == "--inspect-snapshot" || arguments[$0].hasPrefix("--inspect-snapshot=")
        }
        guard !positions.isEmpty else { self = .live; return }
        guard positions.count == 1, let position = positions.first else {
            self = .invalidInspection
            return
        }
        let value = arguments[position].hasPrefix("--inspect-snapshot=")
            ? String(arguments[position].dropFirst("--inspect-snapshot=".count))
            : (position + 1 < arguments.count ? arguments[position + 1] : "")
        self = value.hasPrefix("/") ? .inspection(value) : .invalidInspection
    }

    var notice: String? {
        switch self {
        case .live: nil
        case let .inspection(path): "本地快照 · \(URL(fileURLWithPath: path).lastPathComponent)"
        case .invalidInspection: "本地快照路径无效"
        }
    }

    @MainActor func inspectionStore() -> SnapshotStore? {
        switch self {
        case .live: return nil
        case let .inspection(path):
            return SnapshotStore(transport: LocalSnapshotTransport(path: path), cache: MemorySnapshotCache())
        case .invalidInspection:
            return SnapshotStore(transport: DataSnapshotTransport(result: .failure(
                ErrorBox(message: "snapshot_file_requires_one_absolute_path"))), cache: MemorySnapshotCache())
        }
    }
}

/// Reads the original publication bytes. Decoding and freshness stay in SnapshotStore.
struct LocalSnapshotTransport: SnapshotTransport {
    let path: String
    static let maximumOutputBytes = 1_048_576

    func fetch() async throws -> Data {
        guard path.hasPrefix("/") else { throw ErrorBox(message: "snapshot_file_requires_absolute_path") }
        let descriptor = Darwin.open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard descriptor >= 0 else { throw ErrorBox(message: "snapshot_file_unavailable") }
        defer { Darwin.close(descriptor) }
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0,
              metadata.st_mode & S_IFMT == S_IFREG,
              metadata.st_uid == geteuid(), metadata.st_mode & 0o077 == 0 else {
            throw ErrorBox(message: "snapshot_file_requires_private_regular_file")
        }
        guard metadata.st_size <= Self.maximumOutputBytes else {
            throw ErrorBox(message: "snapshot_file_oversize")
        }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count < 0 {
                if errno == EINTR { continue }
                throw ErrorBox(message: "snapshot_file_read_failed")
            }
            if count == 0 { return data }
            guard data.count + count <= Self.maximumOutputBytes else {
                throw ErrorBox(message: "snapshot_file_oversize")
            }
            data.append(contentsOf: buffer.prefix(count))
        }
    }
}

enum FixtureTransportError: Error, LocalizedError {
    case missingFixture(String)

    var errorDescription: String? {
        switch self {
        case let .missingFixture(name): "Missing bundled fixture: \(name).json"
        }
    }
}

struct BundleFixtureTransport: SnapshotTransport {
    let fixtureName: String

    func fetch() async throws -> Data {
        guard let url = Bundle.main.url(forResource: fixtureName, withExtension: "json") else {
            throw FixtureTransportError.missingFixture(fixtureName)
        }
        return try Data(contentsOf: url)
    }
}

struct DataSnapshotTransport: SnapshotTransport {
    let result: Result<Data, ErrorBox>

    func fetch() async throws -> Data {
        try result.get()
    }
}

struct ErrorBox: Error, Equatable, LocalizedError, Sendable {
    let message: String

    var errorDescription: String? { message }
}

struct FixedCommand: Equatable, Sendable {
    let executable: String
    let arguments: [String]
    let timeoutSeconds: TimeInterval
    let terminationGraceSeconds: TimeInterval
    let maximumOutputBytes: Int
}

enum FixedCommandError: Error, Equatable, LocalizedError, Sendable {
    case launchFailed
    case nonzeroExit(Int32)
    case timedOut
    case outputTooLarge
    case temporaryFileFailed

    var errorDescription: String? {
        switch self {
        case .launchFailed: "exporter_launch_failed"
        case let .nonzeroExit(status): "exporter_exit_\(status)"
        case .timedOut: "exporter_timeout"
        case .outputTooLarge: "exporter_payload_oversize"
        case .temporaryFileFailed: "exporter_local_io_failed"
        }
    }
}

protocol FixedCommandRunning: Sendable {
    func run(_ command: FixedCommand) async throws -> Data
}

struct FoundationFixedCommandRunner: FixedCommandRunning {
    func run(_ command: FixedCommand) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    continuation.resume(returning: try runSynchronously(command))
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private func runSynchronously(_ command: FixedCommand) throws -> Data {
        let fileManager = FileManager.default
        let directory = fileManager.temporaryDirectory
            .appendingPathComponent("pulse-stats-command-\(UUID().uuidString)", isDirectory: true)
        let stdoutURL = directory.appendingPathComponent("stdout")
        let stderrURL = directory.appendingPathComponent("stderr")
        do {
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: false,
                attributes: [.posixPermissions: 0o700]
            )
            guard fileManager.createFile(
                atPath: stdoutURL.path,
                contents: nil,
                attributes: [.posixPermissions: 0o600]
            ), fileManager.createFile(
                atPath: stderrURL.path,
                contents: nil,
                attributes: [.posixPermissions: 0o600]
            ) else {
                throw FixedCommandError.temporaryFileFailed
            }
        } catch let error as FixedCommandError {
            throw error
        } catch {
            throw FixedCommandError.temporaryFileFailed
        }
        defer { try? fileManager.removeItem(at: directory) }

        let stdoutHandle: FileHandle
        let stderrHandle: FileHandle
        do {
            stdoutHandle = try FileHandle(forWritingTo: stdoutURL)
            stderrHandle = try FileHandle(forWritingTo: stderrURL)
        } catch {
            throw FixedCommandError.temporaryFileFailed
        }
        defer {
            try? stdoutHandle.close()
            try? stderrHandle.close()
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: command.executable)
        process.arguments = command.arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdoutHandle
        process.standardError = stderrHandle
        do {
            try process.run()
        } catch {
            throw FixedCommandError.launchFailed
        }

        let deadline = Date().addingTimeInterval(command.timeoutSeconds)
        var oversized = false
        while process.isRunning && Date() < deadline {
            if outputSize(at: stdoutURL, fileManager: fileManager) > command.maximumOutputBytes {
                oversized = true
                process.terminate()
                break
            }
            Thread.sleep(forTimeInterval: 0.02)
        }
        if oversized {
            stop(process, graceSeconds: command.terminationGraceSeconds)
            throw FixedCommandError.outputTooLarge
        }
        if process.isRunning {
            stop(process, graceSeconds: command.terminationGraceSeconds)
            throw FixedCommandError.timedOut
        }
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw FixedCommandError.nonzeroExit(process.terminationStatus)
        }

        let attributes: [FileAttributeKey: Any]
        do {
            try stdoutHandle.synchronize()
            attributes = try fileManager.attributesOfItem(atPath: stdoutURL.path)
        } catch {
            throw FixedCommandError.temporaryFileFailed
        }
        let size = (attributes[.size] as? NSNumber)?.intValue ?? 0
        guard size <= command.maximumOutputBytes else {
            throw FixedCommandError.outputTooLarge
        }
        do {
            return try Data(contentsOf: stdoutURL)
        } catch {
            throw FixedCommandError.temporaryFileFailed
        }
    }

    private func outputSize(at url: URL, fileManager: FileManager) -> Int {
        let attributes = try? fileManager.attributesOfItem(atPath: url.path)
        return (attributes?[.size] as? NSNumber)?.intValue ?? 0
    }

    private func stop(_ process: Process, graceSeconds: TimeInterval) {
        if process.isRunning { process.terminate() }
        let graceDeadline = Date().addingTimeInterval(graceSeconds)
        while process.isRunning && Date() < graceDeadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        if process.isRunning {
            Darwin.kill(process.processIdentifier, SIGKILL)
        }
        process.waitUntilExit()
    }
}

/// Runs the installed snapshot exporter on this Mac under the App's own account.
struct ExporterSnapshotTransport: SnapshotTransport {
    /// `/usr/bin/python3` is only the Command Line Tools stub on macOS; the
    /// exporter runs in the account's shared Python environment.
    static func command(home: String) -> FixedCommand {
        FixedCommand(
            executable: home + "/.local/share/coevolution/python-env/bin/python",
            arguments: [
                "-I",
                "-B",
                home + "/.local/share/pulse/stats/current/Stats/exporter/pulse-stats-snapshot.py",
                "--cost-dashboard",
                home + "/.local/state/pulse/stats/cost-dashboard-v1.json",
                "--json",
            ],
            // 90s, not 30s. Measured 2026-08-03 the same collection took
            // 17.1 / 17.8 / 10.9 / 23.5s across four runs an hour apart: the
            // exporter's cost tracks how busy the machine is, so the spread is
            // the normal condition. A too-tight ceiling throws the fetch away
            // and renders 数据已过期, which reads as bad data when the truth is
            // "we stopped waiting too early". The ceiling only bounds a
            // genuinely hung exporter run.
            timeoutSeconds: 90,
            terminationGraceSeconds: 2,
            maximumOutputBytes: 1_048_576
        )
    }

    let command: FixedCommand
    let runner: any FixedCommandRunning

    init(
        home: String = FileManager.default.homeDirectoryForCurrentUser.path,
        runner: any FixedCommandRunning = FoundationFixedCommandRunner()
    ) {
        self.command = Self.command(home: home)
        self.runner = runner
    }

    func fetch() async throws -> Data {
        try await runner.run(command)
    }
}
