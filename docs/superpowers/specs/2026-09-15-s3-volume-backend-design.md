# S3 Volume Backend for Kubernetes on AWS EKS

- Date: 2026-09-15
- Status: approved design, not implemented
- Related: OSEP-0003 (Volume Support), which names `s3` as a future backend

## Summary

Add an `s3` backend to `volumes[]` in the Lifecycle API. On the Kubernetes runtime the server realizes it with the AWS-supported **Mountpoint for Amazon S3 CSI driver**: for each `s3` volume the server creates a static `PersistentVolume` and a bound `PersistentVolumeClaim`, and the sandbox pod mounts the claim. Credentials never appear in the API or in Kubernetes Secrets. One IAM role, bound to the CSI driver ServiceAccount through EKS Pod Identity or IRSA, gives every sandbox its S3 access.

The Docker and FastSandbox runtimes reject `s3` in this phase.

## Motivation

The existing object-storage backend, `ossfs`, is Alibaba-only, Docker-only, and requires inline access keys in the request. Deployments on AWS EKS have no way to mount a bucket into a sandbox. The first concrete need is to sync command output (stdout, stderr) to S3. The design keeps the API open for other uses, such as dataset input and artifact output.

### Goals

- Mount an S3 bucket, or a prefix inside it, at a path inside a sandbox on EKS.
- No access keys anywhere: not in the API, not in Secrets, not in config.
- Additive API change; existing clients keep working.
- No privileged sandbox pods, no FUSE device in the sandbox, compatible with gVisor and Kata runtime classes.
- Clear validation errors when the runtime or cluster cannot serve the request.

### Non-Goals

- Docker runtime support (a later phase can add `mount-s3` on the host with the EC2 instance profile).
- S3-compatible stores such as MinIO (a later `endpoint` field can add this).
- Per-tenant IAM roles (a later `authenticationSource: pod` extension can add this without an API change).
- Full POSIX semantics. Mountpoint semantics are documented and accepted.
- Changes to the fast-sandbox template publish path or to snapshots.

## API

### Schema

Add an `S3` schema to `specs/sandbox-lifecycle.yml` next to `OSSFS`, and an `s3` property to `Volume`.

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
| `options` | []string | no | Same rules as `ossfs.options`: no leading `-`, no `; & \| \` $ ( ) < > \n \r`. Reserved names are rejected (see below). |

Decisions:

- **`prefix` lives inside `s3`.** It uses the S3 term and gets its own validation. `subPath` on an `s3` volume is rejected with `VOLUME::INVALID_SUB_PATH` and a message that points to `s3.prefix`. A Kubernetes `volumeMounts.subPath` on a FUSE mount fails when the prefix has no objects yet, so the server never emits one for `s3`.
- **No credential fields.**
- **Exactly one backend** per volume; `s3` joins `host`, `pvc`, `ossfs` in that check.
- **Reserved options** owned by the server: `prefix`, `region`, `read-only`, `allow-delete`, `allow-overwrite`. A request that passes one gets `VOLUME::INVALID_S3_OPTION`.

### SDKs

