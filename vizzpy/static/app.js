"use strict";

// ── State ────────────────────────────────────────────────────────────────────
let graphData      = null;       // function-level data from API (source of truth)
let collapsedNodes = new Set();
let currentZoomK   = 1;          // tracks current zoom scale for drag compensation
let viewLevel      = "function"; // "function" | "module"
let lastGraph      = null;       // last rendered dagre graph, for minimap sync
let highlightedNode = null;      // node ID currently highlighted by single-click
let _clickTimer    = null;       // timer used to distinguish single-click from double-click
let deepTrace      = false;      // when true, highlight full upstream/downstream chain

// ── Minimap constants ────────────────────────────────────────────────────────
const MINIMAP_W        = 200;
const MINIMAP_H        = 140;
const CANVAS_THRESHOLD = 150;   // nodes ≥ this → canvas mode (faster for large graphs)

// ── DOM refs ─────────────────────────────────────────────────────────────────
const fileInput    = document.getElementById("file-input");
const dropZone     = document.getElementById("drop-zone");
const analyzeBtn   = document.getElementById("analyze-btn");
const statusMsg    = document.getElementById("status-msg");
const uploadPanel  = document.getElementById("upload-panel");
const uploadSummary= document.getElementById("upload-summary");
const uploadToggle = document.getElementById("upload-toggle");
const graphPanel   = document.getElementById("graph-panel");
const fitBtn         = document.getElementById("fit-btn");
const levelCheck     = document.getElementById("level-check");
const deepTraceCheck = document.getElementById("deep-trace-check");
const darkToggle     = document.getElementById("dark-toggle");
const nodeCountEl  = document.getElementById("node-count");
const tooltip      = document.getElementById("tooltip");
const svgEl        = document.getElementById("graph-svg");
const svg          = d3.select("#graph-svg");
const inner        = d3.select("#graph-inner");

// ── Dark mode ─────────────────────────────────────────────────────────────────
(function initDark() {
  if (localStorage.getItem("vizpy-dark") === "1") {
    document.body.classList.add("dark");
    darkToggle.textContent = "Light";
  }
})();

darkToggle.addEventListener("click", () => {
  const isDark = document.body.classList.toggle("dark");
  darkToggle.textContent = isDark ? "Light" : "Dark";
  localStorage.setItem("vizpy-dark", isDark ? "1" : "0");
  // Canvas minimap uses JS colour constants; redraw when theme changes
  if (lastGraph && graphData && graphData.nodes.length >= CANVAS_THRESHOLD) {
    _buildMinimapCanvas(lastGraph);
  }
});

// ── Module-level aggregation ──────────────────────────────────────────────────
// Derives a module-level graph from function-level data.
// Cross-module function edges are collapsed to module edges (counts summed).
// Intra-module edges are dropped.  A module is external only when every one
// of its functions is external.
function _toModuleGraph(data) {
  const nodeById = new Map(data.nodes.map(n => [n.id, n]));

  // Module → external? (external only when ALL nodes in module are external)
  const moduleExternal = new Map();
  for (const n of data.nodes) {
    if (!moduleExternal.has(n.module)) {
      moduleExternal.set(n.module, n.external);
    } else if (!n.external) {
      moduleExternal.set(n.module, false);
    }
  }

  const nodes = [...moduleExternal.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([mod, ext]) => ({ id: mod, label: mod, module: mod, external: ext, docstring: null }));

  // Aggregate cross-module edges
  const edgeCounts = new Map();
  for (const e of data.edges) {
    const srcNode = nodeById.get(e.source);
    const tgtNode = nodeById.get(e.target);
    if (!srcNode || !tgtNode || srcNode.module === tgtNode.module) continue;
    const key = srcNode.module + "\x00" + tgtNode.module;
    edgeCounts.set(key, (edgeCounts.get(key) || 0) + (e.count || 1));
  }

  const edges = [...edgeCounts.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([key, count]) => {
      const sep = key.indexOf("\x00");
      return { source: key.slice(0, sep), target: key.slice(sep + 1), count };
    });

  return { nodes, edges, modules: {} }; // empty modules → no dagre clusters
}

// ── Level toggle ──────────────────────────────────────────────────────────────
levelCheck.addEventListener("change", () => {
  viewLevel = levelCheck.checked ? "module" : "function";
  if (viewLevel === "module") collapsedNodes.clear(); // module nodes have different ids
  if (graphData) renderGraph();
});

