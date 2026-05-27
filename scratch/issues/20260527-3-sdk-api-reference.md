# rangectl — SDK API Reference
**Created**: 2026-05-27
**Related Issues**: `20260527-1-vm-testbed-platform-design.md`, `20260527-2-requirements-and-design-decisions.md`

---

## Topology

The root object. Declares nodes, links, and drives deployment.

```python
from testbed import Topology

topo = Topology("pentest-lab")
```

### `Topology(name: str)`
Create a named topology. Name prefixes all resources (VMs, bridges, volumes).

### `topo.node(name, image, vcpu=1, memory=1024, os="linux", depends_on=None, ready_when=None) -> Node`
Declare a node in the topology.

```python
router = topo.node("router", image="vyos-1.4", vcpu=2, memory=2048)
target = topo.node("target", image="ubuntu-22.04",
                   depends_on=[router],
                   ready_when=port_open(22))
```

| Param | Type | Default | Description |
|---|---|---|---|
| name | str | required | Unique name within topology |
| image | str | required | Image name from registry |
| vcpu | int | 1 | Virtual CPUs |
| memory | int | 1024 | Memory in MB |
| os | str or OSType | "linux" | "linux" or "windows" |
| depends_on | list[Node] | None | Nodes that must be ready before this one starts |
| ready_when | ReadinessProbe | None | L3 readiness check. Default is L2 (ping) |

### `topo.link(if_a, if_b) -> Link`
Declare a link between two node interfaces with static IPs.

```python
topo.link(router.eth0["10.0.1.1/24"], target.eth0["10.0.1.2/24"])
```

Each side is a node interface with an IP/CIDR bound via `[]` syntax. A linux bridge is created per link.

### `topo.deploy(cleanup_on_fail=True) -> Range`
Deploy the topology. Returns a `Range` context manager.

```python
with topo.deploy() as rng:
    rng["target"].exec("whoami")
```

Steps performed by the engine:
1. Validate host resources (vCPU, memory, disk)
2. Allocate mgmt subnet (next /24 from pool)
3. Create mgmt bridge (`testbed-mgmt-{name}`)
4. Compute dependency waves from `depends_on`
5. Deploy waves — nodes in each wave start in parallel, block until all pass readiness
6. Wire topology links (create bridge + tap for each link)
7. Run dependency injection on each node

On failure: destroys all created resources if `cleanup_on_fail=True`. Set to `False` to leave nodes up for debugging.

### `topo.export(path: str)`
Export topology to YAML. Works without deploying.

```python
topo.export("topology.yaml")
```

Output contains: nodes (name, image, vcpu, memory, os, depends_on), interfaces (name, ip, cidr), links (node_a/iface_a <-> node_b/iface_b), dependency specs.

### `Topology.from_yaml(path: str) -> Topology`
Reconstruct a topology from a previously exported YAML file.

```python
topo = Topology.from_yaml("topology.yaml")
```

### `topo.destroy()`
Destroy all resources: VMs, bridges, overlays, mgmt bridge. Frees mgmt subnet. Called automatically when `Range` context manager exits.

---

## Node

Returned by `topo.node()`. Declares interfaces, dependencies, and configuration.

### Interface access: `node.ethN`
Access interfaces by name. Returns an `InterfaceSpec`.

```python
router.eth0  # InterfaceSpec(node_name="router", interface_name="eth0")
```

### IP binding: `node.ethN["ip/cidr"]`
Bind an IP to an interface. Returns a new `InterfaceSpec` with the IP set.

```python
router.eth0["10.0.1.1/24"]  # InterfaceSpec with ip="10.0.1.1", cidr="24"
```

### `node.ethN.ip`
Access the static IP assigned to an interface. Avairngle after declaring a link.

```python
topo.link(router.eth0["10.0.1.1/24"], target.eth0["10.0.1.2/24"])

@target.configure
def setup(node):
    gateway = router.eth0.ip  # "10.0.1.1"
```

### Dependency methods

