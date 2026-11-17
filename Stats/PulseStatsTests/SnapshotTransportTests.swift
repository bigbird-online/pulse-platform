import Foundation
import XCTest
@testable import PulseStats

private final class StubCommandRunner: FixedCommandRunning, @unchecked Sendable {
    let result: Result<Data, FixedCommandError>
    private(set) var commands: [FixedCommand] = []

    init(result: Result<Data, FixedCommandError>) {
        self.result = result
    }

    func run(_ command: FixedCommand) async throws -> Data {
        commands.append(command)
        return try result.get()
    }
}

final class SnapshotTransportTests: XCTestCase {
    @MainActor func testExplicitInspectionNeverSelectsLiveOnInvalidArguments() {
        XCTAssertEqual(AppSnapshotSource(arguments: ["app"]), .live)
        XCTAssertNil(AppSnapshotSource.live.inspectionStore())
        for args in [["app", "--inspect-snapshot"], ["app", "--inspect-snapshot", "relative.json"],
                     ["app", "--inspect-snapshot=relative.json"], ["app", "--inspect-snapshot="],
                     ["app", "--inspect-snapshot", "/one", "--inspect-snapshot", "/two"]] {
            let source = AppSnapshotSource(arguments: args)
            XCTAssertEqual(source, .invalidInspection)
            XCTAssertNotNil(source.inspectionStore())
        }
        XCTAssertEqual(AppSnapshotSource(arguments: ["app", "--inspect-snapshot", "/private/muse.json"]),
                       .inspection("/private/muse.json"))
        XCTAssertEqual(AppSnapshotSource(arguments: ["app", "--inspect-snapshot=/private/muse.json"]),
                       .inspection("/private/muse.json"))
    }

