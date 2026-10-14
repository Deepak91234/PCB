/**
 * PCBAgent for Pi.
 *
 * One request becomes a checked board. The model writes a spec, a circuit and
 * placement constraints as JSON through typed tools; the Python runners decide
 * whether the design is true: ngspice verifies it across tolerance corners, a
 * local JLCPCB catalog sources it, and a placer, Freerouting and KiCad's DRC
 * lay it out. After `/design <request>` the run keeps going on its own until the
 * board passes DRC, the attempt budget runs out, or you press Escape.
 *
 * Privacy: every tool runs locally. Start Pi with
 *   PI_OFFLINE=1 PI_TELEMETRY=0 PI_SKIP_VERSION_CHECK=1
 * and point it at a local model (this extension registers local Ollama models).
 */

import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

// ---------------------------------------------------------------------------
// Locating the Python side
// ---------------------------------------------------------------------------

const HERE = (() => {
	try {
		return dirname(fileURLToPath(import.meta.url));
	} catch {
		return __dirname;
	}
})();
const PKG_ROOT = resolve(HERE, "..", "..");
const SRC_DIR = join(PKG_ROOT, "src");

function loadSchema(name: string): Record<string, unknown> {
	const file = join(PKG_ROOT, "schemas", name === "prompts" ? "prompts.json" : `${name}.schema.json`);
	return JSON.parse(readFileSync(file, "utf8"));
}

interface CliResult {
	code: number;
	json: any;
	stdout: string;
	stderr: string;
}

function pythonCommand(): { cmd: string; args: string[]; env: NodeJS.ProcessEnv } {
	const env = { ...process.env };
	if (process.env.PCBAGENT_CMD) {
		const parts = process.env.PCBAGENT_CMD.split(" ").filter(Boolean);
		return { cmd: parts[0], args: parts.slice(1), env };
	}
	if (existsSync(join(SRC_DIR, "pcbagent", "__init__.py"))) {
		env.PYTHONPATH = env.PYTHONPATH ? `${SRC_DIR}:${env.PYTHONPATH}` : SRC_DIR;
	}
	return { cmd: process.env.PCBAGENT_PYTHON || "python3", args: ["-m", "pcbagent"], env };
}

function runCli(args: string[], input?: unknown, signal?: AbortSignal, cwd?: string): Promise<CliResult> {
	const { cmd, args: base, env } = pythonCommand();
	return new Promise((resolvePromise, reject) => {
		const child = spawn(cmd, [...base, "--json", ...args], { cwd, env, signal, stdio: ["pipe", "pipe", "pipe"] });
		let stdout = "";
		let stderr = "";
		child.stdout.on("data", (d) => (stdout += d.toString()));
		child.stderr.on("data", (d) => (stderr += d.toString()));
		child.on("error", (err) => reject(err));
		child.on("close", (code) => {
			let json: any = undefined;
			const last = stdout.trim().split("\n").pop() ?? "";
			try {
				json = JSON.parse(last);
			} catch {
				json = undefined;
			}
			resolvePromise({ code: code ?? 1, json, stdout, stderr });
		});
		if (input !== undefined) child.stdin.write(JSON.stringify(input));
		child.stdin.end();
	});
}

// ---------------------------------------------------------------------------
// Run state
// ---------------------------------------------------------------------------

const PCB_TOOLS = ["pcb_models", "pcb_parts_search", "pcb_spec", "pcb_circuit", "pcb_layout", "pcb_status"];

interface RunState {
	dir?: string;
	active: boolean;
	includeLayout: boolean;
	maxCircuitAttempts: number;
	maxLayoutAttempts: number;
	idleNudges: number;
	toolCallsThisRun: number;
	savedTools?: string[];
}

const state: RunState = {
	active: false,
	includeLayout: true,
	maxCircuitAttempts: Number(process.env.PCBAGENT_MAX_ATTEMPTS || 20),
	maxLayoutAttempts: Number(process.env.PCBAGENT_MAX_LAYOUT_ATTEMPTS || 6),
	idleNudges: 0,
	toolCallsThisRun: 0,
};

