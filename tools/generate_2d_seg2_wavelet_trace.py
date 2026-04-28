#!/usr/bin/env python3
"""Generate a concrete 2d_seg2 wavelet trace for the design document.

This is an offline documentation helper.  It builds a small CSR graph, partitions
it with the same block-partition code used by the runner, checks the current
ungated 2d_seg2 transport model, then writes a LaTeX snippet with a cycle-by-
cycle wavelet trace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from picasso.run_csl_tests import build_csr, partition_graph  # noqa: E402
from tools.selective_relay_preflight import (  # noqa: E402
    build_emit_flags,
    build_required_triples,
    receivers_of,
)


NUM_ROWS = 8
NUM_COLS = 8
NUM_VERTS = NUM_ROWS * NUM_COLS
S_ROW = 2


@dataclass(frozen=True)
class TraceEdge:
    label: str
    source_gid: int
    consumer_gid: int
    case_name: str
    description: str
    color_macro: str


@dataclass
class Step:
    pos: Tuple[int, int]
    prev: Optional[Tuple[int, int]]
    action: str
    delay: Optional[str] = None


TRACE_EDGES: List[TraceEdge] = [
    TraceEdge(
        "A", 8, 12, "same-row east",
        "same row; row bridges re-emit across row segments",
        "rowData",
    ),
    TraceEdge(
        "B", 2, 50, "same-column south",
        "same column; with S_col=1 every row boundary is a column bridge",
        "colData",
    ),
    TraceEdge(
        "C", 16, 45, "south-east via e2s",
        "moves east first, then an e2s relay turns the wavelet south",
        "bridgeColor",
    ),
    TraceEdge(
        "D", 21, 49, "anti-diagonal SW, non-last-column source",
        "current correctness path: east to last column, south, then back west",
        "backColor",
    ),
    TraceEdge(
        "E", 7, 34, "anti-diagonal SW, last-column source",
        "source is already at the back-channel ingress column",
        "barrierColor",
    ),
]


def pe_of_gid(gid: int) -> Tuple[int, int]:
    return gid // NUM_COLS, gid % NUM_COLS


def pe_name(gid: int) -> str:
    r, c = pe_of_gid(gid)
    return f"PE{gid}=({r},{c})"


def append_hold(path: List[Step], reason: str) -> None:
    path.append(Step(pos=path[-1].pos, prev=path[-1].pos, action="delay", delay=reason))


def append_move(path: List[Step], dst: Tuple[int, int], action: str) -> None:
    path.append(Step(pos=dst, prev=path[-1].pos, action=action))


def move_row_east(path: List[Step], target_col: int) -> None:
    """Move east with S_row=2 row bridge delay at odd bridge columns."""
    r, c = path[-1].pos
    while c < target_col:
        if c % S_ROW == S_ROW - 1:
            next_c = min(target_col, c + S_ROW)
        else:
            next_c = min(target_col, c + 1)
        append_move(path, (r, next_c), "row")
        c = next_c
        if c < target_col and c % S_ROW == S_ROW - 1:
            append_hold(path, "row br")


def move_col_south(path: List[Step], target_row: int) -> None:
    """Move south with S_col=1, so each intermediate row re-emits."""
    r, c = path[-1].pos
    while r < target_row:
        append_move(path, (r + 1, c), "col")
        r += 1
        if r < target_row:
            append_hold(path, "col br")


def move_back_west(path: List[Step], target_col: int) -> None:
    r, _c = path[-1].pos
    append_move(path, (r, target_col), "back")


def build_path(edge: TraceEdge) -> List[Step]:
    sr, sc = pe_of_gid(edge.source_gid)
    tr, tc = pe_of_gid(edge.consumer_gid)
    path = [Step(pos=(sr, sc), prev=None, action="source")]

    if tr == sr and tc > sc:
        move_row_east(path, tc)
    elif tc == sc and tr > sr:
        move_col_south(path, tr)
    elif tr > sr and tc > sc:
        move_row_east(path, tc)
        append_hold(path, "e2s")
        move_col_south(path, tr)
    elif tr > sr and tc < sc and sc < NUM_COLS - 1:
        move_row_east(path, NUM_COLS - 1)
        append_hold(path, "e2s")
        move_col_south(path, tr)
        append_hold(path, "back")
        move_back_west(path, tc)
    elif tr > sr and tc < sc and sc == NUM_COLS - 1:
        move_col_south(path, tr)
        append_hold(path, "back")
        move_back_west(path, tc)
    else:
        raise ValueError(f"unsupported trace edge shape: {edge}")

    path[-1].action = "done"
    return path


def step_at(path: List[Step], cycle: int) -> Step:
    if cycle < len(path):
        return path[cycle]
    return Step(pos=path[-1].pos, prev=path[-1].pos, action="done")


def direction_for_source(pe_data, edge: TraceEdge) -> str:
    spe = edge.source_gid
    d = pe_data[spe]
    for li, ngid, direction in zip(
        d["boundary_local_idx"],
        d["boundary_neighbor_gid"],
        d["boundary_direction"],
    ):
        if d["global_ids"][li] == edge.source_gid and ngid == edge.consumer_gid:
            return {0: "east", 1: "west", 2: "south", 3: "north"}[int(direction)]
    raise RuntimeError(f"source boundary entry missing for {edge}")


def validate_transport(pe_data) -> Tuple[int, List[Tuple[int, int, int]]]:
    emits_east, emits_south = build_emit_flags(pe_data, NUM_COLS, NUM_ROWS)
    gid_to_pe = lambda gid: gid
    triples = build_required_triples(pe_data, NUM_COLS, NUM_ROWS, gid_to_pe)
    failures: List[Tuple[int, int, int]] = []
    for cpe, spe, gid in sorted(triples):
        recv = receivers_of(
            spe,
            gid,
            NUM_COLS,
            NUM_ROWS,
            emits_east.get((spe, gid), False),
            emits_south.get((spe, gid), False),
            e2s_gate=None,
            back_gate=None,
        )
        if cpe not in recv:
            failures.append((cpe, spe, gid))
    return len(triples), failures


def draw_arrow_command(edge: TraceEdge, step: Step) -> str:
    if step.prev is None or step.prev == step.pos:
        return ""
    pr, pc = step.prev
    r, c = step.pos
    style = {
        "row": "traceRoute",
        "col": "traceRoute",
        "back": "traceBackRoute",
        "done": "traceRoute",
    }.get(step.action, "traceRoute")
    if step.action == "back":
        bend = ",densely dashed"
    else:
        bend = ""
    return (
        f"  \\draw[{style},color={edge.color_macro}{bend}] "
        f"(g-{pr}-{pc}.center) -- (g-{r}-{c}.center);\n"
    )


def draw_marker_command(edge: TraceEdge, step: Step) -> str:
    r, c = step.pos
    fill = f"{edge.color_macro}!12" if step.action == "done" else "white"
    lines = []
    if step.action == "delay":
        lines.append(
            f"  \\node[traceStar,fill={edge.color_macro}!32] at (g-{r}-{c}) {{}};\n"
        )
    lines.append(
        f"  \\node[traceWavelet,draw={edge.color_macro},fill={fill}] "
        f"at (g-{r}-{c}) {{{edge.label}}};\n"
    )
    return "".join(lines)


def cycle_panel(cycle: int, paths: Dict[str, List[Step]]) -> str:
    body = []
    for edge in TRACE_EDGES:
        step = step_at(paths[edge.label], cycle)
        body.append(draw_arrow_command(edge, step))
    for edge in TRACE_EDGES:
        step = step_at(paths[edge.label], cycle)
        body.append(draw_marker_command(edge, step))
    return (
        f"\\actualTraceGrid{{Cycle {cycle}}}{{\n"
        + "".join(body)
        + "}\n"
    )


def pe_label(pos: Tuple[int, int]) -> str:
    r, c = pos
    return f"PE{r * NUM_COLS + c}=({r},{c})"


def describe_step(edge: TraceEdge, step: Step, final_cycle: bool) -> str:
    label = edge.label
    if step.action == "source":
        return f"{label} starts at {pe_label(step.pos)}"
    if step.action == "row":
        return f"{label} moves east on the row stream from {pe_label(step.prev)} to {pe_label(step.pos)}"
    if step.action == "col":
        return f"{label} moves south on the column stream from {pe_label(step.prev)} to {pe_label(step.pos)}"
    if step.action == "back":
        return f"{label} moves west on the back-channel from {pe_label(step.prev)} to {pe_label(step.pos)}"
    if step.action == "delay":
        reason = step.delay or "bridge"
        if reason == "row br":
            return (
                f"{label} waits at row bridge {pe_label(step.pos)}; "
                "the receive task re-emits it on the next row segment"
            )
        if reason == "col br":
            return (
                f"{label} waits at column bridge {pe_label(step.pos)}; "
                "with S\\_col=1 each row boundary is a software handoff"
            )
        if reason == "e2s":
            return (
                f"{label} waits at e2s relay {pe_label(step.pos)}; "
                "the east arrival is re-emitted onto the south stream"
            )
        if reason == "back":
            return (
                f"{label} waits at back relay {pe_label(step.pos)}; "
                "the last-column PE injects the west back-channel next"
            )
        return f"{label} waits at {pe_label(step.pos)} for a software re-emit"
    if step.action == "done":
        if step.prev is not None and step.prev != step.pos:
            return f"{label} reaches consumer {pe_label(step.pos)}"
        if final_cycle:
            return f"{label} remains delivered at {pe_label(step.pos)}"
        return f"{label} is already delivered"
    return f"{label} is at {pe_label(step.pos)}"


def cycle_narrative_table(paths: Dict[str, List[Step]]) -> str:
    max_cycle = max(len(p) for p in paths.values()) - 1
    rows = []
    for cycle in range(max_cycle + 1):
        parts = []
        for edge in TRACE_EDGES:
            step = step_at(paths[edge.label], cycle)
            final_cycle = cycle == max_cycle
            parts.append(describe_step(edge, step, final_cycle))
        rows.append(
            f"{cycle} & " + "; ".join(parts) + r" \\" + "\n"
        )
    return (
        "{\\small\n"
        "\\begin{longtable}{@{}p{0.08\\linewidth}p{0.86\\linewidth}@{}}\n"
        "\\toprule\n"
        "Cycle & What happens in this simulator step \\\\\n"
        "\\midrule\n"
        "\\endhead\n"
        + "".join(rows)
        + "\\bottomrule\n"
        "\\end{longtable}\n"
        "}\n"
    )


def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def make_figure(title: str, panels: List[str], label: str) -> str:
    rows = []
    for row in chunks(panels, 3):
        padded = row + [""] * (3 - len(row))
        rows.append("\n&\n".join(padded))
    table_body = "\n\\\\[0.9em]\n".join(rows)
    return f"""\\begin{{landscape}}
