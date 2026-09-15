---
title: S3 Volume Backend
authors:
  - "Omar Shibli"
creation-date: 2026-09-15
last-updated: 2026-09-15
status: implementing
---

# OSEP-0024: S3 Volume Backend

<!-- toc -->
- [Summary](#summary)
- [Motivation](#motivation)
  - [Goals](#goals)
  - [Non-Goals](#non-goals)
- [Requirements](#requirements)
- [Proposal](#proposal)
  - [Notes/Constraints/Caveats](#notesconstraintscaveats)
  - [Risks and Mitigations](#risks-and-mitigations)
- [Design Details](#design-details)
- [Test Plan](#test-plan)
- [Drawbacks](#drawbacks)
- [Alternatives](#alternatives)
- [Infrastructure Needed](#infrastructure-needed)
- [Upgrade & Migration Strategy](#upgrade--migration-strategy)
<!-- /toc -->

## Summary

Add an `s3` backend to `volumes[]` in the Lifecycle API. On the Kubernetes runtime the server realizes it with the AWS-supported Mountpoint for Amazon S3 CSI driver: for each `s3` volume the server creates a static `PersistentVolume` and a bound `PersistentVolumeClaim`, and the sandbox pod mounts the claim. Credentials never appear in the API or in Kubernetes Secrets — one IAM role, bound to the CSI driver ServiceAccount through EKS Pod Identity or IRSA, gives every sandbox its S3 access.

The Docker and FastSandbox runtimes reject `s3` in this phase.

This OSEP records the design in `docs/superpowers/specs/2026-09-15-s3-volume-backend-design.md` and extends OSEP-0003 (Volume Support), which named `s3` as a future backend.

## Motivation

The existing object-storage backend, `ossfs`, is Alibaba-only, Docker-only, and requires inline access keys in the request. Deployments on AWS EKS have no way to mount a bucket into a sandbox. The first concrete need is to sync command output (stdout, stderr) to S3. The design keeps the API open for other uses, such as dataset input and artifact output.

### Goals

- Mount an S3 bucket, or a prefix inside it, at a path inside a sandbox on EKS.
- No access keys anywhere: not in the API, not in Secrets, not in config.
- Additive API change; existing clients keep working.
- No privileged sandbox pods, no FUSE device in the sandbox, compatible with gVisor and Kata runtime classes.
- Clear validation errors when the runtime or the cluster cannot serve the request.

### Non-Goals

- Docker runtime support. A later phase can add `mount-s3` on the host with the EC2 instance profile.
- S3-compatible stores such as MinIO. A later `endpoint` field can add this.
- Per-tenant IAM roles. A later `authenticationSource: pod` extension can add this without an API change.
- Full POSIX semantics. Mountpoint semantics are documented and accepted.
- Changes to the fast-sandbox template publish path or to snapshots.

## Requirements

- The API change is additive: a new `S3` schema next to `OSSFS` and a new `s3` property on `Volume`. No existing field changes meaning.
- A volume carries exactly one backend; `s3` joins `host`, `pvc` and `ossfs` in that check.
- The request never carries a credential. The only identity is an IAM role attached to the CSI driver ServiceAccount.
- The sandbox pod stays unprivileged: no `/dev/fuse`, no `SYS_ADMIN`, no ServiceAccount change. The kubelet performs the mount on the node, so gVisor and Kata receive it as a host directory.
- Every object the server creates is labeled and removable, including after a server restart.
- Operators can constrain the feature through config: the CSIDriver name, default mount options, and an optional bucket allowlist.
- Unsupported runtimes and missing cluster prerequisites fail with a named error, not a timeout.

## Proposal

Add an `S3` schema to `specs/sandbox-lifecycle.yml` and an `s3` property to `Volume`:

```yaml
volumes:
  - name: logs
    s3:
      bucket: "my-team-sandbox-logs"     # required
      prefix: "sandboxes/task-001/"      # optional; the server adds a trailing "/" if absent
      region: "eu-west-1"                # optional; Mountpoint detects it if absent
      options: ["uid=1000", "gid=1000"]  # optional; raw Mountpoint options, no leading "-"
    mountPath: /mnt/logs
    readOnly: false
```

| Field | Type | Required | Rules |
|---|---|---|---|
| `bucket` | string | yes | S3 bucket naming rules: 3 to 63 chars, lowercase letters, digits, dots, hyphens; starts and ends with a letter or digit. If `storage.s3_allowed_buckets` is non-empty, the bucket must be in it. |
| `prefix` | string | no | No leading `/`, no `..` segment, no shell metacharacters, max 1024 bytes. Normalized to end with `/`. |
| `region` | string | no | Matches `^[a-z]{2}(-[a-z]+)+-\d$`. |
| `options` | []string | no | Same rules as `ossfs.options`: no leading `-`, no `; & \| \` $ ( ) < > \n \r`. Reserved names are rejected. |

Design decisions behind that shape:

- **`prefix` lives inside `s3`.** It uses the S3 term and gets its own validation. `subPath` on an `s3` volume is rejected with `VOLUME::INVALID_SUB_PATH` and a message that points to `s3.prefix`. A Kubernetes `volumeMounts.subPath` on a FUSE mount fails when the prefix has no objects yet, so the server never emits one for `s3`.
- **No credential fields.** Identity is cluster-side only.
- **Reserved options** owned by the server: `prefix`, `region`, `read-only`, `allow-delete`, `allow-overwrite`. A request that passes one gets `VOLUME::INVALID_S3_OPTION`.

Each SDK (Python, TypeScript, Go, C#, Kotlin) gets an `S3` model with the four fields and a `Volume.s3` field, mirroring the existing `OSSFS` model and converters.

### Notes/Constraints/Caveats

Mountpoint is not a POSIX file system. This is the user-facing contract:

| Operation | Supported | Note |
|---|---|---|
| Create a new file, write sequentially, close | yes | Uploaded as a multipart upload; object visible on close |
| Overwrite an existing file (open with truncate) | yes | Requires `allow-overwrite`, set by default for read-write |
| Delete | yes | Requires `allow-delete`, set by default for read-write |
| Append to an existing closed file | no | S3 objects are immutable |
| Random-offset write, in-place edit | no | Same reason |
| Rename, symlink, hardlink | no | S3 has no rename |

The recommended log pattern is therefore **one object per command**, for example `/mnt/logs/<command-id>.stdout` and `.stderr`. A writer that needs to update a file must rewrite the whole file.

### Risks and Mitigations

- **A single IAM role is shared by all sandboxes in the cluster.** Mitigation: the role's resource ARNs are the real boundary, and the optional `s3_allowed_buckets` allowlist adds a server-side check. Per-tenant roles remain possible later through `authenticationSource: pod` with no API change.
- **Users expect POSIX behavior and see silent surprises** (no append, no rename). Mitigation: the semantics table above ships in `docs/examples/kubernetes-s3-volume-mount.md` with the one-object-per-command pattern.
- **Leaked cluster-scoped PVs.** PVs cannot carry a namespaced `ownerReference`, so garbage collection does not reclaim them. Mitigation: labeled objects, deletion on the sandbox delete/expiry/create-failure paths, and a startup sweep that removes PVs whose `claimRef` PVC is gone.
- **Mount failures surface only as a pod that never becomes ready.** Mitigation: on a readiness timeout for a sandbox with an `s3` volume, the server appends the last `FailedMount` event to the error detail.
- **Cluster prerequisites missing.** Mitigation: the server checks for the `CSIDriver` object and fails with `VOLUME::UNSUPPORTED_BACKEND` and a message naming the add-on to install.
- **Option injection through `options`.** Mitigation: the same validation as `ossfs.options` plus the reserved-name list, applied to both request and operator options (operator options are validated at startup).

## Design Details

### Config

New keys in `StorageConfig` (`server/opensandbox_server/config.py`), documented in `server/configuration.md`:

| Key | Default | Purpose |
|---|---|---|
| `s3_csi_driver` | `"s3.csi.aws.com"` | Name of the `CSIDriver` object to check and to put in the PV. |
| `s3_mount_options` | `[]` | Operator defaults added to every S3 mount, for example `uid=1000`. Same validation as request options; reserved names rejected at startup. |
| `s3_allowed_buckets` | `[]` | Optional allowlist. Empty means any bucket. The IAM role is the real boundary. |

### Server flow

All new S3 logic lives in one new module, `server/opensandbox_server/services/k8s/s3_volume.py`. Other files get small additive edits.

1. **Parse.** `api/schema.py`: `S3` Pydantic model, `Volume.s3`, exactly-one-backend validator extended.
2. **Validate.** `services/validators.py`: `ensure_valid_s3_volume`, called from `ensure_volumes_valid`, which also rejects `subPath` for `s3`. Error codes in `services/constants.py`.
3. **Runtime gate.** `services/docker/volumes.py` raises `VOLUME::UNSUPPORTED_BACKEND` for `s3`. FastSandbox already rejects all volumes.
4. **Driver check.** On the first `s3` request, the server reads `storage.k8s.io/v1 CSIDriver <s3_csi_driver>`. A positive result is cached for the process lifetime. If missing, the request fails with `VOLUME::UNSUPPORTED_BACKEND` and a message that names the add-on to install.
5. **Provision.** `_ensure_s3_volumes(volumes, sandbox_id)` runs after `_ensure_pvc_volumes` and before workload creation. For each `s3` volume it creates one PV and one PVC. If any create fails, the server deletes the objects it created in this request and re-raises.
6. **Pod spec.** `services/k8s/volume_helper.py`: an `s3` branch emits a `persistentVolumeClaim` source that points at the generated claim, with `readOnly` from the volume, and a mount with `mountPath` and `readOnly`. No `subPath`.
7. **Ownership.** Generated PVCs join the list that receives `ownerReferences` to the workload CR, the same as managed PVCs today. PVs are cluster-scoped and cannot have a namespaced owner; they need explicit cleanup.

### Naming

`s3-<sandbox-id>-<volume-name>` for the PV, the PVC, and the `volumeHandle`. The sandbox id is a UUID (36 chars) and the volume name is a DNS label (max 63), so the result is at most 103 characters and a valid DNS subdomain.

### Kubernetes objects

For the example request, sandbox id `abc123`, namespace `sandboxes`:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: s3-abc123-logs
  labels:
    opensandbox.io/volume-managed-by: server
    opensandbox.io/id: abc123
spec:
  capacity:
    storage: 1Gi                        # required by the API; Mountpoint ignores it
  accessModes: [ReadWriteMany]          # ReadOnlyMany when readOnly is true
  persistentVolumeReclaimPolicy: Retain # the driver has no controller; the server deletes the PV
  storageClassName: ""                  # static binding, no provisioner
  claimRef:
    namespace: sandboxes
    name: s3-abc123-logs
  mountOptions:
    - allow-other
    - allow-delete                      # read-write only
    - allow-overwrite                   # read-write only
    - prefix sandboxes/task-001/        # from s3.prefix
    - region eu-west-1                  # from s3.region, if given
    - uid=1000                          # operator s3_mount_options, then request options
  csi:
    driver: s3.csi.aws.com              # from storage.s3_csi_driver
    volumeHandle: s3-abc123-logs        # unique in the cluster
    volumeAttributes:
      bucketName: my-team-sandbox-logs
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: s3-abc123-logs
  namespace: sandboxes
  labels:
    opensandbox.io/volume-managed-by: server
    opensandbox.io/id: abc123
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  volumeName: s3-abc123-logs
  resources:
    requests:
      storage: 1Gi
```

Rules:

- **Read-only volumes** get the `read-only` option and no `allow-delete` or `allow-overwrite`; access mode `ReadOnlyMany` on both PV and PVC; pod volume source `readOnly: true`. PVC access modes always mirror the PV.
- **Option order:** server defaults, then operator `s3_mount_options`, then request `options`. Mountpoint takes the last value when an option repeats, so a request can override an operator `uid`.
- The `capacity` value comes from `storage.volume_default_size`.

### Cleanup

- `_cleanup_managed_pvcs(sandbox_id)` also deletes PVs labeled `opensandbox.io/volume-managed-by=server` and `opensandbox.io/id=<sandbox-id>`. It runs on delete, on expiry, and on create failure, as today. Deletion is best effort and logged; 404 is success.
- **Startup sweep.** On start, the server lists PVs with `opensandbox.io/volume-managed-by=server`. For each PV whose `claimRef` PVC no longer exists, it deletes the PV. This covers PVCs removed by controller garbage collection while the server was down.

### RBAC

The server Helm chart ClusterRole adds:

- `persistentvolumes`: `create`, `delete`, `get`, `list`
- `storage.k8s.io` `csidrivers`: `get`

### Identity (operator setup)

Documented in `docs/examples/kubernetes-s3-volume-mount.md`.

1. Install the Mountpoint for Amazon S3 CSI driver EKS add-on (`aws-mountpoint-s3-csi-driver`). It runs in `kube-system` with ServiceAccount `s3-csi-driver-sa`.
2. Create one IAM role with `s3:ListBucket` on the bucket ARNs and `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload` on the object ARNs. Limit resources to the buckets sandboxes may reach.
3. Bind the role to that ServiceAccount with an EKS Pod Identity association (recommended) or an IRSA `eks.amazonaws.com/role-arn` annotation.

The sandbox pod needs no ServiceAccount change, no capabilities, and no FUSE device. The kubelet performs the mount on the node. gVisor and Kata receive the mount as a host directory.

Future extension, no API change: `volumeAttributes.authenticationSource: pod` plus an annotated sandbox ServiceAccount gives one role per tenant.

### Errors

Validation, HTTP 400, before any side effect:

| Code | Cause |
|---|---|
| `VOLUME::INVALID_BACKEND` | Zero or more than one backend (existing) |
| `VOLUME::INVALID_S3_BUCKET` | Name breaks S3 rules, or not in `s3_allowed_buckets` |
| `VOLUME::INVALID_S3_PREFIX` | Leading `/`, `..` segment, shell metacharacters, or over 1024 bytes |
| `VOLUME::INVALID_S3_REGION` | Does not match the region pattern |
| `VOLUME::INVALID_S3_OPTION` | Leading `-`, shell metacharacters, or a reserved name |
| `VOLUME::INVALID_SUB_PATH` | `subPath` given on an `s3` volume; message points to `s3.prefix` |
| `VOLUME::UNSUPPORTED_BACKEND` | Docker or FastSandbox runtime, or the `CSIDriver` object is missing |

Provisioning, from the Kubernetes API:

- **Create fails.** Roll back objects created in this request, return `KUBERNETES::API_ERROR` with the API message. A 403 names the missing RBAC verb, like the PVC code today.
- **409 on create.** The name embeds the sandbox id, so a conflict is a leftover from an earlier attempt for the same id. Read the existing object; if its `opensandbox.io/id` label matches, reuse it; otherwise return 500 with a clear message.
- **Mount fails on the node** (wrong bucket, IAM denied). The kubelet emits a `FailedMount` event and the pod never becomes ready. The readiness loop in `_wait_for_sandbox_ready` reads only the workload status message today. On a readiness timeout for a sandbox that has an `s3` volume, the server reads pod events through the existing `get_sandbox_events` diagnostics helper and appends the last `FailedMount` message to the `KUBERNETES::POD_READY_TIMEOUT` detail. The existing cleanup path then removes the PV and PVC.

Cleanup is best effort and logged; 404 is success, and the startup sweep catches leftovers.

## Test Plan

Unit tests in `server/tests/`, following the `ossfs` and PVC test shapes:

- **Schema** (`test_schema.py`): valid `s3` volume, serialization round trip, `s3` plus another backend rejected, unknown field rejected.
- **Validators** (`test_validators.py`): one test per error code, allowlist, reserved options, `subPath` rejection, prefix normalization.
- **Pod spec** (`test_batchsandbox_provider.py`): PVC source and mount for `s3`, with and without `readOnly`, no `subPath`; multiple `s3` volumes; internal name conflict.
- **Provisioning** (new `test_s3_volume.py`): PV and PVC bodies match this design exactly, including option order and read-only handling; rollback on partial failure; 409 reuse and mismatch; missing `CSIDriver`; cleanup deletes PV and PVC; startup sweep deletes an orphan PV and keeps a bound one; timeout detail includes the `FailedMount` message.
- **Runtime gate** (`test_docker_service.py`): `s3` returns `UNSUPPORTED_BACKEND`.
- **SDKs**: model tests per SDK, like the `OSSFS` tests.

The Kind e2e suite **cannot** cover this backend: it has no AWS credentials, and without an `endpoint` field it cannot be pointed at MinIO. Verification is therefore manual on EKS, and the procedure is recorded in the docs page:

1. Install the add-on and create the IAM role and Pod Identity association.
2. Create a sandbox with an `s3` volume.
3. Run a command that writes a file under the mount; confirm the object in the bucket.
4. Delete the sandbox; confirm the PV and PVC are gone.

## Drawbacks

- The server now creates cluster-scoped objects (PVs), which needs wider RBAC and its own cleanup path, including a startup sweep, because Kubernetes garbage collection cannot own them.
- The feature is cloud-specific. It works on EKS with an AWS add-on, so the Kubernetes runtime gains a backend that not every Kubernetes deployment can use, and an operator prerequisite that the server can only detect, not install.
- Mountpoint semantics are weaker than POSIX. Applications that append or edit in place break inside the mount, which the API cannot express.
- Sandboxes share one IAM role until per-tenant identity lands, so the blast radius of a bucket grant is the whole cluster.
- One PV and one PVC per volume per sandbox adds API objects proportional to sandbox churn.

## Alternatives

- **FUSE sidecar in the pod** (`mount-s3` or `s3fs` with bidirectional mount propagation). Gives per-pod identity for free and more POSIX-like behavior with s3fs. Rejected: it needs `/dev/fuse` and `SYS_ADMIN` or `privileged`, which conflicts with the secure runtime posture (OSEP-0004) and fails on gVisor and Kata; the project would also own a sidecar image.
- **preStart lifecycle hook that runs the mount tool.** Rejected for the same FUSE and capability reasons, plus the sandbox image would have to include the tool.
- **Operator pre-created S3 PVs referenced through the `pvc` backend.** Needs no API change but gives no per-sandbox prefix isolation. Rejected as the primary path; it keeps working as-is for operators who want it.

## Infrastructure Needed

- The **Mountpoint for Amazon S3 CSI driver** EKS add-on (`aws-mountpoint-s3-csi-driver`) installed in the cluster, in `kube-system` with ServiceAccount `s3-csi-driver-sa`.
- One **IAM role** with the S3 list and object permissions above, bound to that ServiceAccount through an EKS Pod Identity association or IRSA.
- An **S3 bucket** per deployment for sandbox output, plus an EKS cluster for the manual verification run. No new services, repositories, or third-party libraries.

## Upgrade & Migration Strategy

The change is additive and there is nothing to migrate. Existing volume backends, requests, and stored sandboxes are unaffected, and a client that never sends `s3` sees no behavior change.

Operators who want the backend install the CSI add-on and the IAM role, then upgrade the Helm chart to pick up the new RBAC rules; the optional `[storage]` keys default to the values above. A server without the CSI add-on stays fully functional and rejects `s3` requests with `VOLUME::UNSUPPORTED_BACKEND`. Downgrading is safe once no `s3` sandboxes are live; leftover PVs and PVCs carry the `opensandbox.io/volume-managed-by=server` label and can be deleted with a label selector.
