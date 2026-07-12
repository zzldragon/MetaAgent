import React from "react";
import { Handle, Position } from "reactflow";
import { KIND_COLORS, KIND_LABELS, STATUS_COLOR } from "./graph.js";

// custom node: 4 sides x {source,target} handles, status ring + step badge
export default function MetaNode({ data }) {
  const sides = [
    [Position.Top, "top"],
    [Position.Right, "right"],
    [Position.Bottom, "bottom"],
    [Position.Left, "left"],
  ];
  return (
    <div
      className={"mnode " + (data.status || "")}
      style={{ background: KIND_COLORS[data.kind] || "#eee" }}
    >
      {sides.map(([pos, id]) => (
        <React.Fragment key={id}>
          <Handle type="target" position={pos} id={"t_" + id} />
          <Handle type="source" position={pos} id={"s_" + id} />
        </React.Fragment>
      ))}
      <div className="kind">{KIND_LABELS[data.kind] || data.kind}</div>
      <div className="name">{data.name}</div>
      <div className="sub">{data.sub}</div>
      {data.status === "running" && (
        <div className="badge" style={{ background: STATUS_COLOR.running }}>▶</div>
      )}
      {data.status === "done" && (
        <div className="badge" style={{ background: STATUS_COLOR.done }}>✓</div>
      )}
      {data.status === "error" && (
        <div className="badge" style={{ background: STATUS_COLOR.error }}>!</div>
      )}
    </div>
  );
}
