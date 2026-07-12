import { MarkerType } from "reactflow";

// MetaAgent palette (mirrors canvas_frame.KIND_COLORS / KIND_LABELS)
export const KIND_COLORS = {
  agent: "#BBDEFB", llm: "#C8E6C9", tool: "#FFE0B2", skill: "#E1BEE7",
  prompt: "#FFF9C4", rag: "#B2DFDB", webserver: "#FFCDD2", mcp: "#D1C4E9",
  workerpool: "#C5CAE9", router: "#B0BEC5", hitl: "#FFE082", eval: "#DCEDC8",
};
export const KIND_LABELS = {
  agent: "Agent", llm: "LLM", tool: "Tools", skill: "Skills", prompt: "Prompt",
  rag: "RAG", webserver: "WebServer", mcp: "MCP", workerpool: "Worker Pool",
  router: "Router", hitl: "HITL", eval: "Eval",
};
export const STATUS_COLOR = { running: "#FFA000", done: "#2E7D32", error: "#C62828" };
export const FLOW_KINDS = new Set(["agent", "workerpool", "router", "hitl"]);
export const RESOURCE_KINDS = new Set(["llm", "tool", "skill", "prompt", "rag", "mcp"]);
export const NODE_W = 168;
export const NODE_H = 64;

export function subtitle(n) {
  const p = n.props || {};
  switch (n.kind) {
    case "llm": return p.model || p.provider || "";
    case "agent":
    case "workerpool":
    case "router": return "role: " + (p.role || "single");
    case "tool": return (p.files || []).join(", ");
    case "prompt": return "role: " + (p.role || "single");
    case "rag": return p.docs_dir ? p.docs_dir : "BM25 search_docs";
    case "skill": return ((p.skills || []).map((s) => s.name).join(", ")) || "skills";
    case "mcp": return p.transport || "";
    case "webserver": return (p.host || "127.0.0.1") + ":" + (p.port || 8765);
    case "eval": return ((p.cases || []).length) + " cases";
    default: return "";
  }
}

// pick which sides an edge attaches to, based on relative node centers
export function sidesFor(a, b) {
  const ax = a.x + NODE_W / 2, ay = a.y + NODE_H / 2;
  const bx = b.x + NODE_W / 2, by = b.y + NODE_H / 2;
  const dx = bx - ax, dy = by - ay;
  if (Math.abs(dx) >= Math.abs(dy))
    return dx >= 0 ? ["s_right", "t_left"] : ["s_left", "t_right"];
  return dy >= 0 ? ["s_bottom", "t_top"] : ["s_top", "t_bottom"];
}

// convert a MetaAgent graph (nodes:{id,kind,name,x,y,props}, edges:{src,dst}) -> RF
export function toReactFlow(graph) {
  const byId = {};
  (graph.nodes || []).forEach((n) => { byId[n.id] = n; });
  const nodes = (graph.nodes || []).map((n) => ({
    id: n.id,
    type: "meta",
    position: { x: n.x, y: n.y },
    data: { kind: n.kind, name: n.name, sub: subtitle(n), status: "" },
  }));
  const edges = (graph.edges || []).map((e, i) => {
    const s = byId[e.src], d = byId[e.dst];
    if (!s || !d) return null;
    const isFlow = FLOW_KINDS.has(s.kind) && FLOW_KINDS.has(d.kind);
    const [sh, th] = sidesFor(s, d);
    return {
      id: "e" + i,
      source: e.src,
      target: e.dst,
      sourceHandle: sh,
      targetHandle: th,
      type: "smoothstep",
      animated: isFlow,
      style: {
        stroke: isFlow ? "#1565C0" : "#90A4AE",
        strokeWidth: isFlow ? 2.2 : 1.3,
        strokeDasharray: isFlow ? undefined : "5 4",
      },
      markerEnd: isFlow ? { type: MarkerType.ArrowClosed, color: "#1565C0" } : undefined,
    };
  }).filter(Boolean);
  return { nodes, edges };
}

// embedded sample (PEC.json — planner->executor->critic + router; keys redacted)
export const SAMPLE = {
  nodes: [
    { id: "agent_1", kind: "agent", name: "planner", x: 293, y: 130, props: { role: "planner" } },
    { id: "llm_2", kind: "llm", name: "llm_planner", x: 294, y: 70, props: { provider: "siliconflow", model: "DeepSeek-V4-Flash" } },
    { id: "agent_3", kind: "agent", name: "executor", x: 744, y: 575, props: { role: "worker" } },
    { id: "llm_4", kind: "llm", name: "llm_executor", x: 742, y: 514, props: { model: "DeepSeek-V4-Flash" } },
    { id: "agent_5", kind: "agent", name: "critic", x: 1110, y: 377, props: { role: "critic" } },
    { id: "llm_6", kind: "llm", name: "llm_critic", x: 1113, y: 497, props: { model: "DeepSeek-V4-Flash" } },
    { id: "tool_7", kind: "tool", name: "tools", x: 750, y: 695, props: { files: ["base64_decode.py"] } },
    { id: "agent_8", kind: "agent", name: "agent", x: 730, y: 271, props: { role: "worker" } },
    { id: "llm_9", kind: "llm", name: "llm_agent", x: 732, y: 211, props: { model: "DeepSeek-V4-Flash" } },
    { id: "tool_11", kind: "tool", name: "tool_4", x: 732, y: 327, props: { files: ["base64_encode.py", "load_csv.py"] } },
    { id: "prompt_12", kind: "prompt", name: "prompt_12", x: 297, y: 185, props: { role: "planner", text: "You are job orchestrator, the planner." } },
    { id: "prompt_13", kind: "prompt", name: "prompt_13", x: 747, y: 634, props: { role: "worker", text: "You are base64decoder, the worker." } },
    { id: "prompt_14", kind: "prompt", name: "prompt_14", x: 731, y: 382, props: { role: "worker", text: "You are base64_encoder and csv loader." } },
    { id: "prompt_15", kind: "prompt", name: "prompt_15", x: 1111, y: 439, props: { role: "critic", text: "You are the critic." } },
    { id: "router_16", kind: "router", name: "router_16", x: 367, y: 469, props: { role: "router" } },
    { id: "llm_17", kind: "llm", name: "llm_17", x: 367, y: 408, props: { model: "DeepSeek-V4-Flash" } },
    { id: "prompt_18", kind: "prompt", name: "prompt_18", x: 367, y: 528, props: { role: "single", text: "You are the router." } },
  ],
  edges: [
    { src: "llm_2", dst: "agent_1" }, { src: "llm_4", dst: "agent_3" }, { src: "llm_6", dst: "agent_5" },
    { src: "tool_7", dst: "agent_3" }, { src: "agent_3", dst: "agent_5" }, { src: "agent_5", dst: "agent_1" },
    { src: "llm_9", dst: "agent_8" }, { src: "tool_11", dst: "agent_8" }, { src: "agent_8", dst: "agent_5" },
    { src: "prompt_12", dst: "agent_1" }, { src: "prompt_13", dst: "agent_3" }, { src: "prompt_14", dst: "agent_8" },
    { src: "prompt_15", dst: "agent_5" }, { src: "llm_17", dst: "router_16" }, { src: "prompt_18", dst: "router_16" },
    { src: "agent_1", dst: "router_16" }, { src: "router_16", dst: "agent_3" }, { src: "router_16", dst: "agent_8" },
  ],
};