// ── Deep trace toggle ─────────────────────────────────────────────────────────
deepTraceCheck.addEventListener("change", () => {
  deepTrace = deepTraceCheck.checked;
  // Re-apply highlight with new trace depth if a node is selected
  if (highlightedNode !== null) applyHighlight(highlightedNode);
});

// ── File selection ────────────────────────────────────────────────────────────
function _isArchive(name) {
  return name.endsWith(".zip") || name.endsWith(".tar.gz") || name.endsWith(".tgz")
      || name.endsWith(".egg") || name.endsWith(".whl");
}

fileInput.addEventListener("change", () => {
  if (fileInput.files.length) {
    const name = fileInput.files[0].name;
    statusMsg.textContent = `Selected: ${name}`;
    uploadSummary.textContent = name;
    analyzeBtn.disabled = false;
  }
});

dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("drag-over"); });
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", e => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  const file = e.dataTransfer.files[0];
  if (file && _isArchive(file.name)) {
    fileInput._droppedFile = file;
    statusMsg.textContent = `Selected: ${file.name}`;
    uploadSummary.textContent = file.name;
    analyzeBtn.disabled = false;
  } else {
    statusMsg.textContent = "Please drop a .zip, .tar.gz, .egg, or .whl file.";
  }
});

// ── Analyze ───────────────────────────────────────────────────────────────────
analyzeBtn.addEventListener("click", async () => {
  analyzeBtn.disabled = true;
  statusMsg.textContent = "Analyzing…";

  try {
    const file = fileInput._droppedFile || fileInput.files[0];
    if (!file) { analyzeBtn.disabled = false; return; }
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/analyze", { method: "POST", body: form });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || res.statusText);
    }
    graphData = await res.json();
    collapsedNodes.clear();
    showGraph();
  } catch (err) {
    statusMsg.textContent = `Error: ${err.message}`;
    analyzeBtn.disabled = false;
  }
});

// ── Upload panel toggle ───────────────────────────────────────────────────────
uploadToggle.addEventListener("click", () => {
  uploadPanel.classList.toggle("collapsed");
});

// ── Render ────────────────────────────────────────────────────────────────────
function showGraph() {
  graphPanel.hidden = false;
  uploadPanel.classList.add("collapsed");
  renderGraph();
}

function clearHighlight() {
  highlightedNode = null;
  inner.selectAll("g.node").classed("highlighted caller callee dimmed", false);
  inner.selectAll("g.edgePath").classed("hl-edge hl-caller-edge hl-callee-edge dimmed", false);
}

function applyHighlight(nodeId) {
  highlightedNode = nodeId;

  // Build forward (v→[w]) and backward (w→[v]) adjacency from rendered edges
  const fwdAdj = new Map();
  const bwdAdj = new Map();
  inner.selectAll("g.edgePath").each(function(ek) {
    if (!fwdAdj.has(ek.v)) fwdAdj.set(ek.v, []);
    fwdAdj.get(ek.v).push(ek.w);
    if (!bwdAdj.has(ek.w)) bwdAdj.set(ek.w, []);
    bwdAdj.get(ek.w).push(ek.v);
  });

  let callers, callees;
  if (deepTrace) {
    // BFS backward → full upstream ancestors
    callers = new Set();
    const bfsBack = [...(bwdAdj.get(nodeId) || [])];
    while (bfsBack.length) {
      const cur = bfsBack.shift();
      if (callers.has(cur) || cur === nodeId) continue;
      callers.add(cur);
      bfsBack.push(...(bwdAdj.get(cur) || []));
    }
    // BFS forward → full downstream descendants
    callees = new Set();
    const bfsFwd = [...(fwdAdj.get(nodeId) || [])];
    while (bfsFwd.length) {
      const cur = bfsFwd.shift();
      if (callees.has(cur) || cur === nodeId) continue;
      callees.add(cur);
      bfsFwd.push(...(fwdAdj.get(cur) || []));
    }
  } else {
    // Immediate: direct callers/callees only
    callers = new Set(bwdAdj.get(nodeId) || []);
    callees = new Set(fwdAdj.get(nodeId) || []);
    callers.delete(nodeId);
    callees.delete(nodeId);
  }

  const hlSet = new Set([nodeId, ...callers, ...callees]);

  inner.selectAll("g.node").each(function(nid) {
    const sel = d3.select(this);
    sel.classed("highlighted", nid === nodeId);
    sel.classed("caller",      callers.has(nid));
    sel.classed("callee",      callees.has(nid));
    sel.classed("dimmed",      !hlSet.has(nid));
  });

  inner.selectAll("g.edgePath").each(function(ek) {
    // Classify each edge: caller edge (part of upstream chain), callee edge (part of downstream chain).
    // An edge belongs to the upstream chain if it points into the selected node or between callers.
    // An edge belongs to the downstream chain if it comes from the selected node or between callees.
    let isCallerEdge, isCalleeEdge;
    if (deepTrace) {
      isCallerEdge = (callers.has(ek.v) || ek.v === nodeId) && (callers.has(ek.w) || ek.w === nodeId) && ek.w !== ek.v;
      isCalleeEdge = (callees.has(ek.v) || ek.v === nodeId) && (callees.has(ek.w) || ek.w === nodeId) && ek.w !== ek.v;
    } else {
      isCallerEdge = ek.w === nodeId;   // edge pointing TO the selected node
      isCalleeEdge = ek.v === nodeId;   // edge pointing FROM the selected node
    }
    const isHl = isCallerEdge || isCalleeEdge;
    d3.select(this)
      .classed("hl-caller-edge", isCallerEdge)
      .classed("hl-callee-edge", isCalleeEdge)
      .classed("hl-edge", false)
      .classed("dimmed", !isHl);
  });
}

