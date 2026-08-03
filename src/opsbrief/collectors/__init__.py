from opsbrief.collectors.audit import collect as collect_audit
from opsbrief.collectors.backup import collect as collect_backup
from opsbrief.collectors.gke_inventory import collect as collect_gke_inventory
from opsbrief.collectors.kubernetes_health import collect as collect_kubernetes_health
from opsbrief.collectors.logging import collect as collect_logging
from opsbrief.collectors.mesh import collect as collect_mesh
from opsbrief.collectors.monitoring import collect as collect_monitoring
from opsbrief.collectors.network import collect as collect_network
from opsbrief.collectors.preflight import collect as collect_preflight
from opsbrief.collectors.prometheus_monitoring import collect as collect_prometheus_monitoring
from opsbrief.collectors.services import collect as collect_services
from opsbrief.collectors.trend_metrics import collect as collect_trend_metrics

COLLECTORS = {
    "preflight": collect_preflight,
    "gke_inventory": collect_gke_inventory,
    "kubernetes_health": collect_kubernetes_health,
    "monitoring": collect_monitoring,
    "prometheus_monitoring": collect_prometheus_monitoring,
    "logging": collect_logging,
    "audit": collect_audit,
    "network": collect_network,
    "mesh": collect_mesh,
    "trend_metrics": collect_trend_metrics,
    "backup": collect_backup,
    "services": collect_services,
}
