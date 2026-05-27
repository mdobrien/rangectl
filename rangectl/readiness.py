from rangectl.types import ReadinessProbe


def port_open(port: int, timeout: int = 300) -> ReadinessProbe:
    return ReadinessProbe(probe_type="port", target=port, timeout=timeout)


def ping(timeout: int = 300) -> ReadinessProbe:
    return ReadinessProbe(probe_type="ping", timeout=timeout)


def process_running(name: str, timeout: int = 300) -> ReadinessProbe:
    return ReadinessProbe(probe_type="process", target=name, timeout=timeout)


def command_succeeds(cmd: str, timeout: int = 300) -> ReadinessProbe:
    return ReadinessProbe(probe_type="command", target=cmd, timeout=timeout)
