import { useState, useMemo, useCallback, useRef } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  ReactFlowProvider,
} from "reactflow";
import JSZip from "jszip";
import MetaNode from "./MetaNode.jsx";
import { KIND_COLORS, KIND_LABELS, toReactFlow, SAMPLE } from "./graph.js";

const nodeTypes = { meta: MetaNode };

export default function App() {
  const [graph, setGraph] = useState(SAMPLE);
  const [statuses, setStatuses] = useState({});
  const [running, setRunning] = useState(false);
  const timerRef = useRef(null);

  const base = useMemo(() => toReactFlow(graph), [graph]);

  // overlay statuses onto the converted nodes
  const nodes = useMemo(
    () =>
      base.nodes.map((n) => ({
        ...n,
        data: { ...n.data, status: statuses[n.id] || "" },
      })),
    [base, statuses]
  );

  const stopSim = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setRunning(false);
  }, []);

  const loadFile = useCallback(
    async (file) => {
      stopSim();
      setStatuses({});
      try {
        if (file.name.toLowerCase().endsWith(".mta")) {
          const zip = await JSZip.loadAsync(file);
          const entry = zip.file("graph.json");
          if (!entry) throw new Error("graph.json not found in .mta bundle");
          setGraph(JSON.parse(await entry.async("string")));
        } else {
          setGraph(JSON.parse(await file.text()));
        }
      } catch (err) {
        alert("Could not load graph:\n" + err.message);
      }
    },
    [stopSim]
  );

  // mirror runtime_overlay: walk the agent stages left->right idle->running->done
  function simulate() {
    stopSim();
    const order = (graph.nodes || [])
      .filter((n) => n.kind === "agent" || n.kind === "workerpool" || n.kind === "router")
      .sort((a, b) => a.x - b.x);
    if (!order.length) return;
    setStatuses({});
    setRunning(true);
    let i = 0;
    timerRef.current = setInterval(() => {
      setStatuses((prev) => {
        const next = { ...prev };
        if (i > 0) next[order[i - 1].id] = "done";
        if (i < order.length) next[order[i].id] = "running";
        return next;
      });
      i += 1;
      if (i > order.length) stopSim();
    }, 700);
  }

  return (
    <div className="app">
      <div className="toolbar">
        <h1>MetaAgent · React Flow canvas</h1>
        <button className="btn" onClick={simulate} disabled={running}>
          ▶ Simulate run
        </button>
        <button
          className="btn secondary"
          onClick={() => {
            stopSim();
            setStatuses({});
          }}
        >
          Reset
        </button>
        <span className="spacer"></span>
        <label className="file">
          Load .json / .mta…
          <input
            type="file"
            accept=".json,.mta"
            onChange={(e) => e.target.files[0] && loadFile(e.target.files[0])}
          />
        </label>
        <button
          className="btn secondary"
          onClick={() => {
            stopSim();
            setStatuses({});
            setGraph(SAMPLE);
          }}
        >
          Load sample
        </button>
      </div>

      <div className="legend">
        {Object.keys(KIND_LABELS).map((k) => (
          <span className="item" key={k}>
            <span className="sw" style={{ background: KIND_COLORS[k] }}></span>
            {KIND_LABELS[k]}
          </span>
        ))}
        <span className="item" style={{ marginLeft: 12 }}>
          <span style={{ width: 22, height: 0, borderTop: "2px solid #1565C0" }}></span> flow
          (agent→agent)
        </span>
        <span className="item">
          <span style={{ width: 22, height: 0, borderTop: "2px dashed #90A4AE" }}></span>{" "}
          resource→agent
        </span>
      </div>

      <div className="flow">
        <ReactFlowProvider>
          <ReactFlow
            nodes={nodes}
            edges={base.edges}
            nodeTypes={nodeTypes}
            fitView
            minZoom={0.1}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={18} size={1} color="#3a3d46" />
            <MiniMap
              pannable
              zoomable
              nodeColor={(n) => KIND_COLORS[n.data.kind] || "#888"}
              style={{ background: "#26282f" }}
            />
            <Controls />
          </ReactFlow>
        </ReactFlowProvider>
      </div>

      <div className="hint">
        {(graph.nodes || []).length} nodes · {(graph.edges || []).length} edges — drag nodes,
        scroll to zoom, drag canvas to pan, use the minimap. Solid blue arrows = control flow;
        dashed grey = resources feeding an agent. "Simulate run" animates the same
        idle→running→done states your wx runtime overlay paints.
      </div>
    </div>
  );
}