All dependency methods are available on both `Node` and `DependencySet`. Execution order: packages -> custom installs -> configure functions -> services.

#### `node.packages(packages: list[str])`
Declare packages to install. The engine resolves the package manager from the image/OS (apt, yum, choco, etc). User doesn't specify how — just what.

```python
target.packages(["nginx", "curl", "net-tools"])
dc.packages(["wireshark", "nmap"])  # windows — engine uses choco
```

#### `node.powershell(command: str)`
Run a PowerShell command (Windows).

```python
dc.powershell("Install-WindowsFeature AD-Domain-Services")
```

#### `node.install(name, src, install_cmd, verify_cmd=None)`
Install custom software from a local archive.

```python
target.install(
    name="custom-agent",
    src="./builds/agent-v2.tar.gz",
    install_cmd="./install.sh",
    verify_cmd="agent --version"
)
```

| Param | Type | Description |
|---|---|---|
| name | str | Human-readable name (for logging) |
| src | str | Local path to archive/binary — uploaded to node |
| install_cmd | str | Command to run after upload |
| verify_cmd | str or None | Command to verify install succeeded |

#### `node.file(dst, src)`
Upload a file to the node.

```python
target.file("/etc/nginx/conf.d/app.conf", src="./configs/app.conf")
```

#### `node.user(name, ssh_key=None, password=None)`
Create a user on the node.

```python
target.user("admin", ssh_key="~/.ssh/id_rsa.pub")
```

#### `node.run_on_boot(command: str)`
Register a command to run on boot.

```python
target.run_on_boot("systemctl enable nginx")
```

#### `node.service(name, enabled=False, start_cmd=None, ready_when=None)`
Declare a service.

```python
target.service("nginx", enabled=True)
target.service("custom-agent",
    start_cmd="/opt/agent/bin/start",
    ready_when=port_open(8080))
```

| Param | Type | Description |
|---|---|---|
| name | str | Service name |
| enabled | bool | Start on boot |
| start_cmd | str or None | Custom start command (if not a systemd service) |
| ready_when | ReadinessProbe or None | Probe to confirm service is up |

#### `@node.configure`
Register a Python function to run after deps install, before services start.

```python
@target.configure
def setup_app(node):
    node.exec("mkdir -p /opt/app")
    node.upload("./app/", "/opt/app/")
    gateway = router.eth0.ip
    node.template("config.j2", "/opt/app/config.yaml",
                  vars={"gateway": gateway})
```

The function receives a `LiveNode` handle with `exec()`, `upload()`, and `template()`.

#### `node.apply(dep_set: DependencySet)`
Apply a reusable dependency set to the node.

```python
target.apply(web_stack)
target.apply(monitoring)
```

---

## DependencySet

Reusable, composable collection of dependencies. Same methods as Node (packages, powershell, install, file, user, run_on_boot, service, configure).

```python
from testbed import DependencySet

web_stack = DependencySet("web-stack")
web_stack.packages(["nginx", "certbot", "flask", "gunicorn"])
web_stack.service("nginx", enabled=True)

monitoring = DependencySet("monitoring")
monitoring.install(name="node-exporter", src="./builds/node-exp.tar.gz",
                   install_cmd="./install.sh")
monitoring.service("node-exporter",
    start_cmd="/usr/local/bin/node_exporter",
    ready_when=port_open(9100))

@monitoring.configure
def setup_monitoring(node):
    node.file("/etc/node-exporter/config.yaml", src="./configs/monitoring.yaml")
```

### `DependencySet(name: str, os: OSType = "linux")`
Create a named dependency set. Optional `os` param for platform-specific sets.

```python
ad_controller = DependencySet("active-directory", os="windows")
ad_controller.packages(["rsat-ad-tools"])
ad_controller.powershell("Install-WindowsFeature AD-Domain-Services")
```

---

## Range

Live handle to a deployed topology. Returned by `topo.deploy()`.

```python
with topo.deploy() as rng:
    rng["target"].exec("whoami")
    rng.snapshot("baseline")
```