/** pi.sendUserMessage starts the run asynchronously: wait until it is busy, then until it is idle. */
async function waitForRun(ctx: { isIdle(): boolean; waitForIdle(): Promise<void> }): Promise<void> {
	for (let i = 0; i < 200 && ctx.isIdle(); i++) await new Promise((r) => setTimeout(r, 25));
	await ctx.waitForIdle();
}

function text(t: string) {
	return { content: [{ type: "text" as const, text: t }] };
}

function requireDir(): string {
	if (!state.dir) throw new Error("No active design. Start one with /design <request>.");
	return state.dir;
}

function truncate(s: string, max = 6000): string {
	return s.length > max ? `${s.slice(0, max)}\n... (truncated)` : s;
}

function formatFailure(r: CliResult): string {
	if (r.json?.errors) return `Rejected:\n${r.json.errors.map((e: string) => `- ${e}`).join("\n")}`;
	if (r.json?.error) return `Error: ${r.json.error}`;
	return `pcbagent exited with code ${r.code}:\n${truncate(r.stderr || r.stdout, 2000)}`;
}

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

// Prompt text lives in Python (src/pcbagent/agent/prompts.py) and is exported to
// schemas/prompts.json, so the Pi extension and `pcbagent run` never drift apart.
const PROMPTS: { rules: string; example: string; steps: string; step_layout: string; step_no_layout: string } =
	loadSchema("prompts") as any;

function designKickoff(request: string, dir: string, includeLayout: boolean): string {
	return `Design a PCB for this request:\n\n"${request}"\n\nDesign directory: ${dir}\n\n${PROMPTS.steps}\n${
		includeLayout ? PROMPTS.step_layout : PROMPTS.step_no_layout
	}\n\n${PROMPTS.rules}\n\n${PROMPTS.example}`;
}

function layoutKickoff(notes: string): string {
	return `The circuit is verified and every part is sourced. Lay out the board with pcb_layout.
Give placement constraints, not coordinates: "near" for decoupling capacitors (to the IC pin they serve, max 2-3 mm), "edge" for connectors, "flow" for the power/signal path, "group" for parts that belong together. The placer, Freerouting and KiCad DRC do the geometry.${notes ? `\nUser notes: ${notes}` : ""}
If DRC fails, adjust the constraints (or allow a larger board) and call pcb_layout again.`;
}

function continuePrompt(status: any): string {
	const lines: string[] = ["PCBAgent: the design is not finished, so the run continues."];
	if (status.phase === "spec") {
		lines.push("No spec is locked yet. Call pcb_spec now.");
	} else if (status.phase === "circuit") {
		const failed: string[] = status.verification?.failed ?? [];
		if (failed.length) {
			lines.push("Failing checks from the last simulation:");
			for (const f of failed.slice(0, 8)) lines.push(`- ${f}`);
		}
		lines.push(`Next: ${status.next}. Call pcb_circuit with the complete revised circuit.`);
		lines.push(`Circuit attempts so far: ${status.circuit_attempts} of ${state.maxCircuitAttempts}.`);
	} else if (status.phase === "layout") {
		if (status.layout?.message) lines.push(`Last layout: ${status.layout.message}`);
		lines.push("Next: call pcb_layout with placement constraints.");
		lines.push(`Layout attempts so far: ${status.layout_attempts} of ${state.maxLayoutAttempts}.`);
	}
	lines.push("Do not ask the user; act with the tools.");
	return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Ollama (local) provider
// ---------------------------------------------------------------------------

function isLocal(url: string): boolean {
	try {
		const h = new URL(url).hostname;
		return h === "localhost" || h === "127.0.0.1" || h === "::1" || h === "[::1]" || h.endsWith(".localhost");
	} catch {
		return false;
	}
}

async function ollamaModels(host: string): Promise<any[]> {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), 800);
	try {
		const res = await fetch(`${host.replace(/\/$/, "")}/api/tags`, { signal: ctrl.signal });
		if (!res.ok) return [];
		const data: any = await res.json();
		return (data.models ?? []).map((m: any) => ({
			id: m.name,
			name: `${m.name} (local)`,
			reasoning: false,
			input: ["text"],
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			contextWindow: Number(process.env.PCBAGENT_CONTEXT_WINDOW || 32768),
			maxTokens: 8192,
			compat: {
				supportsStore: false,
				supportsDeveloperRole: false,
				supportsReasoningEffort: false,
				maxTokensField: "max_tokens",
			},
		}));
	} catch {
		return [];
	} finally {
		clearTimeout(timer);
	}
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------

