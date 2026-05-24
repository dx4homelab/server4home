# Two layers of OS ownership — and where Elemental / Fleet fit (or don't)

A note for future-us so we don't conflate two very different decisions
when this question resurfaces.

## The distinction that matters

There are two separate questions about OS lifecycle in this homelab,
and the answers are deliberately different:

| Question | Answer | Why |
| --- | --- | --- |
| **Q1.** Who builds the OS for the management cluster (Rancher Manager + anything in the control plane)? | **We do** — server4home, bootc, our own image, our own CI, our own cosign chain. | This is the layer we want to fully understand and hand-debug. It's the symmetry partner of [dx4bluefin](https://github.com/dx4homelab/dx4bluefin) on the workstation. |
| **Q2.** Who builds the OS for the workload VMs that Rancher provisions on Harvester (GitLab, SOLR, future services)? | **SUSE does** — we accept whatever image Rancher/Harvester ships natively. | This is the layer where we explicitly delegate lifecycle to Harvester. The whole point of using Harvester is *not* owning this. |

The earlier "rejection of Elemental" was about **Q1**: Elemental Toolkit as
a tool to build our own custom immutable server OS. We tried it, didn't
click for that use case, built server4home instead.

The "rejection" does NOT extend to **Q2**: Rancher / Harvester / Elemental
as infrastructure foundations that ship their own OS choices for the
workload VMs they manage. If the path of least resistance for Harvester-
provisioned VM clusters happens to involve Elemental-based images managed
by Fleet, that's fine — it's SUSE's problem, not ours.

## The actual question — should we use Fleet to manage Harvester VM OS images?

Reframed correctly, the question becomes:

> **If we're standing up Rancher-provisioned downstream clusters on
> Harvester, should the OS-image lifecycle for those VMs flow through
> Fleet + ManagedOSImage CRDs, or should we leave it on whatever default
> Harvester+Rancher ships?**

That's a real, open question. The right answer depends on operational
preference, not architectural principle. Both options are compatible with
the [[homelab-architecture]] split:

### Option A: lean in — Fleet + ManagedOSImage GitOps for workload VMs

```text
git repo  --Fleet GitOps-->  ManagedOSImage CRD  --Elemental controller-->  Harvester VMs
```

Declare the OS image ref in a YAML file in a git repo, Fleet reconciles
it onto the Harvester-managed cluster nodes. A/B partition swap, reboot,
done. Auditable: every OS rev is a git commit.

**Pick this if:** you want every OS-image change for the workload plane
to be a reviewable PR, you like the idea of Fleet running anyway for
workload Helm releases (see below), and you're comfortable being on the
SUSE Elemental supported path for those VMs.

### Option B: stay native — let Rancher/Harvester drive it via UI/API

Configure cluster templates in the Rancher UI, accept the default node
image Harvester provides, take OS updates when Rancher prompts. No
custom Fleet config, no CRDs we own.

**Pick this if:** you want maximum delegation, you trust SUSE's defaults,
and you'd rather spend time on workload Helm releases than on OS-image
GitOps. This is the "Harvester is taking this off my plate" answer in
its purest form.

### Current decision (2026-05-24): Option B, full stop

We're not wiring Fleet + ManagedOSImage CRDs into the workload plane. The
workload-plane VMs use whatever Rancher/Harvester provisions natively;
their OS lifecycle is Harvester's job, not ours.

This is a *decision*, not a "starting point pending pain." The bias
matches the rest of the [[homelab-architecture]] philosophy: don't take
ownership of a layer until it's actually costing us to not own it. The
revisit triggers below are real triggers, not a roadmap.

## Where Fleet IS clearly useful, regardless of the OS-image decision

Fleet is a GitOps engine — it watches git, reconciles to clusters. Even
if Option B wins for OS images, Fleet is still the natural choice for
**workload Helm releases on downstream clusters**:

- `fleet-default` on the management cluster watches a `homelab-workloads/`
  directory in a git repo.
- A `fleet.yaml` in `gitlab/` reconciles a GitLab Helm release onto the
  workload cluster.
- Same for SOLR, Argo, etc.

That's Fleet doing the job it's good at — GitOps over a fleet of
clusters — without needing to take a position on the OS-image-lifecycle
question. We'd land Fleet for workloads regardless.

## When this whole document needs to be revisited

- **server4home VMs themselves start running on Harvester routinely.** If
  we start using Harvester to host server4home VMs (rather than only
  Proxmox/libvirt), Fleet + bootc-image-ref GitOps for those nodes becomes
  interesting — but that'd be an extension of our own toolchain, not
  adoption of Elemental.
- **A workload-plane outage we can't diagnose from Rancher's defaults.**
  If Option B's "trust SUSE's defaults" runs out of road, that's the
  trigger to move to Option A.
- **dx4bluefin migrates off bootc, or Harvester adopts bootc natively.**
  Both would shift the calculus on the symmetry argument — but neither
  is imminent.

Until then: server4home owns the management cluster's OS, Harvester owns
the workload VMs' OS, Fleet handles workload GitOps once we set it up.
Two layers, two owners, no overlap to fight over.