\\begin{{figure}}[p]
\\centering
\\scriptsize
\\begin{{tabular}}{{ccc}}
{table_body}
\\end{{tabular}}
\\caption{{{title}}}
\\label{{{label}}}
\\end{{figure}}
\\end{{landscape}}
\\clearpage
"""


def latex_escape(text: str) -> str:
    return text.replace("_", "\\_")


def write_latex(path: str, directions: Dict[str, str],
                paths: Dict[str, List[Step]], required_count: int) -> None:
    max_cycle = max(len(p) for p in paths.values()) - 1
    panels = [cycle_panel(c, paths) for c in range(max_cycle + 1)]
    first = make_figure(
        "Concrete 2d\\_seg2 wavelet trace, cycles 0--5.",
        panels[:6],
        "fig:actual-wavelet-trace-a",
    )
    second = make_figure(
        f"Concrete 2d\\_seg2 wavelet trace, cycles 6--{max_cycle}.",
        panels[6:],
        "fig:actual-wavelet-trace-b",
    )

    rows = []
    for edge in TRACE_EDGES:
        rows.append(
            f"{edge.label} & ({edge.source_gid}, {edge.consumer_gid}) "
            f"& {pe_name(edge.source_gid)} & {pe_name(edge.consumer_gid)} "
            f"& {directions[edge.label]} & {latex_escape(edge.case_name)} \\\\"
        )
    edge_rows = "\n".join(rows)

    tex = f"""% Generated by tools/generate_2d_seg2_wavelet_trace.py.
