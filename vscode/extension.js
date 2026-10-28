// TokenDiet VS Code extension: a thin UI over the bundled `tokendiet` Python package.
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");

const VERSION = require("./package.json").version;
let output, status, ctx;

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const p = cp.spawn(cmd, args, { ...opts, windowsHide: true });
    let out = "", err = "";
    p.stdout.on("data", (d) => { out += d; if (opts.log) output.append(d.toString()); });
    p.stderr.on("data", (d) => { err += d; if (opts.log) output.append(d.toString()); });
    p.on("error", reject);
    p.on("close", (code) => (code === 0 ? resolve(out) : reject(new Error(`${cmd} ${args.join(" ")} exited ${code}\n${err.slice(-2000)}`))));
  });
}

function folder() {
  const f = vscode.workspace.workspaceFolders;
  return f && f.length ? f[0].uri.fsPath : null;
}

async function findPython() {
  const configured = vscode.workspace.getConfiguration("tokendiet").get("pythonPath");
  const candidates = configured ? [[configured, []]] : process.platform === "win32"
    ? [["py", ["-3"]], ["python", []], ["python3", []]]
    : [["python3", []], ["python", []]];
  for (const [cmd, pre] of candidates) {
    try {
      const v = await run(cmd, [...pre, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"]);
      const [maj, min] = v.trim().split(".").map(Number);
      if (maj === 3 && min >= 10) return [cmd, pre];
    } catch (_) { /* try next */ }
  }
  throw new Error("Python 3.10+ not found. Install it, or set 'tokendiet.pythonPath'.");
}

// Private venv in VS Code's global storage, reinstalled when the extension version changes.
async function runtime() {
  const dir = ctx.globalStorageUri.fsPath;
  const venv = path.join(dir, "venv");
  const py = process.platform === "win32" ? path.join(venv, "Scripts", "python.exe") : path.join(venv, "bin", "python");
  const stamp = path.join(dir, "installed-version");
  if (fs.existsSync(py) && fs.existsSync(stamp) && fs.readFileSync(stamp, "utf8") === VERSION) return py;
  await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: "TokenDiet: setting up (one time)…" }, async (p) => {
    fs.mkdirSync(dir, { recursive: true });
    if (!fs.existsSync(py)) {
      p.report({ message: "creating Python environment" });
      const [cmd, pre] = await findPython();
      await run(cmd, [...pre, "-m", "venv", venv], { log: true });
    }
    p.report({ message: "installing tokendiet + tree-sitter grammars" });
    await run(py, ["-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", path.join(ctx.extensionPath, "python")], { log: true });
    fs.writeFileSync(stamp, VERSION);
  });
  return py;
}

async function td(args, log = false) {
  const py = await runtime();
  return run(py, ["-m", "tokendiet", ...args], { cwd: folder() || undefined, log, env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
}

function fmt(n) {
  n = n || 0;
  const a = Math.abs(n);
  return a >= 1e6 ? (n / 1e6).toFixed(2) + "M" : a >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
}

async function refreshStatus() {
  const f = folder();
  if (!f || !fs.existsSync(path.join(f, ".tokendiet", "enabled"))) {
    status.text = "$(circle-slash) TokenDiet: off";
    status.tooltip = "Click to enable TokenDiet for this workspace";
    status.command = "tokendiet.enable";
    return;
  }
  status.command = "tokendiet.report";
  try {
    const r = JSON.parse(await td(["report", f, "--format", "json", "--quick"]));
    status.text = r.calls ? `$(flame) TokenDiet ${fmt(r.context_tokens_avoided.conservative)} saved` : "$(flame) TokenDiet: on";
    status.tooltip = `${r.calls} tool calls · ${fmt(r.billed_tokens_avoided_est.conservative)} billed tokens avoided (conservative est.)\nClick for the full report`;
  } catch (e) {
    status.text = "$(flame) TokenDiet";
    status.tooltip = "Report unavailable: " + e.message;
  }
}

async function enable() {
  const f = folder();
  if (!f) return vscode.window.showWarningMessage("Open a folder first.");
  const cfg = vscode.workspace.getConfiguration("tokendiet");
  const args = ["init", f, "--vscode", "--budget", String(cfg.get("mapBudget"))];
  if (cfg.get("agents")) args.push("--agents", cfg.get("agents"));
  try {
    output.show(true);
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: "TokenDiet: indexing and wiring up agents…" },
      () => td(args, true));
    vscode.window.showInformationMessage("TokenDiet enabled. Start a new Claude Code / Codex / Copilot chat to pick it up.", "Show report")
      .then((c) => c && vscode.commands.executeCommand("tokendiet.report"));
  } catch (e) {
    vscode.window.showErrorMessage("TokenDiet: " + e.message);
  }
  refreshStatus();
}

