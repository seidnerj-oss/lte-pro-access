from lte_pro_access.config import AtRelayConfig, Config

LTE_SOCAT = "/tmp/socat"
QMI_ABSTRACT_SOCKET = "qmi-proxy"
# TCP keepalive on the Pi relay's connect leg: a leg left half-open by an LTE reboot errors within about a minute,
# the socat child exits and qmicli reconnects (qmicli itself only reads, so no RST would ever reach it).
PI_RELAY_KEEPALIVE = "keepalive,keepidle=30,keepintvl=10,keepcnt=3"


def lte_qmi_relay_cmd(cfg: Config) -> str:
    return (
        f"{LTE_SOCAT} TCP-LISTEN:{cfg.qmi_port},reuseaddr,fork,range={cfg.pi_address}/32 "
        f"ABSTRACT-CONNECT:{QMI_ABSTRACT_SOCKET}"
    )


def lte_at_relay_cmd(cfg: Config, at_relay: AtRelayConfig) -> str:
    return (
        f"{LTE_SOCAT} -T {at_relay.idle_seconds} "
        f"TCP-LISTEN:{at_relay.port},reuseaddr,fork,max-children=1,range={cfg.pi_address}/32 "
        f"OPEN:{at_relay.device},raw,echo=0"
    )


def pi_qmi_relay_argv(cfg: Config) -> list[str]:
    return [
        str(cfg.socat_pi_binary),
        f"ABSTRACT-LISTEN:{QMI_ABSTRACT_SOCKET},fork",
        f"TCP:{cfg.lte_host}:{cfg.qmi_port},{PI_RELAY_KEEPALIVE}",
    ]