Each SDK (Python, TypeScript, Go, C#, Kotlin) gets an `S3` model with the four fields and a `Volume.s3` field, mirroring the existing `OSSFS` model and converters.

## Server

All new S3 logic lives in one new module, `server/opensandbox_server/services/k8s/s3_volume.py`. Other files get small additive edits.

### Config

New keys in `StorageConfig` (`server/opensandbox_server/config.py`), documented in `server/configuration.md`:

| Key | Default | Purpose |
|---|---|---|
| `s3_csi_driver` | `"s3.csi.aws.com"` | Name of the `CSIDriver` object to check and to put in the PV. |
| `s3_mount_options` | `[]` | Operator defaults added to every S3 mount, for example `uid=1000`. Same validation as request options; reserved names rejected at startup. |
| `s3_allowed_buckets` | `[]` | Optional allowlist. Empty means any bucket. The IAM role is the real boundary. |

### Request path

1. **Parse.** `api/schema.py`: `S3` Pydantic model, `Volume.s3`, exactly-one-backend validator extended.
2. **Validate.** `services/validators.py`: `ensure_valid_s3_volume`, called from `ensure_volumes_valid`, which also rejects `subPath` for `s3`. Error codes in `services/constants.py`.
3. **Runtime gate.** `services/docker/volumes.py` raises `VOLUME::UNSUPPORTED_BACKEND` for `s3`. FastSandbox already rejects all volumes.
4. **Driver check.** On the first `s3` request, the server reads `storage.k8s.io/v1 CSIDriver <s3_csi_driver>`. A positive result is cached for the process lifetime. If missing, the request fails with `VOLUME::UNSUPPORTED_BACKEND` and a message that names the add-on to install.
5. **Provision.** `_ensure_s3_volumes(volumes, sandbox_id)` runs after `_ensure_pvc_volumes` and before workload creation. For each `s3` volume it creates one PV and one PVC (see Kubernetes objects). If any create fails, the server deletes the objects it created in this request and re-raises.
6. **Pod spec.** `services/k8s/volume_helper.py`: an `s3` branch emits a `persistentVolumeClaim` source that points at the generated claim, with `readOnly` from the volume, and a mount with `mountPath` and `readOnly`. No `subPath`.
7. **Ownership.** Generated PVCs join the list that receives `ownerReferences` to the workload CR, the same as managed PVCs today. PVs are cluster-scoped and cannot have a namespaced owner; they need explicit cleanup.

### Naming

`s3-<sandbox-id>-<volume-name>` for both PV, PVC, and `volumeHandle`. The sandbox id is a UUID (36 chars) and the volume name is a DNS label (max 63), so the result is at most 103 characters and a valid DNS subdomain.

### Cleanup

- `_cleanup_managed_pvcs(sandbox_id)` also deletes PVs labeled `opensandbox.io/volume-managed-by=server` and `opensandbox.io/id=<sandbox-id>`. It runs on delete, on expiry, and on create failure, as today. Deletion is best effort and logged; 404 is success.
- **Startup sweep.** On start, the server lists PVs with `opensandbox.io/volume-managed-by=server`. For each PV whose `claimRef` PVC no longer exists, it deletes the PV. This covers PVCs removed by controller garbage collection while the server was down.

### RBAC

The server Helm chart ClusterRole adds:

- `persistentvolumes`: `create`, `delete`, `get`, `list`
- `storage.k8s.io` `csidrivers`: `get`

## Kubernetes objects

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

## Identity (operator setup)

Documented in `docs/examples/kubernetes-s3-volume-mount.md`.

1. Install the **Mountpoint for Amazon S3 CSI driver** EKS add-on (`aws-mountpoint-s3-csi-driver`). It runs in `kube-system` with ServiceAccount `s3-csi-driver-sa`.
2. Create one IAM role with `s3:ListBucket` on the bucket ARNs and `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload` on the object ARNs. Limit resources to the buckets sandboxes may reach.
3. Bind the role to that ServiceAccount with an **EKS Pod Identity association** (recommended) or an IRSA `eks.amazonaws.com/role-arn` annotation.

The sandbox pod needs no ServiceAccount change, no capabilities, and no FUSE device. The kubelet performs the mount on the node. gVisor and Kata receive the mount as a host directory.

Future extension, no API change: `volumeAttributes.authenticationSource: pod` plus an annotated sandbox ServiceAccount gives one role per tenant.

## Mountpoint semantics (user-facing contract)

| Operation | Supported | Note |
|---|---|---|
| Create a new file, write sequentially, close | yes | Uploaded as a multipart upload; object visible on close |
| Overwrite an existing file (open with truncate) | yes | Requires `allow-overwrite`, set by default for read-write |
| Delete | yes | Requires `allow-delete`, set by default for read-write |
| Append to an existing closed file | no | S3 objects are immutable |
| Random-offset write, in-place edit | no | Same reason |
| Rename, symlink, hardlink | no | S3 has no rename |

Recommended log pattern: **one object per command**, for example `/mnt/logs/<command-id>.stdout` and `.stderr`. A writer that needs to update a file must rewrite the whole file.

## Error handling

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

- **Create fails.** Roll back objects created in this request, return `K8S_API_ERROR` with the API message. A 403 names the missing RBAC verb, like the PVC code today.
- **409 on create.** The name embeds the sandbox id, so a conflict is a leftover from an earlier attempt for the same id. Read the existing object; if its `opensandbox.io/id` label matches, reuse it; otherwise return 500 with a clear message.
- **Mount fails on the node** (wrong bucket, IAM denied). The kubelet emits a `FailedMount` event and the pod never becomes ready. The readiness loop in `_wait_for_sandbox_ready` reads only the workload status message today. On a readiness timeout for a sandbox that has an `s3` volume, the server reads pod events through the existing `get_sandbox_events` diagnostics helper and appends the last `FailedMount` message to the `K8S_POD_READY_TIMEOUT` detail. The existing cleanup path then removes the PV and PVC.

Cleanup: best effort, logged, 404 is success; the startup sweep catches leftovers.

## Testing

Unit tests in `server/tests/`, following the `ossfs` and PVC test shapes:

- **Schema** (`test_schema.py`): valid `s3` volume, serialization round trip, `s3` plus another backend rejected, unknown field rejected.
- **Validators** (`test_validators.py`): one test per error code, allowlist, reserved options, `subPath` rejection, prefix normalization.
- **Pod spec** (`test_batchsandbox_provider.py`): PVC source and mount for `s3`, with and without `readOnly`, no `subPath`; multiple `s3` volumes; internal name conflict.
- **Provisioning** (new `test_s3_volume.py`): PV and PVC bodies match this spec exactly, including option order and read-only handling; rollback on partial failure; 409 reuse and mismatch; missing `CSIDriver`; cleanup deletes PV and PVC; startup sweep deletes an orphan PV and keeps a bound one; timeout detail includes the `FailedMount` message.
- **Runtime gate** (`test_docker_service.py`): `s3` returns `UNSUPPORTED_BACKEND`.
- **SDKs**: model tests per SDK, like the `OSSFS` tests.

End-to-end: the Kind e2e suite cannot run this (no AWS credentials, no `endpoint` field for MinIO). Manual verification on EKS, recorded in the docs page:

1. Install the add-on and create the IAM role and Pod Identity association.
2. Create a sandbox with an `s3` volume.
3. Run a command that writes a file under the mount; confirm the object in the bucket.
4. Delete the sandbox; confirm the PV and PVC are gone.

## Deliverables

- `specs/sandbox-lifecycle.yml`: `S3` schema and `Volume.s3`.
- Server: schema, validators, constants, config, Docker gate, `s3_volume.py`, `volume_helper.py` branch, cleanup and startup sweep, timeout detail.
- SDKs: `S3` model and converters in Python, TypeScript, Go, C#, Kotlin.
- `manifests/charts/server`: RBAC rules (`templates/rbac.yaml`) and a commented `[storage]` block in `configToml` (`values.yaml`).
- `docs/examples/kubernetes-s3-volume-mount.md`, `docs/examples/index.md`, VitePress nav.
- `server/configuration.md`: three new `[storage]` keys.
- `docs/architecture/index.md`: list volume backends per runtime instead of the current blanket statement.
- A new OSEP recording this design.

## Alternatives considered

- **FUSE sidecar in the pod** (`mount-s3` or `s3fs` with bidirectional mount propagation). Gives per-pod identity for free and more POSIX-like behavior with s3fs. Rejected: needs `/dev/fuse` and `SYS_ADMIN` or `privileged`, which conflicts with the secure runtime posture (OSEP-0004) and fails on gVisor and Kata; the project would own a sidecar image.
- **preStart lifecycle hook that runs the mount tool.** Rejected for the same FUSE and capability reasons, plus the sandbox image must include the tool.
- **Operator pre-created S3 PVs referenced through the `pvc` backend.** Needs no API change but gives no per-sandbox prefix isolation. Rejected as the primary path; it keeps working as-is for operators who want it.
