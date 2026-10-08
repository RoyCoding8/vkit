import "/assets/material.js";

const $ = (id) => document.getElementById(id);
const pages = {
  overview: [
    "Overview",
    "dashboard",
    "YOUR WORKSPACE",
    "Verification at a glance",
    "What is known about your code, and where evidence is still needed.",
  ],
  checks: [
    "Checks",
    "checks",
    "REGISTERED VERIFICATION",
    "Checks",
    "Inspect each check, its inputs, and the scope of its evidence.",
  ],
  runs: [
    "Runs",
    "history",
    "EXECUTION HISTORY",
    "Runs",
    "Recorded outcomes, observations, measurements, and logs.",
  ],
  capabilities: [
    "Capabilities",
    "extension",
    "THE TOOL",
    "What vkit can verify",
    "Reusable verification methods, with explicit evidence and limits.",
  ],
  features: [
    "Feature map",
    "features",
    "APPLICATION COVERAGE",
    "Feature map",
    "What the application does, how to reach it, and which checks cover it.",
  ],
  proposals: [
    "Proposals",
    "proposals",
    "PENDING REVIEW",
    "Proposals",
    "Suggested checks, feature entries, and new files. Nothing is applied here.",
  ],
  setup: [
    "Setup & tools",
    "settings",
    "PROJECT CONFIGURATION",
    "Setup & tools",
    "Project configuration, tool availability, and connection details.",
  ],
};
const methods = {
  scenario: [
    "Scenarios",
    "checks",
    "Run a reusable driver against named behaviors and collect its observations and measurements.",
    "Evidence covers the cases actually run. Performance budgets apply when declared.",
  ],
  pytest: [
    "Python tests",
    "code",
    "Execute the required pytest tests and record each test’s outcome.",
    "Required tests must run. Missing or skipped tests cannot establish a pass.",
  ],
  node_test: [
    "Node tests",
    "code",
    "Execute named Node tests and interpret the runner’s structured report.",
    "Evidence is limited to the required tests and inputs exercised.",
  ],
  property: [
    "Property tests",
    "science",
    "Search generated cases with Hypothesis for disagreement with an expected property or reference model.",
    "Sampling can find counterexamples; it does not prove the property for every input.",
  ],
  static: [
    "Static analysis",
    "rule",
    "Read analyzer findings and compare them with an accepted baseline.",
    "A pass means no new reported findings, not an absence of all possible defects.",
  ],
  lean: [
    "Lean proofs",
    "security",
    "Check formal statements with Lean and audit axioms. Agent-written proofs use comparator against frozen statements.",
    "The result applies to the formal statements. It needs a separate connection to application behavior.",
  ],
  tlc: [
    "Finite model checking",
    "features",
    "Explore a finite TLA+ model with TLC and check the declared properties.",
    "The result applies to that model and its recorded bounds. It does not verify the implementation.",
  ],
};
const labels = {
  fresh_pass: ["Passed", "success"],
  fresh_fail: ["Failed", "error"],
  fresh_blocked: ["Blocked", "warning"],
  stale: ["Stale", "warning"],
  missing: ["Not run", ""],
  not_approved: ["Not accepted", "warning"],
  running: ["Running", "info"],
  starting: ["Starting", "info"],
  done: ["Finished", ""],
  interrupted: ["Interrupted", "warning"],
  PASS: ["Pass", "success"],
  FAIL: ["Fail", "error"],
  BLOCKED: ["Blocked", "warning"],
  READY: ["Ready", "success"],
  REJECTED: ["Rejected", "error"],
  verified: ["Verified", "success"],
  unverified: ["Unverified", "warning"],
};
let report,
  runs = [],
  configuration,
  signature = "",
  refreshing,
  selectedRun = null,
  stream = "stdout";