function renderGraph() {
  highlightedNode = null;
  inner.selectAll("*").remove();

  const activeData = viewLevel === "module" ? _toModuleGraph(graphData) : graphData;

  const g = buildDagreGraph(activeData);
  const render = new dagreD3.render();
  render(inner, g);

  // dagre-d3 doesn't always persist width/height on the node data after render.
  // Read dimensions from the rendered DOM elements so that intersect() works during drag.
  inner.selectAll("g.node").each(function(nodeId) {
    const node = g.node(nodeId);
    if (node && node.elem && (node.width == null || node.height == null)) {
      const bbox = this.getBBox();
      node.width  = bbox.width;
      node.height = bbox.height;
    }
  });

  // dagre-d3 sets marker-end as an SVG attribute with an absolute URL.  Firefox does not
  // re-render SVG attribute-based marker-end when only the path 'd' changes (drag).
  // Moving it to a CSS inline style forces Firefox to re-evaluate it on every repaint.
  inner.selectAll("g.edgePath path.path").each(function() {
    const me = this.getAttribute("marker-end");
    if (me) {
      const id = me.replace(/^url\([^#]*#/, "").replace(/\)$/, "");
      this.removeAttribute("marker-end");
      this.style.setProperty("marker-end", `url(#${id})`);
    }
  });

  // dagre-d3's createClusters only sets class="cluster"; it never copies node.class to the
  // rendered <g> element. Apply depth classes manually so CSS depth colours take effect.
  if (viewLevel === "function") {
    inner.selectAll("g.cluster").each(function(clusterId) {
      const nodeData = g.node(clusterId);
      if (nodeData && nodeData.class) {
        const el = d3.select(this);
        nodeData.class.split(/\s+/).forEach(cls => { if (cls) el.classed(cls, true); });
      }
    });
  }

  attachNodeHandlers(g, activeData);

  nodeCountEl.textContent =
    `${activeData.nodes.length} nodes · ${activeData.edges.length} edges`;

  const zoomBg = document.getElementById("zoom-bg");
  zoomBg.setAttribute("width",  svgEl.clientWidth);
  zoomBg.setAttribute("height", svgEl.clientHeight);

  fitToScreen(g);
  updateMinimap(g);
}

// Build a tree from dotted module names (mirrors _build_module_tree in render.py).
// Returns { nodes: { fullPath: { short, children: Set, isActual } }, topLevel: str[] }
function _buildModuleHierarchy(moduleNames) {
  const nodes = {};
  for (const m of moduleNames) {
    const parts = m.split(".");
    for (let i = 0; i < parts.length; i++) {
      const prefix = parts.slice(0, i + 1).join(".");
      if (!nodes[prefix]) nodes[prefix] = { short: parts[i], children: new Set(), isActual: false };
      if (i > 0) nodes[parts.slice(0, i).join(".")].children.add(prefix);
    }
    nodes[m].isActual = true;
  }
  const topLevel = Object.keys(nodes)
    .filter(k => { const d = k.lastIndexOf("."); return d === -1 || !nodes[k.slice(0, d)]; })
    .sort();
  return { nodes, topLevel };
}

function buildDagreGraph(data) {
  const useCompound = viewLevel === "function"; // clusters only for function view
  const g = new dagreD3.graphlib.Graph({ compound: useCompound, multigraph: false })
    .setGraph({ rankdir: "LR", nodesep: 40, ranksep: 80, marginx: 20, marginy: 20 })
    .setDefaultEdgeLabel(() => ({}));

  const hiddenNodes = viewLevel === "function" ? computeHiddenNodes(data) : new Set();

  if (viewLevel === "function") {
    const { nodes: treeNodes, topLevel } = _buildModuleHierarchy(Object.keys(data.modules));

    function _addClusters(keys, parentId, depthIdx) {
      for (const fullPath of [...keys].sort()) {
        const tNode = treeNodes[fullPath];
        const clusterId = `__cluster__${fullPath}`;
        g.setNode(clusterId, {
          label: fullPath,
          clusterLabelPos: "top",
          class: `cluster-node cluster-depth-${Math.min(depthIdx, 2)}`,
        });
        if (parentId) g.setParent(clusterId, parentId);
        if (tNode.children.size > 0) _addClusters(tNode.children, clusterId, depthIdx + 1);
      }
    }
    _addClusters(topLevel, null, 0);
  }

  for (const node of data.nodes) {
    if (hiddenNodes.has(node.id)) continue;
    const classes = [
      collapsedNodes.has(node.id) ? "collapsed" : "",
      node.external ? "external" : "",
    ].filter(Boolean).join(" ");
    g.setNode(node.id, {
      label: node.label,
      class: classes,
      rx: 6,
      ry: 6,
    });
    if (viewLevel === "function") {
      g.setParent(node.id, `__cluster__${node.module}`);
    }
  }

  for (const edge of data.edges) {
    if (hiddenNodes.has(edge.source) || hiddenNodes.has(edge.target)) continue;
    g.setEdge(edge.source, edge.target, {
      label: edge.count > 1 ? String(edge.count) : "",
      curve: d3.curveBasis,
    });
  }

  return g;
}

function computeHiddenNodes(data) {
  const adj = new Map();
  for (const node of data.nodes) adj.set(node.id, []);
  for (const edge of data.edges) {
    if (adj.has(edge.source)) adj.get(edge.source).push(edge.target);
  }
  const hidden = new Set();
  for (const nid of collapsedNodes) {
    const queue = [...(adj.get(nid) || [])];
    while (queue.length) {
      const cur = queue.shift();
      if (hidden.has(cur) || collapsedNodes.has(cur)) continue;
      hidden.add(cur);
      queue.push(...(adj.get(cur) || []));
    }
  }
  return hidden;
}

// ── Interaction ───────────────────────────────────────────────────────────────
function attachNodeHandlers(g, data) {
  const docMap = new Map(data.nodes.map(n => [n.id, n.docstring]));

  // Build nodeId → [connected edge path elements] lookup for drag
  const connectedEdges = new Map();
  data.nodes.forEach(n => connectedEdges.set(n.id, []));
  // "path.path" is the visible edge line — avoids descending into <defs> marker paths
  inner.selectAll("g.edgePath").each(function(ek) {
    const pathEl = d3.select(this).select("path.path");
    if (connectedEdges.has(ek.v)) connectedEdges.get(ek.v).push({ pathEl, v: ek.v, w: ek.w });
    if (connectedEdges.has(ek.w)) connectedEdges.get(ek.w).push({ pathEl, v: ek.v, w: ek.w });
  });

  const drag = d3.drag()
    .on("start", function() {
      d3.event.sourceEvent.stopPropagation(); // prevent SVG pan
      d3.select(this).raise();               // bring node to front
    })
    .on("drag", function(nodeId) {
      const node = g.node(nodeId);
      node.x += d3.event.dx / currentZoomK;
      node.y += d3.event.dy / currentZoomK;
      d3.select(this).attr("transform", `translate(${node.x},${node.y})`);
      // Redraw connected edges, ending at node boundaries (not centers)
      (connectedEdges.get(nodeId) || []).forEach(({ pathEl, v, w }) => {
        const src = g.node(v);
        const tgt = g.node(w);
        if (src && tgt) pathEl.attr("d", cubicPath(src, tgt));
      });
      updateClusters(g);
    });

  inner.selectAll("g.node")
    .call(drag)
    .on("mouseover", function(nodeId) {
      const doc = docMap.get(nodeId);
      if (!doc) return;
      tooltip.textContent = doc.split("\n").map(l => l.trim()).filter(l => l.length > 0).join("\n");
      tooltip.style.fontSize = Math.max(9, Math.round(11 * currentZoomK)) + "px";
      tooltip.style.display = "block";
    })
    .on("mousemove", function() {
      const tw = tooltip.offsetWidth;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      let x = d3.event.clientX + 14;
      let y = d3.event.clientY + 14;
      if (x + tw + 10 > vw) x = d3.event.clientX - tw - 8;
      if (y + 30    > vh)  y = d3.event.clientY - 30;
      tooltip.style.left = x + "px";
      tooltip.style.top  = y + "px";
    })
    .on("mouseout",  () => { tooltip.style.display = "none"; })
    .on("click", function(nodeId) {
      d3.event.stopPropagation(); // prevent SVG background handler from clearing immediately
      // Use a short timer so double-click can cancel before we act
      if (_clickTimer !== null) {
        // A second click arrived quickly — part of a dblclick; cancel and let dblclick handle it
        clearTimeout(_clickTimer);
        _clickTimer = null;
        return;
      }
      _clickTimer = setTimeout(() => {
        _clickTimer = null;
        if (highlightedNode === nodeId) clearHighlight();
        else                            applyHighlight(nodeId);
      }, 220);
    })
    .on("dblclick",  function(nodeId) {
      // Cancel any pending single-click action
      if (_clickTimer !== null) { clearTimeout(_clickTimer); _clickTimer = null; }
      if (viewLevel === "module") return; // collapse not meaningful at module level
      tooltip.style.display = "none";
      if (collapsedNodes.has(nodeId)) collapsedNodes.delete(nodeId);
      else                            collapsedNodes.add(nodeId);
      renderGraph();
    });
}

// Draw a cubic bezier edge between two node objects, connecting at their boundaries.
// Uses dagre-d3's intersect() (set up on each node during render) to find boundary points.
function cubicPath(srcNode, tgtNode) {
  let src, tgt;
  try {
    src = srcNode.intersect(tgtNode);
    tgt = tgtNode.intersect(srcNode);
  } catch (_) {
    src = { x: srcNode.x, y: srcNode.y };
    tgt = { x: tgtNode.x, y: tgtNode.y };
  }
  const cx = (src.x + tgt.x) / 2;
  return `M${src.x},${src.y} C${cx},${src.y} ${cx},${tgt.y} ${tgt.x},${tgt.y}`;
}

// Recalculate each cluster rect to tightly wrap its current child node positions.
// Process bottom-up (deepest clusters first) so outer clusters include inner ones.
function updateClusters(g) {
  const PAD      = 20;  // padding around child nodes
  const TOP_PAD  = 28;  // extra room at top for the cluster label

  // Collect all cluster IDs and sort deepest-first
  const allClusterIds = [];
  inner.selectAll("g.cluster").each(function(clusterId) { allClusterIds.push(clusterId); });
  function _clusterDepth(id) {
    let d = 0, p = g.parent(id);
    while (p) { d++; p = g.parent(p); }
    return d;
  }
  allClusterIds.sort((a, b) => _clusterDepth(b) - _clusterDepth(a));

  for (const clusterId of allClusterIds) {
    const childIds = (g.children(clusterId) || []).filter(id => g.node(id));
    if (childIds.length === 0) continue;

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const nid of childIds) {
      const n  = g.node(nid);
      const hw = (n.width  || 50) / 2;
      const hh = (n.height || 20) / 2;
      minX = Math.min(minX, n.x - hw);
      minY = Math.min(minY, n.y - hh);
      maxX = Math.max(maxX, n.x + hw);
      maxY = Math.max(maxY, n.y + hh);
    }
    if (!isFinite(minX)) continue;

    const w  = maxX - minX + PAD * 2;
    const h  = maxY - minY + PAD + TOP_PAD;
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2 + (TOP_PAD - PAD) / 2;

    const clusterSel = inner.selectAll("g.cluster").filter(id => id === clusterId);
    clusterSel.attr("transform", `translate(${cx},${cy})`);
    const rectSel  = clusterSel.select("rect");
    const labelSel = clusterSel.select("g.label");

    // Read current label offset relative to rect top before we change anything,
    // so we preserve dagre-d3's initial interior padding on subsequent resizes.
    const curH = +rectSel.attr("height") || h;
    const curLabelTransform = labelSel.attr("transform") || "translate(0,0)";
    const yMatch = curLabelTransform.match(/translate\s*\([^,]+,\s*([^)]+)\)/);
    const curLabelY = yMatch ? +yMatch[1] : 0;
    const labelOffsetFromTop = curLabelY + curH / 2;  // positive = inside rect

    rectSel
      .attr("x", -w / 2)
      .attr("y", -h / 2)
      .attr("width",  w)
      .attr("height", h);
    labelSel.attr("transform", `translate(0, ${-h / 2 + labelOffsetFromTop})`);
  }
}

// ── Zoom & fit ────────────────────────────────────────────────────────────────
const zoom = d3.zoom()
  .on("start", () => svgEl.classList.add("panning"))
  .on("zoom",  () => {
    inner.attr("transform", d3.event.transform);
    currentZoomK = d3.event.transform.k;
    if (lastGraph) _updateMinimapViewport(lastGraph);
  })
  .on("end",   () => svgEl.classList.remove("panning"));
svg.call(zoom);

// Clicking the graph background clears any active highlight
svg.on("click.bg", () => { if (highlightedNode !== null) clearHighlight(); });

function fitToScreen(g) {
  const W = svgEl.clientWidth;
  const H = svgEl.clientHeight;
  if (!g.graph().width || !g.graph().height) return;
  const scale = Math.min(W / (g.graph().width + 40), H / (g.graph().height + 40), 1);
  const tx = (W - g.graph().width  * scale) / 2;
  const ty = (H - g.graph().height * scale) / 2;
  svg.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
}

fitBtn.addEventListener("click", () => {
  if (lastGraph) fitToScreen(lastGraph);
});

// ── Minimap ───────────────────────────────────────────────────────────────────

// Compute the uniform scale + translation that fits the graph into the minimap.
function _mmTransform(g) {
  const gW = g.graph().width  || 1;
  const gH = g.graph().height || 1;
  const scale = Math.min(MINIMAP_W / gW, MINIMAP_H / gH) * 0.9;
  return {
    scale,
    tx: (MINIMAP_W - gW * scale) / 2,
    ty: (MINIMAP_H - gH * scale) / 2,
  };
}

// Rounded-rect helper for canvas (ctx.roundRect not universally available yet).
function _canvasRoundRect(ctx, x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y,     x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x,     y + h, r);
  ctx.arcTo(x,     y + h, x,     y,     r);
  ctx.arcTo(x,     y,     x + w, y,     r);
  ctx.closePath();
}

