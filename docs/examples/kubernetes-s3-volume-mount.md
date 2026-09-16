---
title: Kubernetes S3
description: Mount an Amazon S3 bucket prefix into OpenSandbox containers on EKS with no access keys.
---

# Kubernetes S3 Volume Mount

This example mounts an S3 bucket prefix at a path inside a sandbox that runs on Amazon EKS. The server uses the [Mountpoint for Amazon S3 CSI driver](https://github.com/awslabs/mountpoint-s3-csi-driver). Credentials come from an IAM role bound to the driver ServiceAccount. The API request carries no keys.

The `s3` backend is available on the Kubernetes runtime only. The Docker and FastSandbox runtimes reject it with `VOLUME::UNSUPPORTED_BACKEND`.

## Prerequisites

### CSI driver

Install the EKS add-on `aws-mountpoint-s3-csi-driver`. It runs in `kube-system` with the ServiceAccount `s3-csi-driver-sa`.

```shell
aws eks create-addon --cluster-name <cluster> --addon-name aws-mountpoint-s3-csi-driver
```

### IAM role

Create one IAM role with this policy. Limit the resource ARNs to the buckets sandboxes may reach.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::my-team-sandbox-logs"] },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      "Resource": ["arn:aws:s3:::my-team-sandbox-logs/*"]
    }
  ]
}
```

Bind the role to the driver ServiceAccount with an EKS Pod Identity association (recommended):

```shell
aws eks create-pod-identity-association \
  --cluster-name <cluster> \
  --namespace kube-system \
  --service-account s3-csi-driver-sa \
  --role-arn arn:aws:iam::<account>:role/opensandbox-s3-volumes
```

IRSA also works: annotate the ServiceAccount with `eks.amazonaws.com/role-arn`.

### OpenSandbox Server

The stock Helm chart grants the RBAC the server needs (`persistentvolumes` and `storage.k8s.io/csidrivers`). Optional `[storage]` keys:

```toml
[storage]
s3_csi_driver = "s3.csi.aws.com"        # CSIDriver object name
s3_mount_options = ["uid=1000", "gid=1000"]  # added to every s3 mount
s3_allowed_buckets = ["my-team-sandbox-logs"]  # empty = any bucket
s3_orphan_sweep_interval_seconds = 900       # orphan PV sweep period; 0 disables it
```

## Create a sandbox with an S3 volume

```python
from opensandbox import Sandbox
from opensandbox.models.sandboxes import S3, Volume

sandbox = await Sandbox.create(
    image="python:3.12",
    volumes=[
        Volume(
            name="logs",
            s3=S3(bucket="my-team-sandbox-logs", prefix="sandboxes/task-001/", region="eu-west-1"),
            mount_path="/mnt/logs",
        ),
    ],
)

await sandbox.commands.run("echo hello > /mnt/logs/step-1.stdout")
```

The object `sandboxes/task-001/step-1.stdout` appears in the bucket when the file is closed.

## What the server creates

For each `s3` volume the server creates a `PersistentVolume` named `s3-<sandbox-id>-<volume-name>` (CSI driver `s3.csi.aws.com`, `bucketName`, mount options) and a `PersistentVolumeClaim` of the same name, then mounts the claim. Both objects carry `opensandbox.io/volume-managed-by=server` and `opensandbox.io/id=<sandbox-id>`. The server removes both objects when the sandbox is deleted. When a sandbox expires, Kubernetes garbage-collects the PVC through its `ownerReferences` and the `Released` PV is removed by the orphan sweep, which runs at startup and then every `s3_orphan_sweep_interval_seconds` (default 15 minutes). The sweep never touches a `Bound` PV, and it leaves a PV younger than 10 minutes alone unless it is already `Released` or `Failed`, so a volume that is still being bound is safe.

Mount options in order: `allow-other`; `allow-delete` and `allow-overwrite` (read-write) or `read-only`; `prefix <prefix>`; `region <region>`; operator `s3_mount_options`; request `options`. A request cannot set `prefix`, `region`, `read-only`, `allow-delete` or `allow-overwrite` in `options`.

## Write semantics

| Operation | Supported |
|---|---|
| Create a new file, write sequentially, close | Yes. The object appears on close |
| Overwrite an existing file (truncate) | Yes, read-write mounts |
| Delete | Yes, read-write mounts |
| Append to an existing closed file | No. S3 objects are immutable |
| Random-offset write, edit in place | No |
| Rename, symlink, hardlink | No |

For command logs, write **one object per command**, for example `/mnt/logs/<command-id>.stdout` and `.stderr`. A writer that must update a file rewrites the whole file.

## Errors

| Code | Cause |
|---|---|
| `VOLUME::INVALID_S3_BUCKET` | Invalid bucket name, or not in `s3_allowed_buckets` |
| `VOLUME::INVALID_S3_PREFIX` | Leading `/`, `..`, whitespace or shell characters, or over 1024 bytes |
| `VOLUME::INVALID_S3_REGION` | Not an AWS region format |
| `VOLUME::INVALID_S3_OPTION` | Leading `-`, shell characters, or a reserved option |
| `VOLUME::INVALID_SUB_PATH` | `subPath` given on an `s3` volume; use `s3.prefix` |
| `VOLUME::UNSUPPORTED_BACKEND` | Docker runtime, or the CSI driver is not installed |
| `KUBERNETES::POD_READY_TIMEOUT` | Mount failed on the node; the message includes the last `FailedMount` event, e.g. access denied |

## Manual verification on EKS

1. Install the add-on, create the role and the Pod Identity association.
2. Create a sandbox with an `s3` volume and run `echo ok > /mnt/logs/check.txt`.
3. `aws s3 ls s3://my-team-sandbox-logs/sandboxes/task-001/` shows `check.txt`.
4. Delete the sandbox. `kubectl get pv,pvc -l opensandbox.io/volume-managed-by=server` shows nothing for that sandbox id.
