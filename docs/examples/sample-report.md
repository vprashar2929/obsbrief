# OpsBrief Weekly Operational Report (sample)

This report is designed for platform operations review.
It starts with a stakeholder review, then provides detailed evidence in an appendix.

- Generated At (UTC): 2026-01-15T09:30:00+00:00
- Overall Assessment Result: Action Required
- Organization: OpsBrief

Assessment result definitions:

| Assessment Result | Meaning |
| --- | --- |
| No Findings | Evidence was available and no configured warning or critical finding was found. |
| Needs Review | Evidence shows one or more warning-level findings that should be reviewed. |
| Action Required | A critical finding was found, or required evidence could not be collected. |
| Not Assessed | Check does not apply to this component/environment, or a prerequisite is absent. |

## Executive Summary

- Environment: `sample`
- Cluster `sample-cluster` needs review: unhealthy workloads=1, pod issues=1, pod waiting reasons=sample_key_1:1; assessment result Needs Review.
- Cluster `sample-cluster` needs review: unhealthy workloads=1, pod issues=0, pod waiting reasons=sample_key_1:1; assessment result Needs Review.
- Monitoring stack scope `sample/sample-cluster` observed `1` scored HPA failure condition series in the 7-day window and `1` currently down monitoring target(s).
- Monitoring stack scope `sample/sample-cluster` observed `1` scored HPA failure condition series in the 7-day window and `1` currently down monitoring target(s).
- Project `sample-project` has `1` high-risk audit event(s) out of `1+` activity events reviewed in the last 7 days.
- Project `sample-project` observed resource usage threshold crossings: Cloud SQL=1, VM CPU=1, Kubernetes nodes=1.

## Infrastructure Health Overview

- Overall Assessment Result: Action Required

### Critical Findings