// SVG mode: clone #graph-inner at minimap scale (good for ≤ CANVAS_THRESHOLD nodes).
function _buildMinimapSVG(g) {
  const mmSvg = document.getElementById("minimap-svg");
  mmSvg.innerHTML = "";

  const clone = document.getElementById("graph-inner").cloneNode(true);
  clone.setAttribute("pointer-events", "none");
  const { scale, tx, ty } = _mmTransform(g);
  clone.setAttribute("transform", `translate(${tx},${ty}) scale(${scale})`);
  mmSvg.appendChild(clone);
}

// Canvas mode: draw a simplified dot-and-line representation (better for large graphs).
function _buildMinimapCanvas(g) {
  const canvas = document.getElementById("minimap-canvas");
  canvas.width  = MINIMAP_W;
  canvas.height = MINIMAP_H;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, MINIMAP_W, MINIMAP_H);

  const { scale, tx, ty } = _mmTransform(g);
  const dark = document.body.classList.contains("dark");

  // Edges
  ctx.strokeStyle = dark ? "#3a5888" : "#6690cc";
  ctx.lineWidth   = 0.5;
  for (const eid of g.edges()) {
    const pts = (g.edge(eid) || {}).points;
    if (!pts || pts.length < 2) continue;
    ctx.beginPath();
    ctx.moveTo(pts[0].x * scale + tx, pts[0].y * scale + ty);
    for (let i = 1; i < pts.length; i++) {
      ctx.lineTo(pts[i].x * scale + tx, pts[i].y * scale + ty);
    }
    ctx.stroke();
  }

  // Nodes (skip cluster pseudo-nodes which have clusterLabelPos set)
  for (const nid of g.nodes()) {
    const n = g.node(nid);
    if (!n || n.clusterLabelPos !== undefined) continue;
    const x = (n.x - (n.width  || 0) / 2) * scale + tx;
    const y = (n.y - (n.height || 0) / 2) * scale + ty;
    const w = (n.width  || 0) * scale;
    const h = (n.height || 0) * scale;
    const isExt = n.class && n.class.includes("external");
    ctx.fillStyle   = isExt ? (dark ? "#1e1e1e" : "#f0f0f0")
                            : (dark ? "#162540" : "#dbe9f4");
    ctx.strokeStyle = isExt ? (dark ? "#555555" : "#aaaaaa")
                            : (dark ? "#4070b0" : "#4f8ef7");
    ctx.lineWidth   = 0.5;
    _canvasRoundRect(ctx, x, y, w, h, 2);
    ctx.fill();
    ctx.stroke();
  }
}