### `rng[node_name] -> LiveNode`
Access a running node by name.

### `rng.link(node_a: str, node_b: str) -> Link`
Access a link between two nodes for toggling.

```python
rng.link("router", "target").down()  # simulate link failure
rng.link("router", "target").up()    # restore
```

### `rng.snapshot(name: str)`
Snapshot all nodes in the topology.

### `rng.restore(name: str)`
Restore all nodes to a named snapshot.

### `rng.logs(level=None) -> list[dict]`
Fetch structured logs for the entire topology.

```python
rng.logs()                # all logs
rng.logs(level="error")   # errors only
```

Log entries contain: `topology_name`, `node_name`, `level`, `message`, `timestamp`.

---

## LiveNode

Handle to a running node. Accessed via `rng["node_name"]`.

### `live_node.mgmt_ip -> str`
Management network IP for this node.

### `live_node.exec(command: str) -> ExecResult`
Run a command on the node over SSH (via mgmt network).

```python
result = rng["target"].exec("cat /etc/hostname")
print(result.stdout)
print(result.exit_code)
```

### `live_node.upload(src: str, dst: str)`
Upload a file or directory to the node via SFTP.

```python
rng["target"].upload("./payloads/", "/tmp/payloads/")
```

### `live_node.template(src, dst, vars=None)`
Render a Jinja2 template and upload the result.

```python
rng["target"].template("config.j2", "/opt/app/config.yaml",
                       vars={"db_host": "10.0.2.3", "port": 5432})
```

### `live_node.logs(level=None) -> list[dict]`
Fetch structured logs for this node only.

### `live_node.snapshot(name: str)`
Snapshot this individual node.

### `live_node.restore(name: str)`
Restore this individual node to a named snapshot.

---

## Link

Handle to a link between two nodes.

### `link.down()`
Bring the link down (disable the bridge/tap).

### `link.up()`
Bring the link back up.

---

## ExecResult

```python
@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
```

---

## Readiness Probes

Factory functions that return `ReadinessProbe` objects. Used in `ready_when` on nodes and services.

```python
from testbed import port_open, ping, process_running, command_succeeds
```

### `port_open(port: int, timeout=300) -> ReadinessProbe`
Wait until a TCP port is accepting connections (checked over mgmt network).

### `ping(timeout=300) -> ReadinessProbe`
Wait until the node responds to ICMP ping on mgmt interface. This is the default L2 check.

### `process_running(name: str, timeout=300) -> ReadinessProbe`
Wait until a named process is running on the node.

### `command_succeeds(cmd: str, timeout=300) -> ReadinessProbe`
Wait until a command exits with code 0.

All probes have `timeout` (max seconds to wait) and check every 5 seconds.

---

## ImageRegistry

Manages local image storage.

```python
from testbed import ImageRegistry

registry = ImageRegistry()
registry.add("ubuntu-22.04", "./ubuntu-22.04.qcow2", inject="cloud-init")
registry.add("win-server-2022", "./win2022.qcow2", inject="cloudbase-init")
registry.add("my-custom", "./custom.qcow2")  # default: pre-baked

images = registry.list()
registry.remove("old-image")
```

### `ImageRegistry(storage_path="~/.testbed/images")`
Initialize the registry. Images are stored as qcow2 files with metadata.

### `registry.add(name, path, inject="pre-baked")`
Register an image.

| inject value | When to use |
|---|---|
| `pre-baked` (default) | Image already has SSH key + guest agent (e.g. ImageBuilder output) |
| `cloud-init` | Linux images with cloud-init (Ubuntu, Debian, Fedora, Kali, VyOS) |
| `cloudbase-init` | Windows images with cloudbase-init |
| `guest-agent` | Images with QEMU guest agent only — explicit opt-in |

### `registry.list() -> list[ImageInfo]`
List all registered images.

### `registry.remove(name: str)`
Remove an image from the registry.

### `registry.get(name: str) -> ImageInfo`
Get metadata for a specific image.

### `registry.exists(name: str) -> bool`
Check if an image is registered.