% Do not edit by hand; rerun the generator instead.

\\subsection{{Concrete Graph Wavelet Trace}}

This trace uses a concrete CSR graph with {NUM_VERTS} vertices on an
8 by 8 \\designB{{}} fabric.  With block partitioning, vertex \\code{{gid}}
maps to \\code{{PE gid}}, so the selected graph edges are spread across the
fabric.  The run command was:

\\begin{{verbatim}}
python3 tools/generate_2d_seg2_wavelet_trace.py \\
  --run-id wavelet_trace_2d_seg2_actual_8x8
\\end{{verbatim}}

The generator calls the runner's block partitioner with
\\code{{sw\\_via\\_east\\_backchannel=True}} and then validates the same
ungated transport model used by
\\file{{tools/selective\\_relay\\_preflight.py}}.  For this graph the model
reaches all {required_count} required lower-GID-winner deliveries.

\\begin{{table}}[H]
\\centering
\\small
\\begin{{tabularx}}{{\\linewidth}}{{@{{}}c c c c c X@{{}}}}
\\toprule
Label & CSR edge & Source PE & Consumer PE & Source dir & Case \\\\
\\midrule
{edge_rows}
\\bottomrule
\\end{{tabularx}}
\\caption{{Actual CSR graph used for the wavelet trace.  The source is the
lower-GID endpoint, so it is the winner under the LWW rule and its wavelet must
reach the higher-GID consumer.}}
\\label{{tab:actual-wavelet-trace-edges}}
\\end{{table}}