async function disable() {
  const f = folder();
  if (!f) return;
  try {
    await td(["uninit", f], true);
    vscode.window.showInformationMessage("TokenDiet disabled. Index, usage log and notes are kept in .tokendiet/.");
  } catch (e) {
    vscode.window.showErrorMessage("TokenDiet: " + e.message);
  }
  refreshStatus();
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function reportHtml(r) {
  const c = r.context_tokens_avoided, b = r.billed_tokens_avoided_est, e = r.efficiency;
  const rows = Object.entries(r.by_tool).map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td>${v.calls}</td><td>${fmt(v.served)}</td><td>${fmt(v.lean)}</td><td>${fmt(v.full)}</td></tr>`).join("");
  const cs = r.claude_sessions;
  let sess = `<p class="muted">${esc(cs.reason || "")}</p>`;
  if (cs.available) {
    const keys = [["median_cost_input_equiv", "Cost (input-equivalent tokens)"], ["median_api_calls", "API calls"], ["median_peak_context", "Peak context"],
      ["median_exploration_tokens", "Exploration result tokens"], ["median_exploration_calls", "Exploration calls"], ["median_tokendiet_calls", "TokenDiet calls"]];
    const B = cs.before || {}, A = cs.after || {};
    sess = `<p class="muted">Before: ${B.sessions || 0} sessions · after: ${A.sessions || 0} sessions</p><table><tr><th>Median per session</th><th>Before</th><th>After</th><th>Change</th></tr>` +
      keys.map(([k, l]) => `<tr><td>${l}</td><td>${B[k] != null ? fmt(B[k]) : "–"}</td><td>${A[k] != null ? fmt(A[k]) : "–"}</td><td>${B[k] && A[k] != null ? ((A[k] / B[k] - 1) * 100).toFixed(0) + "%" : "–"}</td></tr>`).join("") + "</table>";
  }
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);padding:16px 24px;max-width:900px}
    .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:16px 0}
    .card{border:1px solid var(--vscode-panel-border);border-radius:6px;padding:12px}
    .big{font-size:26px;font-weight:600;margin:4px 0}.muted{opacity:.7;font-size:12px}
    table{border-collapse:collapse;width:100%;margin:8px 0 20px}td,th{border-bottom:1px solid var(--vscode-panel-border);padding:6px 8px;text-align:right}
    td:first-child,th:first-child{text-align:left}</style></head><body>
    <h2>TokenDiet · ${esc(r.repo.split(/[\\/]/).pop())}</h2>
    <div class="muted">Enabled since ${esc(r.enabled_at || "–")} · ${r.index.files} files, ${fmt(r.index.source_tokens)} source tokens indexed</div>
    <div class="cards">
      <div class="card"><div class="muted">Context tokens kept out</div><div class="big">${fmt(c.conservative)}</div><div class="muted">conservative · ${fmt(c.typical)} typical</div></div>
      <div class="card"><div class="muted">Billed tokens avoided (est.)</div><div class="big">${fmt(b.conservative)}</div><div class="muted">${esc(b.assumption)}</div></div>
      <div class="card"><div class="muted">1 token served replaces</div><div class="big">${e.conservative ?? "–"}×</div><div class="muted">conservative · ${e.typical ?? "–"}× typical</div></div>
      <div class="card"><div class="muted">Tool calls · sessions</div><div class="big">${r.calls} · ${r.sessions}</div><div class="muted">${r.notes.total} notes carried (${r.notes.stale} stale)</div></div>
    </div>
    <h3>By tool</h3><table><tr><th>Tool</th><th>Calls</th><th>Served</th><th>Conservative baseline</th><th>Typical baseline</th></tr>${rows}</table>
    <h3>Claude Code sessions in this folder, before vs after</h3>${sess}
    <p class="muted">Conservative = a careful agent (grep with line numbers + ranged reads). Typical = grep, then read whole files.
    Estimates were checked against measured grep/read output on 4 repos and run ~16% under the careful baseline, i.e. they under-count.</p>
    </body></html>`;
}

async function report() {
  const f = folder();
  if (!f) return;
  if (!fs.existsSync(path.join(f, ".tokendiet", "graph.db"))) return vscode.window.showInformationMessage("TokenDiet is not enabled here.", "Enable").then((c) => c && enable());
  try {
    const r = JSON.parse(await td(["report", f, "--format", "json"]));
    const panel = vscode.window.createWebviewPanel("tokendietReport", "TokenDiet report", vscode.ViewColumn.Active, {});
    panel.webview.html = reportHtml(r);
  } catch (e) {
    vscode.window.showErrorMessage("TokenDiet: " + e.message);
  }
}

function activate(context) {
  ctx = context;
  output = vscode.window.createOutputChannel("TokenDiet");
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.show();
  context.subscriptions.push(output, status,
    vscode.commands.registerCommand("tokendiet.enable", enable),
    vscode.commands.registerCommand("tokendiet.disable", disable),
    vscode.commands.registerCommand("tokendiet.report", report),
    vscode.commands.registerCommand("tokendiet.reindex", async () => {
      try { await td(["index", folder()], true); vscode.window.showInformationMessage("TokenDiet: index rebuilt."); }
      catch (e) { vscode.window.showErrorMessage("TokenDiet: " + e.message); }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(refreshStatus));
  // after an extension update, rewrite this workspace's agent files with the new version's templates
  const f = folder();
  const synced = f && fs.existsSync(path.join(f, ".tokendiet", "enabled"))
    ? td(["sync", f], true).catch((e) => output.appendLine("sync failed: " + e.message)) : Promise.resolve();
  synced.then(refreshStatus);
  const timer = setInterval(refreshStatus, 5 * 60 * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

function deactivate() {}

module.exports = { activate, deactivate };