---

## ImageBuilder

Build custom images from a base. Uses boot-and-snapshot: boots base, applies changes, shuts down, snapshots.

Has the same dependency methods as Node/DependencySet (apt, pip, install, file, user, configure, etc). Also supports chained syntax.

```python
from testbed import ImageBuilder

web_server = (ImageBuilder("ubuntu-22.04")
    .packages(["nginx", "curl", "htop"])
    .file("/etc/nginx/conf.d/app.conf", src="./configs/app.conf")
    .run("systemctl enable nginx")
    .build("my-web-server:v1"))
```

### `ImageBuilder(base_image: str)`
Start from a base image (must exist in registry).

### `.packages(packages: list[str]) -> ImageBuilder`
Declare packages to install. Chainable.

### `.run(command: str) -> ImageBuilder`
Run a command during build. Chainable.

### `.build(name: str) -> str`
Build the image: boot base, apply all deps/files/commands, snapshot, register in registry as `pre-baked`. Returns the image name.

All dependency methods from DependencyMixin also work (packages, file, user, install, configure, service) but are not chainable — use them in statement form:

```python
builder = ImageBuilder("ubuntu-22.04")
builder.packages(["nginx", "curl", "flask"])
builder.user("admin", ssh_key="~/.ssh/id_rsa.pub")
builder.file("/opt/app/config.yaml", src="./config.yaml")

@builder.configure
def setup(node):
    node.exec("mkdir -p /opt/app/data")

builder.build("my-app-image:v1")
```

---

## list_topologies

```python
from testbed import list_topologies

for topo in list_topologies():
    print(f"{topo['name']}: {topo['status']}")
```

Returns a list of dicts with topology state from the database.

---

## Full Example

```python
from testbed import Topology, DependencySet, ImageBuilder, port_open

# reusable dep sets
web_stack = DependencySet("web-stack")
web_stack.packages(["nginx", "certbot", "flask", "gunicorn"])
web_stack.service("nginx", enabled=True)

monitoring = DependencySet("monitoring")
monitoring.install(name="node-exporter", src="./builds/node-exp.tar.gz",
                   install_cmd="./install.sh")
monitoring.service("node-exporter",
    start_cmd="/usr/local/bin/node_exporter",
    ready_when=port_open(9100))

# topology
topo = Topology("pentest-lab")

router = topo.node("router", image="vyos-1.4", vcpu=2, memory=2048)
attacker = topo.node("kali", image="kali-2024", vcpu=4, memory=4096)
target = topo.node("target", image="ubuntu-22.04", memory=2048,
                   depends_on=[router],
                   ready_when=port_open(22))

target.apply(web_stack)
target.apply(monitoring)

target.install(name="vuln-app", src="./builds/vuln-app.tar.gz",
               install_cmd="./install.sh",
               verify_cmd="curl -s localhost:5000/health")

@target.configure
def setup_target(node):
    gateway = router.eth1.ip
    node.template("./templates/network.j2", "/etc/network/interfaces.d/static",
                  vars={"gateway": gateway})
    node.exec("systemctl restart networking")

topo.link(router.eth0["10.0.1.1/24"], attacker.eth0["10.0.1.2/24"])
topo.link(router.eth1["10.0.2.1/24"], target.eth0["10.0.2.2/24"])

# export without deploying
topo.export("pentest-lab.yaml")

# deploy and interact
with topo.deploy() as rng:
    print(rng["target"].mgmt_ip)          # 192.168.100.2
    print(rng["target"].exec("hostname")) # ExecResult(exit_code=0, stdout="target", ...)

    rng["target"].exec("systemctl start vuln-app")
    rng["attacker"].exec("nmap -sV 10.0.2.2")

    rng.link("router", "target").down()
    rng["attacker"].exec("ping -c 3 10.0.2.2")  # should fail
    rng.link("router", "target").up()

    rng.snapshot("post-attack")

    # check logs
    for entry in rng["target"].logs(level="error"):
        print(entry["timestamp"], entry["message"])
```