- Resource Usage Trends: Cloud SQL utilization crossed the review threshold; GKE node utilization crossed the review threshold; Kafka utilization crossed the review threshold. Evidence: 1 Cloud SQL high-utilization finding; 1 VM high-CPU finding; 1 Kubernetes node utilization observation; highest observed Cloud SQL CPU 1.0%, memory 1.0%, disk 1.0%; highest observed VM CPU 1.0%. Where to look: [Capacity Evidence](#infrastructure-capacity-and-managed-services).

### Warning Findings

- Platform Availability: sample-cluster: pod waiting (sample_key_1:1), 1 pod issue, 1 unhealthy workload, 1 autoscaler at max replicas, 1 autoscaler failure condition; sample-cluster: pod waiting (sample_key_1:1), 1 unhealthy workload, 1 autoscaler at max replicas, 1 autoscaler failure condition. Evidence: 2 clusters; 2 clusters needing review; 2 unhealthy workloads; 0 CrashLoopBackOff occurrences. Where to look: [Runtime Evidence](#runtime-and-control-plane-health).
- Incidents & Alerts: autoscaling evidence needs review; monitoring target availability needs review. Evidence: 1 high-risk audit events; 2 HPA failure condition series (7-day); 2 down monitoring targets. Where to look: [Monitoring Evidence](#monitoring-and-alerting).
- Network: sample-project: 1 inactive VPC peering; in-cluster DNS resolution failed for 1 of 1 FQDN checks. Evidence: 1 project; 1 VPC network; 1 firewall rule; 1 inactive VPC peering; 1 DNS zone; 0 missing required DNS zones; in-cluster DNS checks failed 1/1. Where to look: [Network And DNS Evidence](#network-and-dns-posture).

### Healthy Areas

- Backup Coverage: 1 GKE backup plans; 2 Cloud SQL instances with backup enabled.
- Service Mesh: 2 clusters; 0 gateway readiness gaps; 2 remote cluster links synced (expected at least 2); 2 mesh API proxy checks; 4 successful Envoy proxy samples.
- Logging & Compliance: 1 logging sink; 0 projects with topic/subscription gaps.

### Not Assessed Areas

- None identified from collected evidence.

### Immediate Actions Required

| Area | Assessment Result | Immediate Action | Evidence Link |
| --- | --- | --- | --- |
| Resource Usage Trends | Action Required | Review threshold crossings and missing telemetry with capacity owners. | [Capacity Evidence](#infrastructure-capacity-and-managed-services) |
| Platform Availability | Needs Review | Review affected cluster, pod, and workload evidence with the owning team. | [Runtime Evidence](#runtime-and-control-plane-health) |
| Incidents & Alerts | Needs Review | Review active monitoring, autoscaling, and audit signals in the source systems. | [Monitoring Evidence](#monitoring-and-alerting) |
| Network | Needs Review | Review peering, DNS, firewall, and in-cluster DNS evidence for the affected scope. | [Network And DNS Evidence](#network-and-dns-posture) |

## Platform Inventory

High-level scope observed in this report.

| Scope Item | Observed In Report |
| --- | --- |
| In-scope projects | 1 project |
| Kubernetes clusters | 2 clusters |
| Kubernetes nodes | 2 nodes |
| Cloud SQL instances | 1 instance |
| Redis instances | 1 instance |
| Managed Kafka clusters | 1 cluster |
| Compute Engine instances | 16 instances |
| Standalone Compute VMs | 16 VMs |

## Autoscaling Scope

Clarifies which autoscaling controls are expected and how observed evidence is scored for this environment. Detailed HPA conditions remain in the technical appendix.

| Cluster | Workload HPA Policy | Workload HPA Observed | Platform / Istio HPA | Node Pool Autoscaling | Assessment |
| --- | --- | --- | --- | --- | --- |
| sample-cluster | Sample-Value | 1 observed; 1 at max replicas; 1 with issues | 1 observed; 1 at max replicas; 1 with issues | No node-pool autoscaling evidence | Matches environment policy. |
| sample-value | No Kubernetes HPA evidence | No Kubernetes HPA evidence | No platform HPA evidence | 1/1 node pools enabled; total range 1-1 nodes | Matches environment policy. |


## Technical Evidence Appendix

Detailed read-only collector evidence for platform engineers. Stakeholders should use the executive summary and health overview as the primary report view.

## Runtime And Control Plane Health

Access, cluster inventory, and Kubernetes runtime health.

### Access And API Readiness
- Summary: Sample collector result from anonymized evidence.
- Evidence: checks passed `31/31`

### GKE Cluster Inventory
- Summary: Sample collector result from anonymized evidence.

| Cluster | Project | Region | Release Channel | K8s Version | Node Pools | Private Nodes |
| --- | --- | --- | --- | --- | --- | --- |
| sample-value | sample-project | us-central1 | sample-value | sample-value | 1 | yes |
| sample-value | sample-project | us-central1 | sample-value | sample-value | 1 | yes |

### Kubernetes Runtime Health (Needs Review)
- Summary: Sample collector result from anonymized evidence.
- Reason: sample-cluster: pod waiting (sample_key_1:1), 1 pod issue, 1 unhealthy workload, 1 autoscaler at max replicas, 1 autoscaler failure condition; sample-cluster: pod waiting (sample_key_1:1), 1 unhealthy workload, 1 autoscaler at max replicas, 1 autoscaler failure condition

| Cluster | Nodes Ready | Total Pods | Unhealthy Workloads | Pods Waiting By Reason |
| --- | --- | --- | --- | --- |
| sample-cluster | 1/1 | 1 | 1 | sample_key_1:1 |
| sample-cluster | 1/1 | 1 | 1 | sample_key_1:1 |

### Pod Restart Diagnostics

- No CrashLoopBackOff or high-restart pods detected at collection time.

## Service Mesh (Istio)

Istio control-plane, gateway readiness, remote cluster sync, and proxy posture.

### Service Mesh Health
- Summary: Sample collector result from anonymized evidence.

| Cluster | Assessment | Ingress Gateway Ready | East-West Gateway Ready | Istiod Ready | Remote Cluster Sync |
| --- | --- | --- | --- | --- | --- |
| sample-cluster | No Findings | 1/1 | 1/1 | 1/1 | 1 synced; expected at least 1 |
| sample-cluster | No Findings | 1/1 | 1/1 | 1/1 | 1 synced; expected at least 1 |

Mesh Component Readiness:

- Proxy Envoy columns apply to Envoy-based gateway components. Istiod is shown with readiness and version because it is the mesh control plane.

| Cluster | Mesh Component | Ready Pods | Pod Names | Ready Pod Names | Version | Proxy Envoy State | Proxy Cluster ID | Proxy Network | Proxy Discovery Address | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample-cluster | Ingress Gateway | 1/1 | sample-value, sample-value | sample-value, sample-value | sample-value | sample-value | sample-cluster | sample-value | 192.0.2.10 |  |
| sample-cluster | East-West Gateway | 1/1 | sample-value, sample-value | sample-value, sample-value | sample-value | sample-value | sample-cluster | sample-value | 192.0.2.10 |  |
| sample-cluster | Istiod | 1/1 | sample-value, sample-value | sample-value, sample-value | sample-value | Not applicable | Not applicable | Not applicable | Not applicable | Istiod is the mesh control plane; proxy Envoy metadata is not applicable. |
| sample-cluster | Ingress Gateway | 1/1 | sample-value, sample-value, sample-value, sample-value, sample-value, sample-value | sample-value, sample-value, sample-value, sample-value, sample-value, sample-value | sample-value | sample-value | sample-cluster | sample-value | 192.0.2.10 |  |
| sample-cluster | East-West Gateway | 1/1 | sample-value, sample-value | sample-value, sample-value | sample-value | sample-value | sample-cluster | sample-value | 192.0.2.10 |  |
| sample-cluster | Istiod | 1/1 | sample-value, sample-value | sample-value, sample-value | sample-value | Not applicable | Not applicable | Not applicable | Not applicable | Istiod is the mesh control plane; proxy Envoy metadata is not applicable. |

Remote Cluster Sync:

| Cluster | Remote Cluster | Sync Status | Istiod | Evidence Status | Note |
| --- | --- | --- | --- | --- | --- |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |
| sample-cluster | sample-value | sample-value | sample-value | No Findings |  |

Mesh API Proxy Readiness:

| Cluster | Proxy Name | Proxy Type | Namespace | Assessment | Deployment Ready/Desired | Pods Ready/Total | Service Mode | Service Ports | Endpoint Ports | Ready Endpoints | Load Balancer Address | Configured Resources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample-cluster | sample-value | sample-value | sample-namespace | No Findings | 1/1 | 1/1 | sample-value | sample-value | https://service.example | 1 | sample-value | configmap present; cert configured; token configured | sample-value |
| sample-cluster | sample-value | sample-value | sample-namespace | No Findings | 1/1 | 1/1 | sample-value | sample-value | https://service.example | 1 | sample-value | configmap present; cert configured; token configured | sample-value |

Proxy Sync Status:

| Cluster | Proxy | Proxy Cluster | Istiod | Version | Sync State | Evidence Status |
| --- | --- | --- | --- | --- | --- | --- |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |
| sample-cluster | sample-value | sample-cluster | sample-value | sample-value | sample-value | No Findings |

Mesh API Proxy Control Plane Tunnel Evidence:

| Cluster | Proxy | Namespace | Evidence Status | Successful Tunnels | Failed Tunnels | Latest Healthy Target | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sample-cluster | sample-value | sample-namespace | No Findings | 1 | 1 | sample-value |  |
| sample-cluster | sample-value | sample-namespace | No Findings | 1 | 1 | sample-value |  |

## Monitoring And Alerting

GKE monitoring and alerting are provided by Prometheus/Grafana. GKE-native Cloud Monitoring alerting is out of scope for this report.

### Monitoring Stack (Needs Review)
- Reason: 2 HPA failure condition series in 7 days; 2 current HPA failure condition series; 2 currently scaling-limited HPA series; 2 down monitoring targets
- Metric Scope: `cluster` in `sample-cluster`, `sample-cluster`; `environment` in `sample`

| Environment | Cluster | Assessment | HPAs | Current HPA Failure Condition Series | Historical HPA Failure Condition Series (7-Day) | Currently Scaling-Limited Series | Historical Scaling-Limited Series (7-Day) | Monitoring Targets | Down Targets | Down Jobs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample | sample-cluster | Needs Review | 1 | 1 | 1 | 1 | 1 | 1 | 1 | sample-value:1 |
| sample | sample-cluster | Needs Review | 1 | 1 | 1 | 1 | 1 | 1 | 1 | sample-value:1 |

Monitoring Stack Metric Summary:

| Graph | Series | Latest | Min | Max | Avg |
| --- | --- | --- | --- | --- | --- |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |
| sample-value | sample-value | 1.00 | 1.00 | 1.00 | 1.00 |

### Monitoring Stack Trends

The charts below visualize key time-series metrics from the monitoring data source over the reporting window. They provide visual context for utilization, autoscaling behavior, and monitoring target health summarized in the tables above.

![sample-value](evidence/charts/monitoring-stack-sample-value.png)
- Series: `sample-value`, `sample-value`

![sample-value](evidence/charts/monitoring-stack-sample-value.png)
- Series: `sample-value`, `sample-value`

![sample-value](evidence/charts/monitoring-stack-sample-value.png)
- Series: `sample-value`, `sample-value`

![sample-value](evidence/charts/monitoring-stack-sample-value.png)
- Series: `sample-value`, `sample-value`

![sample-value](evidence/charts/monitoring-stack-sample-value.png)
- Series: `sample-value`, `sample-value`

![sample-value](evidence/charts/monitoring-stack-sample-value.png)
- Series: `sample-value`, `sample-value`

![sample-value](evidence/charts/monitoring-stack-sample-value.png)
- Series: `sample-value`, `sample-value`

## Change Audit

Configuration and security changes observed in audit logs.

### Configuration Change Audit
- Summary: Sample collector result from anonymized evidence.
- Focus: named ConfigMap, Secret, IAM, firewall, and route changes from audit logs. Secret values are never collected or displayed.

Configuration And Security Changes Requiring Review (Recent Sample):

- Routine leader-election, heartbeat, lease, service-proxy, autoscaler, and other system-maintenance records are hidden from this table.
- A trailing `+` means the targeted audit query hit its collection limit; counts are lower bounds for the 7-day window.

| Project | Time | Change | Resource Type | Namespace/Scope | Resource Name | Changed By |
| --- | --- | --- | --- | --- | --- | --- |
| No review-candidate changes found in targeted audit query |  |  |  |  |  |  |

Audit Coverage And Filtered Routine Activity:

| Project | Audit Events Scanned (7-Day) | Targeted Review Events Scanned | Review Candidate Changes | ConfigMap Changes | Secret Changes | High-Risk Security Events | Routine Events Hidden From View |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sample-project | 1+ | 1+ | 1+ | 1+ | 1+ | 1 | 1 |

## Backup And Recovery Posture

This section reports backup resources and backup configuration when backup assessment is in scope.

### Backup Discovery
- Summary: Sample collector result from anonymized evidence.

GKE Backup Plans:

| Project | Region | Assessment | Reason | Backup Plans Found |
| --- | --- | --- | --- | --- |
| sample-project | us-central1 | No Findings | Sample evidence generated from anonymized data. | 1 |

Cloud SQL Backup Configuration:

| Project | Assessment | Reason | Cloud SQL Instances | With Backups Enabled |
| --- | --- | --- | --- | --- |
| sample-project | No Findings | Sample evidence generated from anonymized data. | 1 | 2 |

Elasticsearch Backup Checks:

| Target | Assessment | Snapshot Check | Lifecycle Check | Note |
| --- | --- | --- | --- | --- |
| not configured | Not Assessed | n/a | n/a | Sample evidence generated from anonymized data. |

## Network And DNS Posture

Network topology and control-plane posture for in-scope projects.

### Network Inventory And DNS Controls (Needs Review)
- Summary: Sample collector result from anonymized evidence.
- Reason: sample-project: 1 inactive VPC peering; in-cluster DNS resolution failed for 1 of 1 FQDN checks

| Project | Assessment | VPCs | Firewall Rules | Disabled Firewall Rules | VPC Peerings | Inactive Peerings | Forwarding Rules | DNS Zones |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample-project | Needs Review | 1 | 1 | 1 | 1 | 1 | 1 | 1 |

VPC Networks:

| Project | VPC | Auto-created Subnets | Routing Mode | Network MTU | VPC Peerings |
| --- | --- | --- | --- | --- | --- |
| sample-project | sample-value | no | sample-value | sample-value | 1 |

Firewall Rules:

| Project | Rule | VPC | Direction | Priority | Disabled | Source IP Ranges | Target Tags | Target Accounts | Allowed Traffic | Denied Traffic |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | sample-value | sample-value | operator@example.com | sample-value | sample-value |

DNS Managed Zones:

| Project | Zone Name | DNS Domain | Visibility | DNS Records | Attached VPCs | VPC Names |
| --- | --- | --- | --- | --- | --- | --- |
| sample-project | sample-value | sample-value | sample-value | 1 | 1 | sample-value |
| sample-project | sample-value | sample-value | sample-value | 1 | 1 | sample-value |
| sample-project | sample-value | sample-value | sample-value | 1 | 1 | sample-value |

VPC Peerings:

| Project | VPC | Peering Name | State | Peer Network | Imports Custom Routes | Exports Custom Routes |
| --- | --- | --- | --- | --- | --- | --- |
| sample-project | sample-value | sample-value | sample-value | sample-value | yes | yes |
| sample-project | sample-value | sample-value | sample-value | sample-value | yes | yes |
| sample-project | sample-value | sample-value | sample-value | sample-value | yes | no |
| sample-project | sample-value | sample-value | sample-value | sample-value | yes | yes |
| sample-project | sample-value | sample-value | sample-value | sample-value | no | no |
| sample-project | sample-value | sample-value | sample-value | sample-value | yes | no |

## Kubernetes Capacity Evidence

This section records Kubernetes resource evidence for weekly reporting only. It does not represent active GKE monitoring or alerting; alerting and monitoring stack findings are reported in the monitoring sections.

### Current Kubernetes Cluster Usage (Needs Review)
- Reason: sample-cluster: pod waiting (sample_key_1:1), 1 pod issue, 1 unhealthy workload, 1 autoscaler at max replicas, 1 autoscaler failure condition; sample-cluster: pod waiting (sample_key_1:1), 1 unhealthy workload, 1 autoscaler at max replicas, 1 autoscaler failure condition

- Source: current Kubernetes resource usage observed at collection time; these are not 7-day trend values.

| Cluster | Autoscalers Found | Autoscalers At Scaling Limit | Autoscalers With Issues | Nodes Included in Snapshot | Pods Included in Snapshot | CPU Used Now (cores) | Memory Used Now (GiB) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sample-cluster | 1 | 1 | 1 | 1 | 1 | 0.00 | 0.00 |
| sample-cluster | 1 | 1 | 1 | 1 | 1 | 0.00 | 0.00 |

Autoscaling Scope And Conditions:

| Cluster | Namespace | Autoscaler | Autoscaling Level | Policy Scope | Current Replicas | Desired Replicas | Min Replicas | Max Replicas | At Max Replicas | At Scaling Limit | Scaling Issues |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | no | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | no | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | no | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |
| sample-cluster | sample-namespace | sample-value | Sample-Value | Sample-Value | 1 | 1 | 1 | 1 | no | yes | none |

### Current Kubernetes Namespace Usage

- Source: current Kubernetes pod usage. CPU and memory are summed across pods in each namespace at collection time; this is not per-pod and not a 7-day trend.

- Bold CPU/memory values mark the top 3 observed namespace values in this report.

| Cluster | Namespace | Pod Count | Current CPU (cores) | Current Memory (GiB) |
| --- | --- | --- | --- | --- |
| sample-cluster | sample-namespace | 1 | **0.00** | **0.00** |
| sample-cluster | sample-namespace | 1 | **0.00** | **0.00** |
| sample-cluster | sample-namespace | 1 | **0.00** | **0.00** |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |
| sample-cluster | sample-namespace | 1 | 0.00 | 0.00 |

## Infrastructure Capacity And Managed Services

Resource utilization trends and managed service inventory for in-scope projects.

### Infrastructure Utilization Trends (Action Required)
- Summary: Sample collector result from anonymized evidence.
- Bold utilization values are observed values >= 85%.
- Reason: 1 Cloud SQL high-utilization finding >= 85%; 1 VM high-CPU finding >= 85%; 1 VM high-memory finding >= 85%; 1 Kubernetes node utilization observation >= 85%; 1 Redis utilization warning finding; 1 Kafka utilization warning finding; sample-project: sample-value

Infrastructure CPU Utilization Summary (1-Day):

- Spare CPU Capacity At Peak is derived as 100% minus Highest CPU Observed, floored at 0%; it is a remaining-capacity calculation, not separately collected CPU idle-time telemetry. VM rows use Compute Engine CPU utilization. GKE cluster rows use the highest node CPU allocatable utilization observed in that cluster; the row note states when the peak came from a historical node.

CPU Usage And Spare Capacity Summary:

| Project | Resource Type | Total Resources | CPU Telemetry Available | CPU Telemetry Missing | Highest CPU Observed (%) | Spare CPU Capacity At Peak (%) |
| --- | --- | --- | --- | --- | --- | --- |
| sample-project | Compute VM | 5 | 5 | 0 | 1.0% | 99.0% |
| sample-project | GKE cluster | 2 | 2 | 0 | 1.0% | 99.0% |

| Project | Resource Type | Resource | CPU Evidence Basis | Highest CPU Observed (%) | Spare CPU Capacity At Peak (%) | CPU Telemetry Available | Telemetry Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sample-project | Compute VM | sample-value | Compute Engine CPU utilization | 1.0% | 1.0% | Yes | sample-value |
| sample-project | Compute VM | sample-value | Compute Engine CPU utilization | 1.0% | 1.0% | Yes | sample-value |
| sample-project | Compute VM | sample-value | Compute Engine CPU utilization | 1.0% | 1.0% | Yes | sample-value |
| sample-project | Compute VM | sample-value | Compute Engine CPU utilization | 1.0% | 1.0% | Yes | sample-value |
| sample-project | Compute VM | sample-value | Compute Engine CPU utilization | 1.0% | 1.0% | Yes | sample-value |
| sample-project | GKE cluster | sample-cluster | Highest node CPU allocatable utilization | 1.0% | 1.0% | Yes |  |
| sample-project | GKE cluster | sample-cluster | Highest node CPU allocatable utilization | 1.0% | 1.0% | Yes |  |

Cloud SQL Utilization Summary (7-Day):

- `Highest Observed` means the highest point-in-time usage value Cloud Monitoring returned during the last 1 days. It is not the weekly average and it is not total usage over the week.

| Project | Instance | Highest CPU Observed (%) | Highest Memory Observed (%) | Highest Disk Observed (%) |
| --- | --- | --- | --- | --- |
| sample-project | sample-value | 1.0% | 1.0% | 1.0% |
| sample-project | sample-value | 1.0% | 1.0% | 1.0% |

VM Telemetry Source And Memory Summary (7-Day):

- VM CPU values are reported once in the CPU activity summary above. VM memory requires Ops Agent and is shown here with telemetry source notes.

| Project | Instance | CPU Source | Highest Memory Observed (%) | Note |
| --- | --- | --- | --- | --- |
| sample-project | sample-value | sample-value | n/a | sample-value; sample-value |
| sample-project | sample-value | sample-value | n/a | sample-value; sample-value |
| sample-project | sample-value | sample-value | n/a | sample-value; sample-value |
| sample-project | sample-value | sample-value | n/a | sample-value; sample-value |
| sample-project | sample-value | sample-value | n/a | sample-value; sample-value |

- VM telemetry notes:
  - sample-project: sample-value
  - sample-project: sample-value

GKE Workload Usage Summary (1-Day):

- Workload CPU is summed container CPU usage for the cluster, reported in cores. This is workload demand, not a cluster saturation percentage. Node saturation is reported separately in the CPU activity summary and node utilization table.

| Project | Cluster | Highest Workload CPU Observed (cores) | Highest Workload Memory Observed (GiB) |
| --- | --- | --- | --- |
| sample-project | sample-cluster | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | 1.00 | 0.00 GiB |

GKE Node Highest Observed Allocatable Utilization (1-Day Cloud Monitoring):

- Node State compares current Kubernetes node inventory with Cloud Monitoring history; Historical means the node was seen in the trend window but is not registered now.

| Project | Cluster | Node | Node State | Ready Now | Highest CPU Observed (%) | Highest Memory Observed (%) |
| --- | --- | --- | --- | --- | --- | --- |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |
| sample-project | sample-cluster | **sample-value** | **Active** | **yes** | 1.0% | 1.0% |

GKE Namespace 7-Day Usage Trends (1-Day Cloud Monitoring):

- CPU is an hourly rate summed across containers by namespace; memory is an hourly max sample summed by namespace. This is namespace-level, not per-pod.

| Project | Cluster | Namespace | Average CPU (cores) | P95 CPU (cores) | Highest CPU Observed (cores) | Average Memory (GiB) | P95 Memory (GiB) | Highest Memory Observed (GiB) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | 1.00 | 1.00 | 1.00 | 0.00 GiB | 0.00 GiB | 0.00 GiB |

GKE Pod Highest Observed Usage (1-Day Cloud Monitoring, Top 30 Shown):

- Pod CPU `Highest Observed` is the highest point-in-time CPU rate returned for the pod; pod memory `Highest Observed` is the highest memory value returned.
- `n/a` means that specific pod value was not returned by the top-N query.

| Project | Cluster | Namespace | Pod | Highest CPU Observed (cores) | Highest Memory Observed (GiB) |
| --- | --- | --- | --- | --- | --- |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | n/a |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | n/a |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | n/a |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | n/a |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | n/a |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | n/a |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |
| sample-project | sample-cluster | sample-namespace | sample-value | 1.00 | 0.00 GiB |

- GKE utilization notes:
  - sample-project: sample-value

Redis Throughput Trend (1-Day):

| Project | Instance | Traffic Direction | Average Throughput | Highest Throughput Observed |
| --- | --- | --- | --- | --- |
| sample-project | sample-value | sample-value | 1.00 B/s | 1.00 B/s |

Redis Utilization Details (1-Day):

| Project | Instance | Assessment | Highest CPU Observed (%) | Highest Memory Observed (%) | Evictions (/s) | Connected Clients | Ops (/s) | Replication Lag | Metric Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample-project | sample-value | No Findings | n/a | 1.0% | 1.00/s | 1 | 1.00/s | 1 s | sample-value |

- Redis data notes:
  - sample-project: sample-value
  - sample-project: sample-value

Managed Kafka Throughput Trend (1-Day):

| Project | Cluster | Average Inbound Traffic | Average Outbound Traffic | Average Messages | Highest Inbound Traffic Observed | Highest Outbound Traffic Observed | Highest Messages Observed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sample-project | sample-cluster | 1.00 B/s | 1.00 B/s | 1.00/s | 1.00 B/s | 1.00 B/s | 1.00/s |

Managed Kafka Utilization Details (1-Day):

| Project | Cluster | Assessment | Highest Broker CPU Observed (%) | Highest Broker Memory Observed (%) | Highest Broker Disk Observed (%) | Under-Replicated Partitions | Offline Partitions | Consumer Lag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample-project | sample-cluster | No Findings | 1.0% | 1.0% | 1.0% | 1 | 1 | 1 |

- Kafka data notes:
  - sample-project: sample-value
  - sample-project: sample-value

### Managed Service Inventory
- Summary: Sample collector result from anonymized evidence.

Cloud SQL Inventory:

| Project | Assessment | Instances Found |
| --- | --- | --- |
| sample-project | No Findings | 1 |

- Redis Assessment: No Findings
- Managed Kafka Assessment: No Findings

Redis Inventory:

| Project | Assessment | Instance | State | Version | Memory (GB) |
| --- | --- | --- | --- | --- | --- |
| sample-project | No Findings | sample-value | sample-value | sample-value | 1 |

Managed Kafka Inventory:

| Project | Assessment | Cluster | State | Capacity Details |
| --- | --- | --- | --- | --- |
| sample-project | No Findings | sample-value | sample-value | {'sample_key_1': 'sample-value', 'sample_key_2': 'sample-value'} |

Compute Engine Instance Inventory:

- Includes standalone VMs and GKE node VMs. Standalone VM CPU is summarized in Infrastructure Utilization Trends.

| Name | Project | Zone | VM State | Machine Type | Note |
| --- | --- | --- | --- | --- | --- |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |
| sample-value | sample-project | us-central1 | sample-value | sample-value |  |

Load Balancer Backend Health:

- Summary: 7 backend services reviewed; 7 backend services requiring review; 7 unhealthy backends; 0 unknown backends

Backend Services Requiring Review:

| Backend Service | Project | Scope | Backends | Health Checks | Backend Health | Protocol | Traffic Type | Assessment | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sample-value | sample-project | sample-value | 1 | 1 | 0 healthy; 1 unhealthy; 0 unknown | sample-value | sample-value | No Findings | Sample evidence generated from anonymized data. |
| sample-value | sample-project | sample-value | 1 | 1 | 0 healthy; 1 unhealthy; 0 unknown | sample-value | sample-value | No Findings | Sample evidence generated from anonymized data. |
| sample-value | sample-project | sample-value | 1 | 1 | 0 healthy; 1 unhealthy; 0 unknown | sample-value | sample-value | No Findings | Sample evidence generated from anonymized data. |
| sample-value | sample-project | sample-value | 1 | 1 | 0 healthy; 1 unhealthy; 0 unknown | sample-value | sample-value | No Findings | Sample evidence generated from anonymized data. |
| sample-value | sample-project | sample-value | 1 | 1 | 0 healthy; 1 unhealthy; 0 unknown | sample-value | sample-value | No Findings | Sample evidence generated from anonymized data. |
| sample-value | sample-project | sample-value | 1 | 1 | 0 healthy; 1 unhealthy; 0 unknown | sample-value | sample-value | No Findings | Sample evidence generated from anonymized data. |
| sample-value | sample-project | sample-value | 1 | 1 | 0 healthy; 1 unhealthy; 0 unknown | sample-value | sample-value | No Findings | Sample evidence generated from anonymized data. |

## Logging And Delivery Pipeline

Cloud Logging sink, bucket, ingestion, retention, and Pub/Sub delivery posture.

### Cloud Logging Pipeline
- Summary: Sample collector result from anonymized evidence.

| Project | Assessment | Logging Sinks | Log Buckets | Pub/Sub Topics | Pub/Sub Subscriptions | Splunk Destination Detected |
| --- | --- | --- | --- | --- | --- | --- |
| sample-project | No Findings | 1 | 1 | 1 | 1 | yes |

Cloud Logging Buckets:

| Project | Bucket | Location | Retention Days | Retention Locked |
| --- | --- | --- | --- | --- |
| sample-project | sample-value | us-central1 | 1 | no |
| sample-project | sample-value | us-central1 | 1 | yes |
| sample-project | sample-value | us-central1 | 1 | no |

Project-Level Log Ingestion (7-Day):

- Source: Cloud Monitoring log-volume data. Log ingestion is project-level because this table uses the project-level ingestion metric.

| Project | Total Logs Ingested (7-Day) | Peak Hour Log Ingestion | Stored Volume Metric |
| --- | --- | --- | --- |
| sample-project | 1 B | 1 B | metric not returned |

Log Bucket Ingestion (7-Day):

- Source: Cloud Monitoring `log_bucket_bytes_ingested`, grouped by log bucket. This is bucket-level ingestion volume and peak hourly insertion, not retained size.

| Project | Bucket | Location | Total Logs Ingested (7-Day) | Peak Hour Log Insertion |
| --- | --- | --- | --- | --- |
| sample-project | sample-value | us-central1 | 1 B | 1 B |

Long-Retention Log Storage Metric:

- Source: Cloud Monitoring `bytes_stored`, grouped by `log_bucket_id` and `log_bucket_location`. Google defines this as logs retained past the default 30 days; it is not total current bucket occupancy.
- Cloud Logging bucket metadata exposes retention settings, not current stored bytes. `_Required` is fixed at 400 days and is excluded from retention charges.

- Metric was not returned for sample-project. If any non-`_Required` bucket retains logs longer than 30 days, confirm Cloud Monitoring metric availability or use an agreed external source such as Billing Export or Log Analytics.

Logging Sink Pub/Sub Delivery Health (7-Day):

| Project | Subscription | Peak Unacked Messages | Oldest Message Age | Lowest Delivery Health Score | Dead Letter Messages (7-Day) |
| --- | --- | --- | --- | --- | --- |
| sample-project | 192.0.2.10 | 1 | 1 s | 1.00 | 1 |

- Logging/Pub/Sub data notes:
  - sample-project: sample-value


## Evidence Index

- Report Markdown: `opsbrief-sample-weekly-report.md`
- Report JSON: `opsbrief-sample-weekly-report.json`
- Assessment run evidence: `evidence/collector-status.json`
- Detailed evidence by check: `evidence/collectors/*.json`
- Chart evidence: `evidence/charts/*.png`
- Evidence is generated from read-only API calls only.