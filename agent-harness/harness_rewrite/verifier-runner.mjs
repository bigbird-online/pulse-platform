// Fixed installed adapter. No command-line model, principal or tool overrides.
import { readFile, open, realpath } from "node:fs/promises";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = process.cwd();
const attempt = process.env.HARNESS_REWRITE_ATTEMPT;
if (attempt && !/^[0-9]{6}$/.test(attempt)) throw new Error("Invalid continuation attempt");
const target = attempt ? join(root, "attempts", attempt) : root;
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
async function fixed(name, value) {
  const handle = await open(join(target, name), "wx", 0o600);
  try { await handle.writeFile(JSON.stringify(value)); await handle.sync(); }
  finally { await handle.close(); }
}
let session;
let invoked = false;
let reason = "sdk_configuration_unavailable";
try {
  const configBytes = await readFile(join(target, "launch-config.json"));
  const config = JSON.parse(configBytes);
  const self = fileURLToPath(import.meta.url);
  if (await realpath(root) !== root || await realpath(target) !== target || self !== join(target, "verifier-runner.mjs") ||
      hash(await readFile(self)) !== config.runner_sha256 ||
      config.effect_id !== process.env.HARNESS_REWRITE_DISPATCH ||
      config.runner !== "pi-sdk/v1") throw new Error();
  const selected = config.selection;
  if (!selected.tools.includes("bash")) throw new Error();
  const sdk = await import(pathToFileURL(config.sdk_entry).href);
  reason = "runtime_transport_unavailable";
  const transport = await import(pathToFileURL(config.http_entry).href);
  const settingsPath = join(config.agent_dir, "settings.json");
  let settingsBytes;
  let networkSettings = {};
  try {
    if (await realpath(settingsPath) !== settingsPath) throw new Error();
    const handle = await open(settingsPath, "r");
    try {
      const info = await handle.stat();
      if (!info.isFile() || info.size > 1048576) throw new Error();
      settingsBytes = await handle.readFile();
    } finally { await handle.close(); }
    const stored = JSON.parse(settingsBytes.toString("utf8").replace(/^\uFEFF/, ""));
    if (!stored || typeof stored !== "object" || Array.isArray(stored)) throw new Error();
    networkSettings = { httpProxy: stored.httpProxy, httpIdleTimeoutMs: stored.httpIdleTimeoutMs };
  } catch (error) { if (error?.code !== "ENOENT") throw error; }
  if (networkSettings.httpProxy !== undefined && typeof networkSettings.httpProxy !== "string") throw new Error();
  const timeout = transport.parseHttpIdleTimeoutMs(networkSettings.httpIdleTimeoutMs);
  if (networkSettings.httpIdleTimeoutMs !== undefined && timeout === undefined) throw new Error();
  const envProxy = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"].some(key => Boolean(process.env[key]));
  // Same installed Pi networking implementation and precedence as its CLI.
  // Only this child process receives fallback environment values; no file is changed.
  transport.applyHttpProxySettings(networkSettings.httpProxy);
  transport.configureHttpDispatcher(timeout);
  await fixed("transport.json", {
    schema: "harness.native-transport/v1", implementation: "pi-http-dispatcher/v1",
    module_sha256: hash(await readFile(config.http_entry)),
    config_sha256: hash(configBytes),
    settings_sha256: settingsBytes ? hash(settingsBytes) : null,
    proxy_from_operator_settings: Boolean(networkSettings.httpProxy?.trim()),
    proxy_environment_already_present: envProxy,
    initialized: true
  });
  const settingsManager = sdk.SettingsManager.inMemory({
    compaction: { enabled: true }, retry: { enabled: true, maxRetries: 2 },
    cacheWarming: "off"
  });
  const resourceLoader = new sdk.DefaultResourceLoader({
    cwd: root, agentDir: config.agent_dir, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true,
    noContextFiles: true, noThemes: true
  });
  await resourceLoader.reload();
  const modelRuntime = await sdk.ModelRuntime.create({
    authPath: join(config.agent_dir, "auth.json"),
    modelsPath: join(config.agent_dir, "models.json"),
    allowModelNetwork: false, refreshOnCreate: false
  });
  reason = "model_catalog_invalid";
  // Restore the selected provider's existing local catalog in this same runtime.
  // Network catalog refresh and editing the operator configuration are not needed.
  const catalog = await modelRuntime.refresh({
    allowNetwork: false, providers: [selected.provider]
  });
  if (catalog.aborted || catalog.errors.size || modelRuntime.getError()) throw new Error();
  reason = "exact_model_unavailable";
  const model = modelRuntime.getModel(selected.provider, selected.model);
  if (!model || model.provider !== selected.provider || model.id !== selected.model) throw new Error();
  const resuming = attempt && config.session_file;
  const sessionManager = resuming
    ? sdk.SessionManager.open(config.session_file, join(root, "session"), root)
    : sdk.SessionManager.create(root, join(root, "session"), { id: config.native_session_id });
  if (resuming && (sessionManager.getSessionId() !== config.native_session_id ||
      sessionManager.getSessionFile() !== config.session_file)) throw new Error();
  ({ session } = await sdk.createAgentSession({
    cwd: root, agentDir: config.agent_dir, modelRuntime, model,
    thinkingLevel: selected.thinking, tools: selected.tools,
    resourceLoader, settingsManager, sessionManager
  }));
  reason = "constructed_session_selection_differs";
  if (session.model?.provider !== selected.provider ||
      session.model?.id !== selected.model ||
      session.thinkingLevel !== selected.thinking ||
      JSON.stringify([...session.getActiveToolNames()].sort()) !==
        JSON.stringify([...selected.tools].sort())) throw new Error();
  const proof = {
    selection: selected, runner_sha256: config.runner_sha256,
    config_sha256: hash(configBytes)
  };
  if (!resuming) {
    // Session identity is bound to the unchanged original assignment. A first
    // launch recovered before spawn also records its actual adapter separately.
    const originalBytes = attempt ? await readFile(join(root, "launch-config.json")) : configBytes;
    const original = JSON.parse(originalBytes);
    sessionManager.appendCustomEntry("harness.verification-compute/v1", {
      selection: original.selection, runner_sha256: original.runner_sha256,
      config_sha256: hash(originalBytes)
    });
  }
  if (attempt) sessionManager.appendCustomEntry("harness.verification-continuation/v1", { ...proof, attempt });
  await fixed("selection.json", { schema: "harness.compute-selection/v1", ...proof });
  // Distinct admission: never impersonate the ordinary Pi CLI process title.
  process.title = "harness-pi-v1";
  let modelFailure = false;
  session.subscribe(event => {
    const message = event.message;
    if (event.type === "message_end" && message?.role === "assistant" &&
        ["error", "aborted"].includes(message.stopReason)) modelFailure = true;
    process.stdout.write(JSON.stringify(event) + "\n");
  });
  const prompt = (attempt ? "Resume this SAME fixed assignment and Session after interruption. Inspect the original candidate and saved results first. Preserve the ACK and completed checks; submit only the outstanding verdict. This continuation is not human acceptance.\n" : "") +
    await readFile(join(root, "instructions.txt"), "utf8");
  reason = "native_model_interrupted";
  invoked = true;
  await session.prompt(prompt);
  if (modelFailure) { reason = "native_model_error"; throw new Error(); }
  await fixed("runner-result.json", { phase: "prompt_completed", model_invoked: true });
} catch {
  // Fixed classifications cannot accidentally publish SDK credential errors.
  await fixed("runner-result.json", {
    phase: invoked ? "model_interrupted" : "pre_model_failure",
    reason, model_invoked: invoked
  });
  process.exitCode = 2;
} finally { session?.dispose(); }