let search = "",
  kind = "",
  activePage = "";

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function icon(name, cls = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("viewBox", "0 0 24 24");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", `/assets/icons.svg#${name}`);
  svg.append(use);
  return svg;
}
function badge(state) {
  const [label, tone] = labels[state] || [state || "No result", ""];
  return el("span", label, `status-badge ${tone}`);
}
function link(text, action) {
  const button = el("button", text, "text-link");
  button.type = "button";
  button.onclick = action;
  return button;
}
function action(text, page, type = "md-text-button") {
  const button = el(type, text);
  button.onclick = () => {
    location.hash = page;
  };
  return button;
}
function panel(title, description, children, button) {
  const box = el("section", null, "panel"),
    header = el("div", null, "panel-header"),
    copy = el("div");
  copy.append(el("h2", title));
  if (description) copy.append(el("p", description));
  header.append(copy);
  if (button) header.append(button);
  box.append(header, ...children);
  return box;
}
function empty(title, description, symbol = "info") {
  const box = el("div", null, "empty-state");
  box.append(icon(symbol), el("h3", title), el("p", description));
  return box;
}
function note(title, text) {
  const box = el("div", null, "scope-note");
  box.append(el("strong", title), el("p", text));
  return box;
}
function date(value) {
  return value
    ? new Date(value).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Not recorded";
}
function row(cells) {
  const tr = el("tr");
  for (const value of cells) {
    const td = el("td");
    td.append(
      value instanceof Node ? value : document.createTextNode(value ?? ""),
    );
    tr.append(td);
  }
  return tr;
}
function table(headers, name) {
  const wrap = el("div", null, "panel table-scroll"),
    grid = el("table"),
    head = el("thead"),
    hr = el("tr"),
    body = el("tbody");
  grid.setAttribute("aria-label", name);
  for (const name of headers) {
    const th = el("th", name);
    th.scope = "col";
    hr.append(th);
  }
  head.append(hr);
  grid.append(head, body);
  wrap.append(grid);
  return { wrap, body };
}
function toolbar(placeholder, update, withKind = false) {
  const bar = el("div", null, "toolbar"),
    box = el("label", null, "search-box"),
    input = el("input");
  input.type = "search";
  input.id = "view-search";
  input.placeholder = placeholder;
  input.value = search;
  input.setAttribute("aria-label", placeholder);
  input.oninput = () => {
    search = input.value;
    update();
  };
  box.append(icon("search"), input);
  bar.append(box);
  if (withKind) {
    const select = el("select");
    select.setAttribute("aria-label", "Filter by verification method");
    const all = el("option", "All methods");
    all.value = "";
    select.append(all);
    for (const [key, value] of Object.entries(methods)) {
      const option = el("option", value[0]);
      option.value = key;
      select.append(option);
    }
    select.value = kind;
    select.onchange = () => {
      kind = select.value;
      update();
    };
    bar.append(select);
  }
  return bar;
}
function matches(value) {
  return JSON.stringify(value).toLowerCase().includes(search.toLowerCase());
}
function checkState(check) {
  return check.running.length ? "running" : check.state;
}
function checkRow(check) {
  const box = el("div", null, "check-row"),
    symbol = el("div", null, "kind-icon"),
    copy = el("div", null, "row-copy");
  symbol.append(icon(methods[check.kind]?.[1] || "checks"));
  copy.append(
    link(check.id, () => showCheck(check.id)),
    el(
      "p",
      `${methods[check.kind]?.[0] || check.kind} · ${check.inputs.scope}`,
    ),
  );
  box.append(symbol, copy, badge(checkState(check)));
  return box;
}
function activityRow(run) {
  const box = el("div", null, "activity-row"),
    copy = el("div", null, "row-copy");
  copy.append(
    link(run.check_id, () => {
      location.hash = `runs/${encodeURIComponent(run.run_id)}`;
    }),
    el("p", date(run.ended_at || run.started_at)),
  );
  box.append(copy, badge(run.result || run.state));
  return box;
}
function overview() {
  const fragment = document.createDocumentFragment(),
    banner = el("section", null, "verdict-banner");
  banner.dataset.verdict = report.gate.verdict;
  const symbol = el("div", null, "verdict-icon");
  symbol.append(
    icon(
      report.gate.verdict === "READY"
        ? "check"
        : report.gate.verdict === "REJECTED"
          ? "error"
          : "schedule",
    ),
  );
  const copy = el("div", null, "verdict-copy");
  const titles = {
    READY: "Current evidence is ready",
    REJECTED: "A current check failed",
    BLOCKED: "Evidence is incomplete",
  };
  copy.append(
    el("h2", titles[report.gate.verdict]),
    el("p", report.manifest_error || report.gate.reason),
  );
  banner.append(
    symbol,
    copy,
    action("Inspect checks", "checks", "md-filled-tonal-button"),
  );
  fragment.append(banner);
  const metrics = el("div", null, "metrics");
  const passed = report.checks.filter((c) => c.state === "fresh_pass").length;
  const running = new Set(report.checks.flatMap((c) => c.running)).size;
  for (const [value, label, description] of [
    [report.checks.length, "Registered checks", "Defined for this workspace"],
    [passed, "Current passes", "Against the current inputs"],
    [running, "Active runs", "Visible in recent history"],
    [
      report.features.filter((f) => f.verified).length,
      "Verified features",
      `${report.features.length} mapped features`,
    ],
  ]) {
    const metric = el("div", null, "metric");
    metric.append(
      el("span", value, "metric-value"),
      el("span", label, "metric-label"),
      el("span", description, "metric-note"),
    );
    metrics.append(metric);
  }
  fragment.append(metrics);
  const grid = el("div", null, "overview-grid"),
    left = el("div", null, "stack"),
    right = el("div", null, "stack");
  const attention = report.checks.filter((c) => c.state !== "fresh_pass");
  left.append(
    panel(
      "Evidence to review",
      `${attention.length} of ${report.checks.length} checks lack a current pass`,
      attention.length
        ? attention.slice(0, 6).map(checkRow)
        : [
            empty(
              report.checks.length
                ? "Every check has current evidence"
                : "No checks registered",
              report.checks.length
                ? "Inspect individual checks to see what each pass establishes."
                : "Register reusable checks in verification/manifest.json.",
              "check",
            ),
          ],
      action("All checks", "checks"),
    ),
  );
  left.append(
    panel(
      "Recent activity",
      "Latest recorded executions",
      runs.length
        ? runs.slice(0, 4).map(activityRow)
        : [
            empty(
              "No recorded runs",
              "Run history will show outcomes, observations, and logs once a check executes.",
              "history",
            ),
          ],
      action("Run history", "runs"),
    ),
  );
  const body = el("div", null, "panel-body"),
    steps = el("ol", null, "steps");
  [
    [
      "Define",
      "A check names its inputs, procedure, and required observations.",
    ],
    ["Execute", "Vkit handles the registered procedure and reads its report."],
    ["Record", "Results retain their inputs, scope, measurements, and logs."],
    ["Check freshness", "Relevant file changes make earlier evidence stale."],
  ].forEach(([title, text], i) => {
    const step = el("li"),
      textBox = el("div");
    textBox.append(el("h3", title), el("p", text));
    step.append(el("span", i + 1, "step-number"), textBox);
    steps.append(step);
  });
  body.append(
    steps,
    note(
      "What “ready” means",
      "Every registered check passed against its current inputs. Features without checks and behavior outside those requirements remain unverified.",
    ),
  );
  right.append(
    panel("How verification works", "Reusable procedures. Recorded evidence.", [
      body,
    ]),
  );
  const methodsBody = el("div", null, "panel-body");
  methodsBody.append(
    el(
      "p",
      `${new Set(report.checks.map((c) => c.kind)).size} of ${Object.keys(methods).length} verification methods are configured in this workspace.`,
      "small quiet",
    ),
    action("Explore capabilities", "capabilities"),
  );
  right.append(
    panel(
      "Verification methods",
      "From application scenarios to formal statements",
      [methodsBody],
    ),
  );
  grid.append(left, right);
  fragment.append(grid);
  return fragment;
}
function checks() {
  const fragment = document.createDocumentFragment(),
    grid = table(
      ["CHECK", "STATE", "METHOD", "INPUT SCOPE", "LAST RUN"],
      "checks",
    );
  function update() {
    const shown = report.checks.filter(
      (c) => matches(c) && (!kind || c.kind === kind),
    );
    grid.body.replaceChildren(
      ...shown.map((c) => {
        const copy = el("div");
        copy.append(
          link(c.id, () => showCheck(c.id)),
          el("p", c.description),
        );
        const last = c.last
          ? link(`${c.last.result} · ${date(c.last.ended_at)}`, () => {
              location.hash = `runs/${encodeURIComponent(c.last.run_id)}`;
            })
          : "Not run";
        return row([
          copy,
          badge(checkState(c)),
          methods[c.kind]?.[0] || c.kind,
          c.inputs.scope,
          last,
        ]);
      }),
    );
    if (!shown.length) {
      const tr = el("tr"),
        td = el("td");
      td.colSpan = 5;
      td.append(
        empty(
          "No matching checks",
          report.checks.length
            ? "Try another search or verification method."
            : "This workspace has no registered checks.",
          "checks",
        ),
      );
      tr.append(td);
      grid.body.append(tr);
    }
  }
  fragment.append(toolbar("Search checks", update, true), grid.wrap);
  update();
  return fragment;
}
function runHistory() {
  const fragment = document.createDocumentFragment(),
    grid = table(["CHECK / RUN", "RESULT", "STARTED", "WORKTREE"], "runs");
  function update() {
    const shown = runs.filter(matches);
    grid.body.replaceChildren(
      ...shown.map((r) => {
        const copy = el("div");
        copy.append(
          link(r.check_id, () => {
            location.hash = `runs/${encodeURIComponent(r.run_id)}`;
          }),
          el("p", r.run_id),
        );
        return row([
          copy,
          badge(r.result || r.state),
          date(r.started_at),
          r.worktree,
        ]);
      }),
    );
    if (!shown.length) {
      const tr = el("tr"),
        td = el("td");
      td.colSpan = 4;
      td.append(
        empty(
          "No matching runs",
          runs.length
            ? "Try another search."
            : "Completed and active runs appear here when checks execute.",
          "history",
        ),
      );
      tr.append(td);
      grid.body.append(tr);
    }
  }
  const detail = el("section", null, "panel run-selected");
  detail.id = "run-detail";
  detail.hidden = !selectedRun;
  fragment.append(toolbar("Search recent runs", update), grid.wrap, detail);
  update();
  return fragment;
}
function capabilities() {
  const fragment = document.createDocumentFragment(),
    list = el("section", null, "panel");
  for (const [key, [title, symbol, text, limit]] of Object.entries(methods)) {
    const entry = el("div", null, "capability"),
      visual = el("div", null, "kind-icon"),
      copy = el("div");
    visual.append(icon(symbol));
    copy.append(el("h3", title), el("p", text), el("p", limit, "limit"));
    const count = report.checks.filter((c) => c.kind === key).length;
    entry.append(
      visual,
      copy,
      el(
        "span",
        count ? `${count} configured` : "Not configured",
        "capability-count",
      ),
    );
    list.append(entry);
  }
  fragment.append(
    list,
    el("h2", "Functions available to agents", "section-label"),
  );
  const tools = el("div", null, "tool-grid");
  for (const [name, description] of [
    ["status", "Read current evidence and identify stale or missing checks."],
    [
      "check_run",
      "Execute registered checks; vkit owns the internal procedure.",
    ],
    ["run_get", "Inspect one execution, its outcome, and its logs."],
    ["run_cancel", "Stop an execution and its child processes."],
    ["features", "Find application behavior, reach steps, and coverage gaps."],
    ["gate", "Read the combined verdict for the registered checks."],
    [
      "propose",
      "Submit suggested checks, feature entries, and new files for review.",
    ],
  ]) {
    const item = el("div", null, "tool-item");
    item.append(el("code", name), el("p", description));
    tools.append(item);
  }
  fragment.append(
    panel(
      "A callable verification tool",
      "Agents choose when to call these functions. Each function returns a scoped result.",
      [tools],
    ),
  );
  return fragment;
}
function featureMap() {
  const fragment = document.createDocumentFragment(),
    list = el("section", null, "panel");
  function update() {
    const opened = new Set(
      [...list.querySelectorAll("details[open]")].map((d) => d.dataset.feature),
    );
    const shown = report.features.filter(matches);
    list.replaceChildren(
      ...shown.map((f) => {
        const entry = el("details", null, "feature-row");
        entry.dataset.feature = f.id;
        entry.open = opened.has(f.id);
        const summary = el("summary"),
          copy = el("div", null, "row-copy");
        copy.append(el("h3", f.id), el("p", f.behavior));
        summary.append(
          copy,
          badge(f.verified ? "verified" : "unverified"),
          icon("expand"),
        );
        const content = el("div", null, "feature-content"),
          grid = el("div", null, "detail-grid");
        for (const [title, lines] of [
          ["How to reach it", f.how_to_reach],
          ["Entry points", f.entry_points],
        ]) {
          const section = el("div", null, "detail-section"),
            ul = el("ul");
          for (const text of lines) ul.append(el("li", text));
          section.append(
            el("h3", title),
            lines.length ? ul : el("p", "Not documented", "small quiet"),
          );
          grid.append(section);
        }
        content.append(grid);
        const coverage = el("div", null, "dialog-section"),
          chips = el("div", null, "check-chips");
        for (const c of f.checks) {
          const button = link(
            `${c.id} · ${labels[c.state]?.[0] || c.state}`,
            () => showCheck(c.id),
          );
          chips.append(button);
        }
        coverage.append(
          el("h3", "Covering checks"),
          f.checks.length
            ? chips
            : el("p", "No check covers this feature.", "small quiet"),
        );
        content.append(coverage);
        if (f.gaps.length || f.problems.length)
          content.append(
            note("Known gaps", [...f.gaps, ...f.problems].join(" · ")),
          );
        entry.append(summary, content);
        return entry;
      }),
    );
    if (!shown.length)
      list.append(
        empty(
          "No matching features",
          report.feature_error ||
            (report.features.length
              ? "Try another search."
              : "Describe application behavior and covering checks in verification/features.json."),
          "features",
        ),
      );
  }
  fragment.append(toolbar("Search features", update), list);
  update();
  return fragment;
}
function command(text) {
  const box = el("div", null, "command-line"),
    button = el("md-icon-button");
  button.setAttribute("aria-label", "Copy command");
  button.title = "Copy command";
  button.append(icon("copy"));
  button.onclick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.setAttribute("aria-label", "Copied");
      button.replaceChildren(icon("check"));
    } catch {
      button.setAttribute(
        "aria-label",
        "Copy unavailable; select the command text",
      );
      button.title = "Select and copy the command text";
    }
  };
  box.append(el("code", text), button);
  return box;
}
function proposals() {
  const list = el("section", null, "panel");
  for (const p of report.proposals) {
    const entry = el("article", null, "proposal");
    entry.append(
      el("code", p.digest.slice(0, 12), "quiet"),
      el("h3", p.rationale),
    );
    const changes = el("div", null, "check-chips");
    for (const [label, values] of [
      ["Checks", p.checks],
      ["Features", p.features],
      ["New files", p.files],
    ])
      if (values.length)
        changes.append(
          el("span", `${label}: ${values.join(", ")}`, "small quiet"),
        );
    entry.append(changes, command(p.accept));
    list.append(entry);
  }
  if (!report.proposals.length)
    list.append(
      empty(
        "No proposals waiting for review",
        "Agents can suggest reusable checks and verification rules. Suggested changes appear here before they are accepted.",
        "proposals",
      ),
    );
  return list;
}
function setup() {
  const fragment = document.createDocumentFragment(),
    grid = el("div", null, "overview-grid"),
    project = el("div", null, "panel-body"),
    definitions = el("dl", null, "definition-list");
  for (const [name, value] of [
    ["Repository", report.project],
    ["Evidence storage", configuration.doctor.state_root],
    ["Check definitions", "verification/manifest.json"],
    ["Feature map", "verification/features.json"],
    ["Version", configuration.version],
  ]) {
    const pair = el("div");
    pair.append(el("dt", name), el("dd", value));
    definitions.append(pair);
  }
  project.append(
    definitions,
    note(
      "Input identity",
      "Evidence depends on check definitions, input contents, and any pinned baseline. Undeclared dependencies can leave evidence apparently fresh.",
    ),
  );
  const prerequisites = el("div", null, "panel-body"),
    tools = new Map();
  for (const f of configuration.doctor.findings) {
    const key = f.prerequisite || f.check;
    if (!tools.has(key)) tools.set(key, { ...f, checks: [] });
    tools.get(key).checks.push(f.check);
  }
  for (const [name, item] of tools) {
    const status = el("div", null, "tool-status"),
      copy = el("div", null, "row-copy");
    copy.append(el("h3", name), el("p", item.detail));
    status.append(
      icon(item.ok ? "check" : "error"),
      copy,
      el(
        "span",
        item.ok ? "Available" : "Missing",
        `status-badge ${item.ok ? "success" : "warning"}`,
      ),
    );
    prerequisites.append(status);
  }
  if (!tools.size)
    prerequisites.append(
      el(
        "p",
        "No prerequisites are declared by the registered checks.",
        "small quiet",
      ),
    );
  grid.append(
    panel("Project", configuration.description, [project]),
    panel(
      "Required tools",
      "Availability in the console server’s environment",
      [prerequisites],
    ),
  );
  const connection = el("div", null, "panel-body");
  connection.append(
    command(`vkit mcp serve --project "${report.project}"`),
    note(
      "Independent interfaces",
      "The MCP server exposes callable functions. The Claude plugin separately bundles guidance skills and session hooks. This console displays evidence without executing or accepting checks.",
    ),
  );
  fragment.append(
    grid,
    el("h2", "Connect an agent", "section-label"),
    panel(
      "MCP over stdio",
      "Use this command in your agent’s MCP configuration.",
      [connection],
    ),
  );
  return fragment;
}
function showCheck(id) {
  const check = report.checks.find((c) => c.id === id);
  if (!check) return;
  $("detail-title").textContent = id;
  $("detail-label").textContent =
    methods[check.kind]?.[0].toUpperCase() || "CHECK";
  const body = $("detail-body");
  body.replaceChildren(
    badge(checkState(check)),
    el(
      "p",
      check.description || "No description provided.",
      "dialog-description",
    ),
  );
  for (const [title, text] of [
    ["Establishes", check.establishes],
    ["Does not establish", check.does_not_establish],
    [
      "Inputs",
      check.inputs.declared.join(", ") || "Whole eligible repository tree",
    ],
  ]) {
    const section = el("section", null, "dialog-section");
    section.append(el("h3", title), el("p", text));
    body.append(section);
  }
  if (check.inputs.unmatched.length)
    body.append(
      note("Missing declared inputs", check.inputs.unmatched.join(", ")),
    );
  if (check.last)
    body.append(
      action(
        "Inspect last run",
        `runs/${encodeURIComponent(check.last.run_id)}`,
        "md-outlined-button",
      ),
    );
  const definition = configuration.definitions[id];
  if (definition) {
    const section = el("section", null, "dialog-section");
    section.append(
      el("h3", "Registered definition"),
      el("pre", JSON.stringify(definition, null, 2)),
    );
    body.append(section);
  }
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
}
async function refreshRun() {
  if (!selectedRun || activePage !== "runs") return;
  const id = selectedRun,
    requestedStream = stream;
  const detail = await get(
    `/api/run?id=${encodeURIComponent(id)}&log=${requestedStream}&offset=-16000`,
  );
  if (id !== selectedRun || requestedStream !== stream || activePage !== "runs")
    return;
  const box = $("run-detail");
  if (!box) return;
  const detailSignature = JSON.stringify(detail);
  if (box.dataset.signature === detailSignature) return;
  const logWasFocused = document.activeElement?.id === "run-log";
  box.dataset.signature = detailSignature;
  box.hidden = false;
  box.replaceChildren();
  const header = el("div", null, "panel-header"),
    title = el("div");
  title.append(el("h2", detail.check_id), el("p", id));
  header.append(title, badge(detail.outcome?.result || detail.state));
  box.append(header);
  const body = el("div", null, "panel-body"),
    meta = el("div", null, "run-meta");
  meta.append(
    el("span", `Started ${date(detail.started_at)}`),
    el("span", `Finished ${date(detail.ended_at)}`),
    el("span", `${detail.inputs?.scope || ""}`),
  );
  body.append(meta);
  if (detail.outcome?.detail)
    body.append(
      note(
        detail.outcome.reason?.replaceAll("_", " ") || "Outcome",
        detail.outcome.detail,
      ),
    );
  for (const scenario of detail.outcome?.scenarios || []) {
    const entry = el("div", null, "result-row"),
      copy = el("div", null, "row-copy");
    copy.append(el("h3", scenario.id), el("p", scenario.observation));
    entry.append(badge(scenario.result), copy);
    body.append(entry);
  }
  if (detail.measurements?.length) {
    const measurements = el("div", null, "measurement-grid");
    for (const m of detail.measurements) {
      const item = el("div");
      item.append(
        el("span", m.name, "small quiet"),
        el("strong", `${m.value} ${m.unit}`),
        el(
          "small",
          `${m.better} is better${m.baseline != null ? ` · baseline ${m.baseline}` : ""}`,
        ),
      );
      measurements.append(item);
    }
    body.append(measurements);
  }
  for (const finding of detail.findings || [])
    body.append(
      note(
        `${finding.rule} · ${finding.path}${finding.line ? ":" + finding.line : ""}`,
        finding.message,
      ),
    );
  for (const text of detail.unchecked_budgets || [])
    body.append(note("Budget not checked", text));
  if (detail.obligations) {
    const obligations = el("section", null, "dialog-section");
    obligations.append(el("h3", "Formal obligations"));
    obligations.append(el("pre", JSON.stringify(detail.obligations, null, 2)));
    body.append(obligations);
  }
  const provenance = el("details", null, "dialog-section");
  provenance.append(el("summary", "Recorded inputs and execution"));
  provenance.append(
    el(
      "pre",
      JSON.stringify(
        {
          category: detail.category,
          worktree: detail.worktree,
          check_digest: detail.check_digest,
          evidence_key: detail.key,
          inputs: detail.inputs,
          command: detail.argv,
          exit_code: detail.exit_code,
          environment: detail.environment,
        },
        null,
        2,
      ),
    ),
  );
  body.append(provenance);
  const logHeader = el("div", null, "log-header"),
    select = el("select");
  select.id = "run-log";
  select.setAttribute("aria-label", "Log stream");
  for (const name of ["stdout", "stderr"]) {
    const option = el("option", name);
    option.value = name;
    select.append(option);
  }
  select.value = stream;
  select.onchange = () => {
    stream = select.value;
    refreshRun().catch(showError);
  };
  logHeader.append(el("h3", "Execution log"), select);
  const log = el("pre", detail.log?.text || "No output in this stream.");
  log.id = "run-text";
  body.append(logHeader, log);
  if (detail.log?.size > 16000)
    body.append(el("p", "Showing the last 16,000 bytes.", "small quiet"));
  box.append(body);
  if (logWasFocused) select.focus();
}
function render() {
  const focused = document.activeElement,
    position = focused?.selectionStart;
  const openedFeatures = [...$("view").querySelectorAll("details[open]")].map(
    (d) => d.dataset.feature,
  );
  const [, , eyebrow, title, description] = pages[activePage];
  $("breadcrumb").textContent = pages[activePage][0];
  $("page-eyebrow").textContent = eyebrow;
  $("page-title").textContent = title;
  $("page-description").textContent = description;
  for (const nav of $("navigation").querySelectorAll("a")) {
    if (nav.dataset.page === activePage)
      nav.setAttribute("aria-current", "page");
    else nav.removeAttribute("aria-current");
  }
  const counts = {
    checks: report.checks.length,
    runs: runs.length,
    features: report.features.length,
    proposals: report.proposals.length,
  };
  for (const node of $("navigation").querySelectorAll(".nav-count"))
    node.textContent = counts[node.dataset.page] || "";
  $("view").replaceChildren(
    {
      overview,
      checks,
      runs: runHistory,
      capabilities,
      features: featureMap,
      proposals,
      setup,
    }[activePage](),
  );
  for (const details of $("view").querySelectorAll("details"))
    details.open = openedFeatures.includes(details.dataset.feature);
  if (focused?.id === "view-search") {
    const input = $("view-search");
    input?.focus();
    if (position != null) input?.setSelectionRange(position, position);
  }
}
function route() {
  const [requested, runId] = location.hash.slice(1).split("/");
  const next = Object.hasOwn(pages, requested) ? requested : "overview";
  if (next !== activePage) {
    search = "";
    kind = "";
  }
  activePage = next;
  selectedRun = next === "runs" && runId ? decodeURIComponent(runId) : null;
  $("detail-dialog").close();
  closeNavigation();
  if (report) {
    render();
    refreshRun().catch(showError);
  }
}
async function get(url) {
  const response = await fetch(url);
  const body = await response.json();
  if (!response.ok)
    throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
function showError(error) {
  $("connection-error").hidden = false;
  $("connection-error").textContent =
    `Unable to refresh: ${error.message}. Showing the last received evidence.`;
  document.body.classList.add("disconnected");
}
function refresh() {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      const [nextReport, nextRuns, nextConfig] = await Promise.all([
        get("/api/status"),
        get("/api/runs?limit=50"),
        get("/api/config"),
      ]);
      report = nextReport;
      runs = nextRuns;
      configuration = nextConfig;
      const nextSignature = JSON.stringify([report, runs, configuration]);
      $("workspace-name").textContent = report.project
        .replaceAll("\\", "/")
        .split("/")
        .pop();
      $("workspace-name").title = report.project;
      $("project-path").textContent = report.project;
      $("version").textContent = `v${configuration.version} · Read-only`;
      $("updated").textContent =
        `Updated ${new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
      if (signature !== nextSignature) {
        signature = nextSignature;
        render();
      }
      await refreshRun();
      $("connection-error").hidden = true;
      document.body.classList.remove("disconnected");
    } catch (error) {
      showError(error);
      if (!report)
        $("view").replaceChildren(
          empty("Workspace unavailable", error.message),
        );
    } finally {
      $("content").setAttribute("aria-busy", "false");
      refreshing = null;
    }
  })();
  return refreshing;
}
function stored(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function save(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {}
}
const systemTheme = matchMedia("(prefers-color-scheme: dark)");
function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  const next = mode === "dark" ? "light" : "dark";
  $("theme-icon").setAttribute("href", `/assets/icons.svg#${next}`);
  $("theme-toggle").setAttribute("aria-label", `Switch to ${next} theme`);
  $("theme-toggle").title = `Switch to ${next} theme`;
}
applyTheme(stored("vkit-theme") || (systemTheme.matches ? "dark" : "light"));
systemTheme.addEventListener("change", (event) => {
  if (!stored("vkit-theme")) applyTheme(event.matches ? "dark" : "light");
});
$("theme-toggle").onclick = () => {
  const next =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  save("vkit-theme", next);
  applyTheme(next);
};
function closeNavigation() {
  document.body.classList.remove("nav-open");
  document.querySelector(".workspace-main").inert = false;
  $("scrim").hidden = true;
  $("mobile-nav").setAttribute("aria-expanded", "false");
}
$("mobile-nav").onclick = () => {
  document.body.classList.add("nav-open");
  document.querySelector(".workspace-main").inert = true;
  $("scrim").hidden = false;
  $("mobile-nav").setAttribute("aria-expanded", "true");
  $("navigation").querySelector("a").focus();
};
$("scrim").onclick = () => {
  closeNavigation();
  $("mobile-nav").focus();
};
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("nav-open")) {
    closeNavigation();
    $("mobile-nav").focus();
  }
});
function collapse(collapsed) {
  document.body.classList.toggle("nav-collapsed", collapsed);
  $("collapse-nav").setAttribute("aria-expanded", String(!collapsed));
  $("collapse-nav").setAttribute(
    "aria-label",
    collapsed ? "Expand sidebar" : "Collapse sidebar",
  );
}
collapse(stored("vkit-nav-collapsed") === "true");
$("collapse-nav").onclick = () => {
  const collapsed = !document.body.classList.contains("nav-collapsed");
  save("vkit-nav-collapsed", String(collapsed));
  collapse(collapsed);
};
$("close-detail").onclick = () => $("detail-dialog").close();
$("detail-dialog").addEventListener("click", (event) => {
  if (event.target === $("detail-dialog")) {
    const rect = event.target.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      event.target.close();
  }
});
$("refresh").onclick = () => refresh();
for (const [id, [name, symbol]] of Object.entries(pages)) {
  if (id === "capabilities" || id === "setup")
    $("navigation").append(el("div", null, "nav-divider"));
  const entry = el("a", null, "nav-item");
  entry.href = `#${id}`;
  entry.dataset.page = id;
  entry.title = name;
  entry.setAttribute("aria-label", name);
  entry.append(icon(symbol), el("span", name, "nav-label"));
  const count = el("span", "", "nav-count");
  count.dataset.page = id;
  entry.append(count);
  $("navigation").append(entry);
}
window.addEventListener("hashchange", route);
route();
async function poll() {
  await refresh();
  setTimeout(poll, 2000);
}
poll();
