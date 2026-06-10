# Feature: Topology Diagram Generation — Options Survey
**Created**: 2026-06-10
**Status**: Complete (Option A implemented)

## Related Issues
- **CLI**: `20260529-11-phase14-cli.md` — command conventions this slots into (backlink added)
- **Net summary**: `cmd_net` in `rangectl/cli.py:261` — existing text version of this feature
- **API**: `20260527-3-sdk-api-reference.md` — SDK surface
- **VLAN**: `20260609-4-phase25-vlan-support.md` — vlan_a/vlan_b data the diagram should show

## Goal
`rangectl diagram <range>` (and/or SDK `range.diagram()`) renders a picture of an arbitrary
topology: node name, OS type, interface names, IPs. Switches/hubs as distinct shapes, links with
per-end iface/IP labels.

## Data — already solved, two sources
1. **Deployed range (CLI path)**: StateDB has everything — `nodes` (name, os_type, mgmt_ip,
   state), `links` (node_a/iface_a/ip_a, node_b/iface_b/ip_b, bridge_name, is_up, vlan_a/vlan_b),
   `bridges` (bridge_type, vlan_aware). `Range.connect(name)` → read → render. No live kernel
   queries needed.
2. **Undeployed Topology (SDK/YAML path)**: same fields exist on the `Topology` object pre-deploy.
   Build one shared intermediate (nodes + edges + labels) so both sources feed one renderer.

Model note: a VM↔VM link is an edge; a switch/hub is a **node** whose links fan out (star), not an
edge label. VLAN config belongs on the port end of each edge.

## Rendering Options

### A. Graphviz `dot` via subprocess — **recommended**
Emit DOT text, shell out to `dot -Tsvg/-Tpng`.
- **Deps**: none in Python; `graphviz` apt/brew package (already on most dev boxes; EC2 apt-get).
- **Quality**: the standard for this exact job. HTML-like labels give per-node tables
  (name / os / iface:ip rows); `headlabel`/`taillabel` put iface+IP at each edge end; shapes per
  os_type (router=diamond, switch=box, hub=ellipse, container=component); color per VLAN/subnet.
- **Layout**: `dot` (hierarchical), `neato`/`fdp` (spring) — good up to ~50 nodes with zero effort.
- **Effort**: ~150 lines (DOT emitter is string formatting; trivially unit-testable on MockBackend
  topologies — assert on DOT text, no kernel, no binary).
- **Degradation**: binary missing → still write `.dot` + print render hint. `--format dot` is free.
- **Risk**: edge-end labels can crowd on dense graphs; mitigate by putting iface/IP rows in node
  tables instead of edge ends (toggle).

### B. `graphviz` Python package
Same engine, nicer API. Adds a pip dep for what string-building does fine. Not worth it.

### C. NetworkX + matplotlib
- **Deps**: two pip packages (pure Python, no system binary).
- **Quality**: layouts OK (spring/kamada-kawai) but label placement is manual and mediocre for
  edge-end labels; node tables require custom drawing. Looks "plotted", not "diagrammed".
- **Effort**: more code than A for worse output. Only attractive if a hard no-system-deps rule.

### D. Hand-rolled SVG (zero deps)
Own layout (layered: routers top, switches mid, hosts bottom) + write SVG text.
- **Quality ceiling high**, deterministic, fully styleable; pairs with the existing capstone
  aesthetic. But layout is the hard part of this problem and graphviz already solved it.
- **Effort**: 400+ lines and growing. Justifiable later for the polished-doc use case, not v1.

### E. D2 (terrastruct)
Modern, prettiest output, simple text language. External Go binary, less ubiquitous than
graphviz. Same architecture as A (emit text, shell out) — could be an alternate `--format d2`
emitter later, cheap to add.

### F. Mermaid text
Emit `.mmd`; renders in GitHub/markdown viewers without us rendering anything. PNG/SVG locally
requires mermaid-cli (node.js — heavy). Weak edge-end label support. Good as a *secondary*
`--format mermaid` for README embedding, not the primary.

### G. Interactive HTML (vis-network / cytoscape.js, single file)
Write one self-contained `.html` with inline vanilla JS + embedded topology JSON; open in browser.
Pan/zoom/drag, tooltips with full iface tables — best for big ranges. Aligns with the project's
vanilla-JS UI stance. No picture *file* though (screenshot manually). Strong **phase-2 companion**
(`--format html`), not the v1 "picture" ask.

### H. `diagrams` pip package
Icon-style cloud diagrams; wraps graphviz anyway, icon set wrong for L2 labs. No.

### I. ASCII in terminal
`rangectl net` already covers the quick-look need; ASCII art layout for arbitrary graphs is
poor ROI. Skip.

## Comparison