\\newcommand{{\\actualTraceGrid}}[2]{{%
\\begin{{tikzpicture}}[x=0.34cm,y=0.34cm,
  traceWavelet/.style={{circle,fill=white,draw=black,line width=0.55pt,
    inner sep=0.9pt,font=\\tiny\\bfseries}},
  traceTarget/.style={{circle,fill=white,draw=black!70,dashed,line width=0.4pt,
    inner sep=0.7pt,font=\\tiny}},
  traceStar/.style={{star,star points=5,draw=black!70,inner sep=0.55pt}},
  traceRoute/.style={{->,>=Stealth,line width=1.05pt}},
  traceBackRoute/.style={{->,>=Stealth,line width=1.05pt,dashed}},
  traceTitle/.style={{draw=black!45,fill=white,rounded corners=1pt,
    align=center,font=\\tiny,inner sep=1.4pt}}
]
  \\foreach \\r in {{0,...,7}} {{
    \\foreach \\c in {{0,...,7}} {{
      \\pgfmathtruncatemacro{{\\oddcol}}{{mod(\\c,2)}}
      \\ifnum\\c=7
        \\ifnum\\r=0
          \\node[pebox,minimum width=0.28cm,minimum height=0.28cm,fill=peLight] (g-\\r-\\c) at (\\c,-\\r) {{}};
        \\else
          \\node[pebox,minimum width=0.28cm,minimum height=0.28cm,fill=backFill] (g-\\r-\\c) at (\\c,-\\r) {{}};
        \\fi
      \\else
        \\ifnum\\c=0
          \\node[pebox,minimum width=0.28cm,minimum height=0.28cm,fill=sinkFill] (g-\\r-\\c) at (\\c,-\\r) {{}};
        \\else
          \\ifnum\\oddcol=1
            \\node[pebox,minimum width=0.28cm,minimum height=0.28cm,fill=bridgeFill] (g-\\r-\\c) at (\\c,-\\r) {{}};
          \\else
            \\node[pebox,minimum width=0.28cm,minimum height=0.28cm,fill=colBridgeFill!45] (g-\\r-\\c) at (\\c,-\\r) {{}};
          \\fi
        \\fi
      \\fi
    }}
  }}
  \\foreach \\c in {{0,...,7}} {{\\node[font=\\tiny] at (\\c,0.48) {{\\c}};}}
  \\foreach \\r in {{0,...,7}} {{\\node[font=\\tiny] at (-0.48,-\\r) {{\\r}};}}

  \\node[traceTarget,draw=rowData] at (g-1-4) {{a}};
  \\node[traceTarget,draw=colData] at (g-6-2) {{b}};
  \\node[traceTarget,draw=bridgeColor] at (g-5-5) {{c}};
  \\node[traceTarget,draw=backColor] at (g-6-1) {{d}};
  \\node[traceTarget,draw=barrierColor] at (g-4-2) {{e}};

  #2

  \\node[traceTitle,anchor=north] at (3.5,-7.72) {{#1}};
\\end{{tikzpicture}}%
}}

\\noindent
\\textbf{{How to read the cycle panels.}}  Each panel is one offline simulator
cycle.  The uppercase labels A, B, C, D, and E are the five tracked
wavelets.  Each uppercase marker shows the current position of that wavelet;
the matching lowercase marker a, b, c, d, or e is the consumer PE for that
wavelet.  A star means a receive task has the wavelet and must spend a later
step re-emitting it on a row-bridge, column-bridge, e2s, or back-channel
color.  The cycle numbers are simulator steps for explanation, not WSE-3
performance-counter cycles.

\\paragraph{{Cycle-by-cycle explanation.}}
{cycle_narrative_table(paths)}

{first}
{second}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(tex)


def write_trace_json(path: str, pe_data, directions: Dict[str, str],
                     paths: Dict[str, List[Step]], failures) -> None:
    payload = {
        "graph": {
            "num_vertices": NUM_VERTS,
            "num_rows": NUM_ROWS,
            "num_cols": NUM_COLS,
            "edges": [
                {
                    "label": e.label,
                    "source_gid": e.source_gid,
                    "consumer_gid": e.consumer_gid,
                    "source_pe": pe_of_gid(e.source_gid),
                    "consumer_pe": pe_of_gid(e.consumer_gid),
                    "source_direction": directions[e.label],
                    "case": e.case_name,
                }
                for e in TRACE_EDGES
            ],
        },
        "transport_failures": failures,
        "cycles": {
            label: [
                {
                    "cycle": idx,
                    "row": step.pos[0],
                    "col": step.pos[1],
                    "action": step.action,
                    "delay": step.delay,
                    "prev": step.prev,
                }
                for idx, step in enumerate(path)
            ]
            for label, path in paths.items()
        },
        "nonempty_pes": [
            {
                "pe": idx,
                "row": idx // NUM_COLS,
                "col": idx % NUM_COLS,
                "global_ids": d["global_ids"],
                "boundary_neighbor_gid": d["boundary_neighbor_gid"],
                "boundary_direction": d["boundary_direction"],
            }
            for idx, d in enumerate(pe_data)
            if d["global_ids"] or d["boundary_neighbor_gid"]
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-scope", default="local")
    parser.add_argument("--run-id", default="wavelet_trace_2d_seg2_actual_8x8")
    parser.add_argument(
        "--tex-out",
        default="docs/generated/wavelet_trace/actual_2d_seg2_trace.tex",
    )
    args = parser.parse_args()

    run_dir = os.path.join(REPO_ROOT, "runs", args.run_scope, args.run_id)
    os.makedirs(run_dir, exist_ok=True)
    tex_out = os.path.join(REPO_ROOT, args.tex_out)
    os.makedirs(os.path.dirname(tex_out), exist_ok=True)

    edges = [(e.source_gid, e.consumer_gid) for e in TRACE_EDGES]
    offsets, adj = build_csr(NUM_VERTS, edges)
    pe_data = partition_graph(
        NUM_VERTS,
        offsets,
        adj,
        num_cols=NUM_COLS,
        num_rows=NUM_ROWS,
        mode="block",
        sw_via_east_backchannel=True,
    )
    directions = {e.label: direction_for_source(pe_data, e) for e in TRACE_EDGES}
    required_count, failures = validate_transport(pe_data)
    if failures:
        print("ERROR: transport model failed for concrete trace graph")
        for failure in failures[:10]:
            print(f"  failure: {failure}")
        return 1

    paths = {e.label: build_path(e) for e in TRACE_EDGES}
    write_latex(tex_out, directions, paths, required_count)
    write_trace_json(os.path.join(run_dir, "trace.json"), pe_data, directions, paths, failures)

    partition_txt = os.path.join(run_dir, "partition.txt")
    with open(partition_txt, "w", encoding="utf-8") as f:
        f.write("Concrete 2d_seg2 wavelet trace partition\n")
        f.write(f"grid={NUM_ROWS}x{NUM_COLS} vertices={NUM_VERTS}\n\n")
        for e in TRACE_EDGES:
            f.write(
                f"{e.label}: edge=({e.source_gid},{e.consumer_gid}) "
                f"source={pe_name(e.source_gid)} consumer={pe_name(e.consumer_gid)} "
                f"dir={directions[e.label]} case={e.case_name}\n"
            )

    print(f"PASS concrete trace graph: required_deliveries={required_count}")
    print(f"  run_dir: {run_dir}")
    print(f"  trace_json: {os.path.join(run_dir, 'trace.json')}")
    print(f"  partition: {partition_txt}")
    print(f"  latex: {tex_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