// Update the semi-transparent viewport rectangle on the minimap overlay.
function _updateMinimapViewport(g) {
  const { scale, tx, ty } = _mmTransform(g);
  const t  = d3.zoomTransform(svgEl);
  const W  = svgEl.clientWidth;
  const H  = svgEl.clientHeight;

  // Visible area in graph-coordinate space
  const vx = -t.x / t.k;
  const vy = -t.y / t.k;
  const vw =  W   / t.k;
  const vh =  H   / t.k;

  // Map to minimap pixels
  const vp = document.getElementById("minimap-viewport");
  vp.setAttribute("x",      vx * scale + tx);
  vp.setAttribute("y",      vy * scale + ty);
  vp.setAttribute("width",  vw * scale);
  vp.setAttribute("height", vh * scale);
}

// Wire up click/drag on the minimap overlay to pan the main view.
function _setupMinimapInteraction(g) {
  const { scale, tx, ty } = _mmTransform(g);
  const overlayEl = document.getElementById("minimap-overlay");

  function panTo(clientX, clientY) {
    const rect = overlayEl.getBoundingClientRect();
    const mx   = clientX - rect.left;
    const my   = clientY - rect.top;
    // Minimap pixel → graph coordinate
    const gx   = (mx - tx) / scale;
    const gy   = (my - ty) / scale;
    // Keep current zoom level, re-center main view on (gx, gy)
    const t    = d3.zoomTransform(svgEl);
    const nx   = svgEl.clientWidth  / 2 - gx * t.k;
    const ny   = svgEl.clientHeight / 2 - gy * t.k;
    svg.call(zoom.transform, d3.zoomIdentity.translate(nx, ny).scale(t.k));
  }

  // Replace previous listeners by re-selecting the element each render
  const overlay = d3.select(overlayEl);
  overlay.on("click", function() {
    panTo(d3.event.clientX, d3.event.clientY);
  });
  overlay.call(
    d3.drag()
      .on("start", () => d3.event.sourceEvent.stopPropagation())
      .on("drag",  () => panTo(d3.event.sourceEvent.clientX,
                               d3.event.sourceEvent.clientY))
  );
}

// Entry point called after each render.
function updateMinimap(g) {
  lastGraph = g;
  const container = document.getElementById("minimap-container");
  const mmSvg     = document.getElementById("minimap-svg");
  const mmCanvas  = document.getElementById("minimap-canvas");

  if (!g.graph().width || !g.graph().height) {
    container.hidden = true;
    return;
  }

  container.hidden = false;
  const useCanvas = graphData.nodes.length >= CANVAS_THRESHOLD;

  // Show the active layer, hide the other
  mmSvg.style.display    = useCanvas ? "none" : "";
  mmCanvas.style.display = useCanvas ? ""     : "none";

  if (useCanvas) {
    _buildMinimapCanvas(g);
  } else {
    _buildMinimapSVG(g);
  }

  _updateMinimapViewport(g);
  _setupMinimapInteraction(g);
}

// ── Preloaded project (optional --project CLI flag) ───────────────────────────
// If the server was started with a project path, fetch and render it immediately.
(async () => {
  try {
    const res = await fetch("/api/preloaded");
    if (!res.ok) return;
    const data = await res.json();
    if (data && data.nodes) {
      graphData = data;
      collapsedNodes.clear();
      showGraph();
    }
  } catch (_) {}
})();