| Option | Deps | Output | Edge-end iface/IP labels | Effort | Verdict |
|---|---|---|---|---|---|
| A dot subprocess | system pkg | SVG/PNG/PDF | yes (head/taillabel) | S | **v1** |
| B graphviz-py | pip+system | same as A | yes | S | redundant |
| C networkx+mpl | 2 pip | PNG | manual, poor | M | no |
| D hand SVG | none | SVG | yes (own layout) | L | later, maybe |
| E D2 | Go binary | SVG/PNG | partial | S | later `--format` |
| F mermaid | none (emit) | .mmd | weak | XS | later `--format` |
| G HTML interactive | none | .html | tooltips | M | phase 2 |
| I ASCII | none | terminal | no | M | skip |

## Recommended shape (Option A, v1)
- `rangectl diagram <range> [-o topo.svg] [--format svg|png|dot] [--mgmt] [--engine dot|neato]`
- SDK: `range.diagram(path)` / `topology.diagram(path)` (works pre-deploy too).
- New module `rangectl/diagram.py`: `build_graph(nodes, links, bridges) -> DOT str` (pure
  function) + thin `render(dot_text, fmt, out)` subprocess wrapper (tolerant if binary absent).
- Render rules: node = HTML-like table (name, os_type badge, iface:ip rows incl. mgmt if
  `--mgmt`); L2 nodes shaped/colored by switch|hub + `vlan-aware` tag; edge ends labeled
  `iface\nip` when not folded into tables; VLAN access/trunk annotated on the port end; dashed
  red edge when `is_up=0`; subnet-consistent edge colors.
- Gate 1: DOT-emitter unit tests on Mock topologies (string assertions). Gate 2: one EC2 test —
  deploy Topo-style range, run CLI, assert file exists + `dot` exit 0.

## Steps (once an option is picked)
- [x] Decide v1 option + flag surface (this issue) — **Option A picked**
- [x] `diagram.py` emitter + unit tests (`tests/unit/test_diagram.py`, 17 tests)
- [x] CLI command + SDK method (`rangectl diagram`, `Topology.diagram`, `Range.diagram`)
- [x] Diagrams for every integration-test topology
  (`scratch/scripts/generate_test_topology_diagrams.py` → `scratch/capstone/diagrams/`)
- [ ] Gate 2 smoke test (deferred — next EC2 batch)
- [ ] (later) `--format html` interactive companion (G), `--format mermaid`/`d2` emitters

## Progress Log
- 2026-06-10: options surveyed; StateDB confirmed to already hold all required fields
  (`state.py:22-66`). Recommendation: A (DOT emit + dot subprocess), G as phase 2.
- 2026-06-10: Option A implemented.
  - `rangectl/diagram.py`: `build_dot(topology, include_mgmt=False)` pure emitter +
    `render(dot_text, out, fmt)` subprocess wrapper (missing binary → writes `.dot`
    fallback + clear RuntimeError; `--format dot` needs no binary).
  - Interface/IP data is collected from BOTH `node._interfaces` (definition path) and
    link endpoint specs (`Range.connect` path doesn't populate `_interfaces`); VLAN
    config likewise read from `PortSpec.vlan` with fallback to `link._endpoints[i].vlan`.
    One renderer serves YAML/SDK definitions and reconstructed deployed ranges.
  - Edge ends carry iface name only (IPs live in node tables, per the crowding
    mitigation noted above); L2 port ends add `access(N)` / `trunk(...) native N`.
    Down links render dashed red. Switch=box, hub=ellipse, VM=rounded table, colors
    keyed by os_type (vyos orange, linux blue, container green, l2 grays).
  - SDK: `Topology.diagram(path, fmt, include_mgmt)`; `Range.define()` (idempotent
    declarative-phase-only helper) + `Range.diagram(...)` work pre-deploy.
    `Range.deploy()` now routes through `define()` so diagram-then-deploy is safe.
  - CLI: `rangectl diagram <range> | --file topo.yaml [-o out] [--format svg|png|dot]
    [--mgmt]` (`cmd_diagram` in `cli.py`).
  - Gate 1: full unit suite green (532 passed).
  - All 23 integration-test topologies rendered to svg+png in
    `scratch/capstone/diagrams/`. Re-declared (inline in test fns, not importable):
    topo7, nstwo, nsvyos, nsmix. Skipped as exact shape duplicates: ns_regression
    nstopo3/4/5 (= topo3/4/5) and the trivial a-b pairs (nsfreeze, nsinet*, nsres,
    _pair — all = topo1 shape). The `_topoN`/`RasLab`-style routers declared via
    `image="vyos"` only get their os_type patched to vyos in the script
    (definition-time os defaults to linux; the engine resolves it from the image
    registry at deploy).

## Resolution
Option A shipped: `rangectl/diagram.py` (pure DOT emitter + tolerant `dot` subprocess
wrapper), `Topology.diagram`/`Range.diagram` SDK methods (work on undeployed
definitions), `rangectl diagram` CLI (deployed range OR `--file` YAML), 17 Gate-1
unit tests, and rendered diagrams of all integration-test topologies. Gate 2 smoke
test and the phase-2 emitters (html/mermaid/d2) remain follow-ups.