    func testLocalPublicationPreservesBytesAndReadsAtomicReplacement() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("snapshot.json")
        let first = Data("{\"generated_at\":\"2026-01-01\",\"unknown_extension\":true}\n".utf8)
        try first.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        let transport = LocalSnapshotTransport(path: url.path)
        let received = try await transport.fetch()
        XCTAssertEqual(received, first)
        let replacement = Data("second publication".utf8)
        try replacement.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        let replaced = try await transport.fetch()
        XCTAssertEqual(replaced, replacement)
    }

    func testLocalPublicationRejectsSymlinkPublicFileDirectoryAndOversize() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("snapshot.json")
        try Data("{}".utf8).write(to: url)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: url.path)
        let link = directory.appendingPathComponent("link.json")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: url)
        for path in [url.path, link.path, directory.path, directory.appendingPathComponent("absent").path] {
            do { _ = try await LocalSnapshotTransport(path: path).fetch(); XCTFail("accepted \(path)") }
            catch { XCTAssertTrue(error is ErrorBox) }
        }
        try Data(repeating: 0, count: LocalSnapshotTransport.maximumOutputBytes + 1).write(to: url)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        do { _ = try await LocalSnapshotTransport(path: url.path).fetch(); XCTFail("accepted oversize") }
        catch { XCTAssertEqual(error as? ErrorBox, ErrorBox(message: "snapshot_file_oversize")) }
    }

    @MainActor func testInspectionUsesRealDecoderAndPreservesExpiredSnapshotOnMissingFile() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("snapshot.json")
        try fixtureData("expired-cache").write(to: url)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        let store = try XCTUnwrap(AppSnapshotSource.inspection(url.path).inspectionStore())
        await store.refresh()
        let original = try XCTUnwrap(store.snapshot)
        XCTAssertNil(PanelStatusText.refreshFailure(store.transportState))
        XCTAssertLessThan(original.expiresAt, Date())
        try FileManager.default.removeItem(at: url)
        await store.refresh()
        XCTAssertEqual(store.snapshot, original)
        guard case .cached(_, let expired, _) = store.transportState else { return XCTFail("missing degraded cache") }
        XCTAssertTrue(expired)
        XCTAssertEqual(PanelStatusText.refreshFailure(store.transportState), "本次读取失败，以下保留上次数据")
        let restarted = try XCTUnwrap(AppSnapshotSource.inspection(url.path).inspectionStore())
        await restarted.refresh()
        XCTAssertNil(restarted.snapshot, "inspection must not persist or load production cache")
        XCTAssertEqual(PanelStatusText.refreshFailure(restarted.transportState), "本次读取失败，暂无可显示的数据")
        try fixtureData("expired-cache").write(to: url)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        await restarted.refresh()
        XCTAssertEqual(restarted.snapshot, original)
        XCTAssertNil(PanelStatusText.refreshFailure(restarted.transportState), "successful recovery clears the failure notice")
    }

    func testExporterCommandRunsLocallyInTheAccountPythonEnvironment() {
        let command = ExporterSnapshotTransport.command(home: "/Users/example")
        XCTAssertEqual(command.executable, "/Users/example/.local/share/coevolution/python-env/bin/python")
        XCTAssertEqual(
            command.arguments,
            [
                "-I",
                "-B",
                "/Users/example/.local/share/pulse/stats/current/Stats/exporter/pulse-stats-snapshot.py",
                "--cost-dashboard",
                "/Users/example/.local/state/pulse/stats/cost-dashboard-v1.json",
                "--json",
            ]
        )
        XCTAssertFalse(([command.executable] + command.arguments).contains { $0.contains("ssh") || $0.hasPrefix("/home/") })
        // Must stay comfortably above the exporter's real cost, and "comfortably"
        // has to account for spread, not just the median: measured 2026-08-03
        // the same collection took 17.1 / 17.8 / 10.9 / 23.5s across four runs.
        // A dropped fetch is not silent — it renders as 数据已过期, which reads
        // as bad data rather than "we stopped waiting too early".
        // Do not lower this without re-measuring against production first.
        XCTAssertEqual(command.timeoutSeconds, 90)
        XCTAssertEqual(command.maximumOutputBytes, 1_048_576)
    }

    func testDefaultTransportUsesTheCurrentUsersHome() {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        XCTAssertEqual(ExporterSnapshotTransport().command, ExporterSnapshotTransport.command(home: home))
    }

    func testTransportReturnsRunnerPayload() async throws {
        let expected = Data("snapshot".utf8)
        let runner = StubCommandRunner(result: .success(expected))
        let transport = ExporterSnapshotTransport(home: "/Users/example", runner: runner)
        let actual = try await transport.fetch()
        XCTAssertEqual(actual, expected)
        XCTAssertEqual(runner.commands, [ExporterSnapshotTransport.command(home: "/Users/example")])
    }

    func testTransportPreservesStableRunnerError() async {
        let transport = ExporterSnapshotTransport(runner: StubCommandRunner(result: .failure(.timedOut)))
        do {
            _ = try await transport.fetch()
            XCTFail("expected timeout")
        } catch {
            XCTAssertEqual(error as? FixedCommandError, .timedOut)
            XCTAssertEqual(error.localizedDescription, "exporter_timeout")
        }
    }

    func testFoundationRunnerSuccessAndNonzeroExit() async throws {
        let runner = FoundationFixedCommandRunner()
        let output = try await runner.run(
            command(executable: "/usr/bin/printf", arguments: ["snapshot"], maximumOutputBytes: 16)
        )
        XCTAssertEqual(String(decoding: output, as: UTF8.self), "snapshot")

        do {
            _ = try await runner.run(command(executable: "/usr/bin/false"))
            XCTFail("expected nonzero exit")
        } catch {
            XCTAssertEqual(error as? FixedCommandError, .nonzeroExit(1))
        }
    }

    func testFoundationRunnerTimesOutAndKillsChild() async {
        let runner = FoundationFixedCommandRunner()
        do {
            _ = try await runner.run(
                command(
                    executable: "/bin/sleep",
                    arguments: ["5"],
                    timeoutSeconds: 0.05,
                    terminationGraceSeconds: 0.05
                )
            )
            XCTFail("expected timeout")
        } catch {
            XCTAssertEqual(error as? FixedCommandError, .timedOut)
        }
    }

    func testFoundationRunnerRejectsOversizeOutput() async {
        let runner = FoundationFixedCommandRunner()
        do {
            _ = try await runner.run(
                command(executable: "/usr/bin/printf", arguments: [String(repeating: "x", count: 64)], maximumOutputBytes: 8)
            )
            XCTFail("expected oversize failure")
        } catch {
            XCTAssertEqual(error as? FixedCommandError, .outputTooLarge)
        }
    }

    func testFoundationRunnerStopsStreamingOversizeBeforeTimeout() async {
        let runner = FoundationFixedCommandRunner()
        do {
            _ = try await runner.run(
                command(
                    executable: "/usr/bin/python3",
                    arguments: ["-c", "import sys,time;sys.stdout.write('x'*1024);sys.stdout.flush();time.sleep(5)"],
                    timeoutSeconds: 4,
                    maximumOutputBytes: 8
                )
            )
            XCTFail("expected oversize failure")
        } catch {
            // `.outputTooLarge` is only reached when the runner detects the
            // oversize file while still inside the command deadline. Measuring
            // total wall time here also includes Process terminate/cleanup latency
            // on the CI host and does not strengthen that functional guarantee.
            XCTAssertEqual(error as? FixedCommandError, .outputTooLarge)
        }
    }

    private func command(
        executable: String,
        arguments: [String] = [],
        timeoutSeconds: TimeInterval = 1,
        terminationGraceSeconds: TimeInterval = 0.1,
        maximumOutputBytes: Int = 1_024
    ) -> FixedCommand {
        FixedCommand(
            executable: executable,
            arguments: arguments,
            timeoutSeconds: timeoutSeconds,
            terminationGraceSeconds: terminationGraceSeconds,
            maximumOutputBytes: maximumOutputBytes
        )
    }
}
