# Hosting options

This note compares practical places to run the production worker. It is a
point-in-time purchasing aid, not a change to the deployment architecture in
`DESIGN.md`. The current deployment remains a small Linux VM running Docker
Compose, an always-on checker process, a persistent SQLite volume, and a
repository-scoped self-hosted GitHub Actions runner.

**Research date:** 2026-08-15. Prices below are public list prices and can
change. Unless stated otherwise, they exclude tax, backup storage, excess
traffic, and optional public IPv4 charges. Verify the selected region and final
checkout price before purchasing.

## Workload and selection criteria

The worker has no inbound web traffic and needs relatively little steady CPU.
The hosting choice must support:

- an uninterrupted background process with outbound HTTPS access;
- durable storage for the SQLite state database;
- Docker Engine and Docker Compose, or an explicitly redesigned equivalent;
- backups that are independent of the live disk;
- enough headroom to build a container image during deployment; and
- a trusted self-hosted GitHub Actions runner if the current deployment flow is
  retained.

Start with **2 GB RAM** for a conventional VM. The checker alone may fit in less,
but Docker image builds and the co-located Actions runner need headroom. A 1 GB
VM is reasonable only after measuring peak build memory and configuring a safe
amount of swap.

## Shortlist

| Option | Representative monthly floor | Fit with current deployment | Main trade-off |
| --- | ---: | --- | --- |
| Existing local VM | $0 incremental | Excellent | The operator owns power, connectivity, patching, monitoring, and off-host backups. |
| Google Cloud Free Tier `e2-micro` with external IPv4 | about $3.65 for IPv4 | Conditional | The eligible VM and 30 GB disk fit within the Free Tier, but its 1 GB RAM is tight for Docker builds and the public IPv4 address is billed separately. |
| Hetzner Cloud CX23 in Europe | $6.49, excluding VAT and IPv4 | Excellent | Low price and 4 GB RAM, but the location may be farther from the checked provider and cost-optimized capacity is limited. |
| DigitalOcean Basic Droplet, 2 GB | $12 | Excellent | Simple, predictable VM; backups add 20% weekly or 30% daily unless usage-based backup is selected. |
| AWS Lightsail Linux, 2 GB with public IPv4 | $12 | Excellent | Predictable bundle and AWS regions; snapshots cost $0.05/GB-month. |
| Oracle Cloud Always Free Ampere A1 | $0 within allowance | Conditional | ARM architecture, regional capacity errors, and idle-instance reclamation make it a better lab or fallback than the default production host. |
| Fly.io Machine, 1 shared CPU and 2 GB | about $11.11 plus storage | Requires adaptation | A persistent volume is $0.15/GB-month; deployment moves from Compose/runner to Fly tooling. |
| Railway Hobby | $5 minimum, then measured usage | Requires adaptation | RAM is $10/GB-month, CPU is $20/vCPU-month, and volumes are $0.15/GB-month; actual cost depends on utilization. |

Price sources and qualifications:

- Google Cloud's [Free Tier documentation](https://docs.cloud.google.com/free/docs/free-cloud-features#compute)
  includes one non-preemptible `e2-micro` VM per month in `us-west1`,
  `us-central1`, or `us-east1`, plus 30 GB-months of standard persistent disk.
  The [E2 machine documentation](https://docs.cloud.google.com/compute/docs/general-purpose-machines#e2_machine_types)
  lists 1 GB RAM for `e2-micro`. Google charges
  [in-use external IPv4 addresses](https://cloud.google.com/vpc/network-pricing#ipaddress)
  on standard VMs at $0.005 per hour, or about $3.65 for 730 hours. The worker
  needs outbound internet access; an external IPv4 address is the simplest
  option, while Cloud NAT adds its own charges and complexity. Billing-account,
  traffic, region, and Free Tier limits still apply.
- Hetzner lists the CX23 as 2 shared vCPUs, 4 GB RAM, and 40 GB storage. Its
  [15 June 2026 adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
  sets the European price at $6.49 per month excluding VAT and IPv4. The
  [product page](https://www.hetzner.com/cloud/cost-optimized) describes this
  tier as cost-optimized for low-to-medium CPU use and subject to limited
  capacity.
- DigitalOcean's [Droplet price table](https://www.digitalocean.com/pricing/droplets)
  lists the 1 vCPU, 2 GiB, 50 GiB Basic Droplet at $12 per month and documents
  its backup premiums. Powered-off Droplets remain billable until destroyed,
  according to the [billing documentation](https://docs.digitalocean.com/products/droplets/details/pricing/).
- AWS documents the $12, 2 vCPU, 2 GB, 60 GB, public-IPv4
  [Lightsail bundle](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)
  and [snapshot pricing](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-faq-snapshots.html).
- Oracle's [Always Free limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
  currently provide 1,500 Ampere OCPU-hours and 9,000 GB-hours per month,
  equivalent to 2 ARM OCPUs and 12 GB RAM, plus 200 GB combined boot/block
  storage and five volume backups. Oracle also warns that capacity errors can
  occur and that idle free instances may be reclaimed. Do not rely on older
  4-OCPU/24-GB summaries.
- Fly.io's [resource pricing](https://fly.io/docs/about/pricing/) lists an
  always-running 1 shared CPU, 2 GB Machine at about $11.11 per month and local
  persistent volumes at $0.15/GB-month. Keep auto-stop disabled for this
  background worker.
- Railway's [pricing documentation](https://docs.railway.com/pricing/plans)
  lists the $5 Hobby minimum and usage rates. The subscription covers the first
  $5 of monthly resource usage; it is not $5 plus the same usage charge.

Render is also technically suitable: it provides continuously running
[background workers](https://render.com/docs/background-workers) and allows a
paid worker to attach a [persistent disk](https://render.com/docs/disks).
However, a disk belongs to one service instance, prevents zero-downtime deploys,
and preserves only its mount path. It offers no clear advantage over the other
managed choices for this single SQLite-backed worker, so it is not in the priced
shortlist.

## Recommendation

1. **Keep the existing local VM for the first real-world deployment** if it can
   remain online, receive security updates, and send backups to another device
   or provider. This has no migration work and exercises the production design
   already implemented and tested.
2. **Choose a conventional VPS when off-site availability is worth a monthly
   fee.** A 2 GB or larger x86-64 Linux VM preserves the current Compose,
   SQLite, operations, rollback, and Actions-runner model. Hetzner is the
   lowest listed price where its European location and capacity are acceptable;
   DigitalOcean and Lightsail are straightforward $12 alternatives with North
   American regions.
3. **Consider Google Cloud's Free Tier when minimum recurring cost matters.**
   Its approximately $3.65 monthly external-IPv4 charge makes it an inexpensive
   x86-64 VM, but 1 GB RAM is below the recommended starting point. Validate
   deployment memory first, add swap, and preferably build the production image
   in CI instead of on the VM.
4. **Treat Oracle Always Free as an experiment or disaster-recovery candidate.**
   Validate the multi-architecture image and runner on ARM first, and do not
   make a time-sensitive checker depend on free capacity or a resource subject
   to idle reclamation.
5. **Do not migrate to a managed container platform yet.** Fly.io, Railway, and
   Render can run the worker, but each requires platform-specific deployment and
   volume configuration. The current persistent self-hosted runner also assumes
   host-level Docker access. GitHub notes that the operator owns self-hosted
   runner maintenance and recommends ephemeral runners for autoscaling; see the
   [runner documentation](https://docs.github.com/en/actions/concepts/runners/self-hosted-runners)
   and [reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).

## Backup and operations baseline

Whichever VM provider is selected:

- keep the production SQLite volume on persistent local storage;
- make an application-consistent SQLite backup and copy it off the VM;
- enable provider snapshots as a second recovery layer, not the only backup;
- test a restore before depending on the deployment;
- enable unattended security updates or establish a patching schedule;
- alert when the worker is unhealthy or has not completed a check recently;
- restrict SSH and keep the repository-scoped Actions runner dedicated to this
  trusted production workflow; and
- test the provider from the chosen region against the target site before
  migrating production. Latency is unimportant at polling cadence, but egress
  filtering or provider blocking is not.

## Revisit this decision when

Re-evaluate hosting when any of these becomes true:

- the local VM cannot meet the required uptime or off-site backup target;
- container builds compete with the checker for memory or disk;
- more than one worker instance must share state;
- SQLite is replaced by a managed database;
- a management UI or public API creates inbound traffic and TLS requirements;
- deployment no longer needs a persistent self-hosted runner; or
- operating-system and Docker maintenance cost more than a managed platform's
  premium.

At that point, separate the durable database from worker compute before scaling
to multiple instances. A SQLite file on a single attached volume is deliberately
a single-instance design.