export default async function pcbagent(pi: ExtensionAPI) {
	// Local Ollama models appear in /model without editing models.json.
	const host = process.env.OLLAMA_HOST
		? process.env.OLLAMA_HOST.startsWith("http")
			? process.env.OLLAMA_HOST
			: `http://${process.env.OLLAMA_HOST}`
		: "http://127.0.0.1:11434";
	if (process.env.PCBAGENT_REGISTER_OLLAMA !== "0") {
		if (!isLocal(host) && process.env.PCBAGENT_ALLOW_REMOTE_MODEL !== "1") {
			// A remote model endpoint would ship the design off this machine.
		} else {
			const models = await ollamaModels(host);
			if (models.length) {
				pi.registerProvider("ollama", {
					name: "Ollama (local)",
					baseUrl: `${host.replace(/\/$/, "")}/v1`,
					api: "openai-completions",
					apiKey: "ollama",
					models,
				});
			}
		}
	}

	const specSchema = loadSchema("spec");
	const circuitSchema = loadSchema("circuit");
	const constraintsSchema = loadSchema("constraints");
	const strict = { type: "json_schema" as const, strict: "prefer" as const };

	pi.registerTool({
		name: "pcb_models",
		label: "PCB part models",
		description:
			"List the part models a circuit may use: id, kind, pins (name: meaning), ratings and datasheet rules. Optionally filter by kind (resistor, capacitor, inductor, led, diode, npn, nmos, ldo, connector, testpoint).",
		promptSnippet: "List the parts a PCBAgent circuit may use",
		parameters: Type.Object({ kind: Type.Optional(Type.String({ description: "Filter by kind" })) }),
		annotations: { readOnlyHint: true, openWorldHint: false },
		async execute(_id, params, signal) {
			const args = ["models"];
			if (params.kind) args.push("--kind", params.kind);
			const r = await runCli(args, undefined, signal);
			if (!r.json?.ok) throw new Error(formatFailure(r));
			const lines = r.json.models.map((m: any) => JSON.stringify(m));
			return { ...text(lines.join("\n")), details: { count: lines.length } };
		},
	});

	pi.registerTool({
		name: "pcb_parts_search",
		label: "Search parts catalog",
		description:
			"Search the local JLCPCB catalog (offline). Use to check which values/packages are in stock before choosing them, e.g. {kind: 'resistor', package: '0603', value: '19.6k'} or {query: 'AMS1117'}.",
		parameters: Type.Object({
			kind: Type.Optional(Type.String({ description: "resistor, capacitor, inductor, led, diode, ldo, transistor, connector" })),
			package: Type.Optional(Type.String({ description: "e.g. 0603" })),
			value: Type.Optional(Type.String({ description: "e.g. 10k, 100n" })),
			query: Type.Optional(Type.String({ description: "free text: MPN or description words" })),
			limit: Type.Optional(Type.Number({ description: "max results (default 10)" })),
		}),
		annotations: { readOnlyHint: true, openWorldHint: false },
		async execute(_id, params, signal) {
			const args = ["catalog", "search", "--limit", String(params.limit ?? 10)];
			if (params.kind) args.push("--kind", params.kind);
			if (params.package) args.push("--package", params.package);
			if (params.value) args.push("--value", params.value);
			if (params.query) args.push("--query", params.query);
			const r = await runCli(args, undefined, signal);
			if (!r.json?.ok) throw new Error(formatFailure(r));
			const rows = r.json.parts.map(
				(p: any) => `${p.lcsc} ${p.mpn} | ${p.package} | ${p.library_type} | stock ${p.stock} | ${p.description}`,
			);
			return { ...text(rows.length ? rows.join("\n") : "no matching parts in the local catalog"), details: undefined };
		},
	});

	pi.registerTool({
		name: "pcb_spec",
		label: "Write and lock spec",
		description:
			"Write the circuit specification and lock it. Ports: every external net and GND (role ground). Conditions: test benches with ideal sources ({net, volts} or {net, amps}) and loads ({net, ohms} or {net, amps}) on port nets. Requirements: {id, condition, measure, target+tolerance or min/max}. Measure types: voltage{net}, source_current{net}, source_power{net}, load_power{net}, efficiency, component_current{ref,pin}, component_power{ref}, voltage_change{net,from_condition}, gain_db{input,output,hz}, cutoff_hz{input,output}. Can only be called once per design.",
		promptSnippet: "Write and lock the PCB specification",
		parameters: Type.Unsafe<any>(specSchema),
		constrainedSampling: strict,
		executionMode: "sequential",
		async execute(_id, params, signal) {
			const dir = requireDir();
			const r = await runCli(["spec", dir], params, signal);
			if (!r.json?.ok) throw new Error(formatFailure(r));
			return { ...text(`${r.json.summary}\nNext: call pcb_circuit with the full circuit.`), details: r.json };
		},
	});

	pi.registerTool({
		name: "pcb_circuit",
		label: "Submit circuit",
		description:
			"Submit the complete circuit: components [{ref, part, value?, package?, pins: {pin: NET}}]. Parts come from pcb_models. The tool validates wiring, simulates every spec condition in ngspice across tolerance corners, checks ratings and datasheet rules, then sources each part from the local JLCPCB catalog. Returns pass/fail per requirement with measured values. Resubmit the whole circuit after each fix.",
		promptSnippet: "Submit a circuit for simulation, verification and sourcing",
		parameters: Type.Unsafe<any>(circuitSchema),
		constrainedSampling: strict,
		executionMode: "sequential",
		async execute(_id, params, signal, onUpdate) {
			const dir = requireDir();
			onUpdate?.({ ...text("simulating with ngspice..."), details: undefined });
			const r = await runCli(["circuit", dir], params, signal);
			if (!r.json?.ok) throw new Error(formatFailure(r));
			const res = r.json;
			const body = `${res.summary}\nNext: ${res.next}`;
			return { ...text(truncate(body)), details: { passed: res.passed, stage: res.stage, version: res.version } };
		},
	});

	pi.registerTool({
		name: "pcb_layout",
		label: "Place and route",
		description:
			"Lay out the verified design. Give placement constraints, never coordinates: near{ref,to,pin?,max_mm}, edge{ref,side}, group{refs,max_mm}, flow{refs,axis}, align{refs,axis}, apart{ref,to,min_mm}; optional board{max_width_mm,max_height_mm}. A placer positions parts, Freerouting routes, KiCad DRC scores the board.",
		promptSnippet: "Place and route the board from placement constraints",
		parameters: Type.Unsafe<any>(constraintsSchema),
		constrainedSampling: strict,
		executionMode: "sequential",
		async execute(_id, params, signal, onUpdate) {
			const dir = requireDir();
			onUpdate?.({ ...text("placing, routing with Freerouting, running KiCad DRC..."), details: undefined });
			const r = await runCli(["layout", dir], params, signal);
			if (!r.json?.ok) throw new Error(formatFailure(r));
			const res = r.json;
			const extra = res.passed
				? `\nBoard: ${dir}/layout. Watch it with /pcb-replay; export fabrication files with /pcb-fab.`
				: "\nAdjust constraints (or allow a bigger board) and call pcb_layout again.";
			return { ...text(truncate(res.summary + extra)), details: { passed: res.passed, drc: res.drc } };
		},
	});

	pi.registerTool({
		name: "pcb_status",
		label: "Design status",
		description: "Show the current design's phase, last verification failures, layout result and next step.",
		parameters: Type.Object({}),
		annotations: { readOnlyHint: true, openWorldHint: false },
		async execute(_id, _params, signal) {
			const dir = requireDir();
			const r = await runCli(["status", dir], undefined, signal);
			if (!r.json) throw new Error(formatFailure(r));
			return { ...text(JSON.stringify(r.json, null, 1)), details: undefined };
		},
	});

	// Defence in depth: the Python side re-hashes the locked spec on every call,
	// but built-in file tools should not touch design state at all.
	pi.on("tool_call", async (event) => {
		if (!state.dir) return;
		if (!["write", "edit", "bash"].includes(event.toolName)) return;
		const blob = JSON.stringify(event.input ?? {});
		const protectedFiles = ["spec.json", "pcbagent.json", "verification.json", "parts.json"];
		if (protectedFiles.some((f) => blob.includes(f)) && blob.includes(state.dir.split("/").pop() ?? "\u0000")) {
			return { block: true, reason: "PCBAgent design state is written only by the pcb_* tools." };
		}
	});

	pi.on("tool_execution_end", async (event: any) => {
		if (state.active && PCB_TOOLS.includes(event.toolName)) {
			state.toolCallsThisRun++;
			state.idleNudges = 0;
		}
	});

	const finish = (ctx: ExtensionContext, message: string, level: "info" | "warning" | "error" = "info") => {
		state.active = false;
		if (state.savedTools) {
			pi.setActiveTools(state.savedTools);
			state.savedTools = undefined;
		}
		if (ctx.hasUI) ctx.ui.notify(message, level);
	};

	// The run continues on its own: whenever the model stops, check the design
	// state and push it to the next step until it is done or the budget is spent.
	pi.on("agent_before_settle", async (event, ctx) => {
		if (!state.active || !state.dir) return;
		if (event.outcome !== "completed") {
			finish(ctx, `PCBAgent run stopped (${event.outcome}). Resume with /layout or /design.`, "warning");
			return;
		}
		const r = await runCli(["status", state.dir]);
		const status = r.json;
		if (!status || status.ok === false) {
			finish(ctx, `PCBAgent: cannot read design status: ${formatFailure(r)}`, "error");
			return;
		}
		if (status.phase === "done" || (status.phase === "layout" && !state.includeLayout)) {
			finish(ctx, `PCBAgent: ${status.phase === "done" ? "board routed, DRC clean" : "circuit verified and sourced"} (${state.dir})`);
			return;
		}
		if (status.circuit_attempts >= state.maxCircuitAttempts && status.phase !== "layout") {
			finish(ctx, `PCBAgent: stopped after ${status.circuit_attempts} circuit attempts without a passing design.`, "warning");
			return;
		}
		if (status.layout_attempts >= state.maxLayoutAttempts && status.phase === "layout") {
			finish(ctx, `PCBAgent: stopped after ${status.layout_attempts} layout attempts without a DRC-clean board.`, "warning");
			return;
		}
		state.idleNudges++;
		if (state.idleNudges > 3) {
			finish(ctx, "PCBAgent: the model stopped calling tools; run stopped.", "warning");
			return;
		}
		return {
			entries: [{ type: "custom_message", customType: "pcbagent-continue", content: continuePrompt(status), display: true }],
			continue: true,
		};
	});

	pi.on("session_start", async (_event, ctx) => {
		for (const entry of ctx.sessionManager.getBranch() as any[]) {
			if (entry.type === "custom" && entry.customType === "pcbagent-run" && entry.data?.dir) {
				state.dir = entry.data.dir;
			}
		}
		const missing = ["PI_OFFLINE", "PI_TELEMETRY", "PI_SKIP_VERSION_CHECK"].filter((v) => !process.env[v]);
		if (missing.length && ctx.hasUI) {
			ctx.ui.notify(
				`PCBAgent: for a fully local session start pi with PI_OFFLINE=1 PI_TELEMETRY=0 PI_SKIP_VERSION_CHECK=1 (missing: ${missing.join(", ")})`,
				"warning",
			);
		}
	});

	const startRun = (includeLayout: boolean) => {
		state.active = true;
		state.includeLayout = includeLayout;
		state.idleNudges = 0;
		state.toolCallsThisRun = 0;
		if (!state.savedTools) state.savedTools = pi.getActiveTools();
		pi.setActiveTools(PCB_TOOLS);
	};

	pi.registerCommand("design", {
		description: "Design a verified, routed PCB from a request (runs until done or Escape)",
		handler: async (args, ctx) => {
			let request = args.trim();
			let includeLayout = true;
			if (request.startsWith("--no-layout")) {
				includeLayout = false;
				request = request.replace("--no-layout", "").trim();
			}
			if (!request) {
				ctx.ui.notify("Usage: /design [--no-layout] <what the board should do>", "warning");
				return;
			}
			if (!ctx.isIdle()) {
				ctx.ui.notify("Agent is busy; wait or press Escape first.", "warning");
				return;
			}
			const designs = process.env.PCBAGENT_DESIGNS_DIR || join(ctx.cwd, "designs");
			const r = await runCli(["init", "--request", request, "--designs", designs], undefined, undefined, ctx.cwd);
			if (!r.json?.ok) {
				ctx.ui.notify(`PCBAgent: ${formatFailure(r)}`, "error");
				return;
			}
			state.dir = r.json.dir;
			pi.appendEntry("pcbagent-run", { dir: state.dir, request });
			startRun(includeLayout);
			ctx.ui.notify(`PCBAgent: designing in ${state.dir}`, "info");
			pi.sendUserMessage(designKickoff(request, state.dir!, includeLayout));
			// Print/JSON/RPC modes end when the command returns; keep the run alive there.
			if (ctx.mode !== "tui") await waitForRun(ctx);
		},
	});

	pi.registerCommand("layout", {
		description: "Place, route and DRC the current verified design",
		handler: async (args, ctx) => {
			if (!state.dir) {
				ctx.ui.notify("No active design; use /design or /pcb-open <dir>.", "warning");
				return;
			}
			const r = await runCli(["status", state.dir]);
			if (r.json?.phase !== "layout" && r.json?.phase !== "done") {
				ctx.ui.notify(`The design is in phase '${r.json?.phase}'; it must pass verification first.`, "warning");
				return;
			}
			startRun(true);
			pi.sendUserMessage(layoutKickoff(args.trim()));
			if (ctx.mode !== "tui") await waitForRun(ctx);
		},
	});

	pi.registerCommand("pcb-open", {
		description: "Make an existing design directory the active design",
		handler: async (args, ctx) => {
			const p = args.trim();
			const dir = isAbsolute(p) ? p : resolve(ctx.cwd, p);
			const r = await runCli(["status", dir]);
			if (!r.json || r.json.ok === false) {
				ctx.ui.notify(`PCBAgent: ${formatFailure(r)}`, "error");
				return;
			}
			state.dir = dir;
			pi.appendEntry("pcbagent-run", { dir });
			ctx.ui.notify(`PCBAgent: active design ${dir} (phase ${r.json.phase}; next: ${r.json.next})`, "info");
		},
	});

	pi.registerCommand("pcb-status", {
		description: "Show the active design's state",
		handler: async (_args, ctx) => {
			if (!state.dir) return ctx.ui.notify("No active design.", "warning");
			const r = await runCli(["status", state.dir]);
			ctx.ui.notify(r.json ? `phase ${r.json.phase}; next: ${r.json.next}` : formatFailure(r), "info");
		},
	});

	pi.registerCommand("pcb-stop", {
		description: "Stop the automatic PCBAgent run after the current step",
		handler: async (_args, ctx) => finish(ctx, "PCBAgent: automatic run stopped."),
	});

	pi.registerCommand("pcb-replay", {
		description: "Open the board in KiCad and replay placement and routing",
		handler: async (_args, ctx) => {
			if (!state.dir) return ctx.ui.notify("No active design.", "warning");
			const r = await runCli(["replay", state.dir]);
			ctx.ui.notify(r.json?.message ?? formatFailure(r), r.json?.launched ? "info" : "warning");
		},
	});

	pi.registerCommand("pcb-fab", {
		description: "Export Gerbers, drill, JLCPCB BOM and CPL for the routed board",
		handler: async (_args, ctx) => {
			if (!state.dir) return ctx.ui.notify("No active design.", "warning");
			const r = await runCli(["fab", state.dir]);
			ctx.ui.notify(r.json?.ok ? `fabrication files: ${r.json.files.join(", ")}` : formatFailure(r), r.json?.ok ? "info" : "error");
		},
	});
}
