# Copyright 2025 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
S3 volume backend for the Kubernetes runtime.

Realizes ``volumes[].s3`` with the Mountpoint for Amazon S3 CSI driver: one
static PersistentVolume and one bound PersistentVolumeClaim per volume, both
labeled as server-managed. Credentials never appear here; the CSI driver's
ServiceAccount carries the IAM role (EKS Pod Identity or IRSA).
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from opensandbox_server.api.schema import Volume
from opensandbox_server.config import StorageConfig
from opensandbox_server.services.constants import SANDBOX_ID_LABEL, SANDBOX_MANAGED_VOLUMES_LABEL

logger = logging.getLogger(__name__)

_READ_WRITE_ACCESS_MODES = ["ReadWriteMany"]
_READ_ONLY_ACCESS_MODES = ["ReadOnlyMany"]
# One line of ``get_sandbox_events`` output: "[<timestamp>] <TYPE> <REASON> <MESSAGE>".
# The timestamp may contain spaces, so anchor on the closing bracket.
_EVENT_LINE_RE = re.compile(r"^\[.*?\]\s+(\S+)\s+(\S+)\s+(.*)$")


def s3_object_name(sandbox_id: str, volume_name: str) -> str:
    """Name shared by the PV, the PVC and the CSI volumeHandle of one s3 volume."""
    return f"s3-{sandbox_id}-{volume_name}"


def normalize_s3_prefix(prefix: Optional[str]) -> Optional[str]:
    """Return the prefix with exactly one trailing '/', or None when empty."""
    if not prefix:
        return None
    return prefix if prefix.endswith("/") else f"{prefix}/"


def build_s3_mount_options(volume: Volume, operator_options: List[str]) -> List[str]:
    """
    Mountpoint options in precedence order: server defaults, operator
    ``storage.s3_mount_options``, then request ``s3.options``. Mountpoint
    takes the last value when an option repeats.
    """
    assert volume.s3 is not None
    options: List[str] = ["allow-other"]
    if volume.read_only:
        options.append("read-only")
    else:
        options.extend(["allow-delete", "allow-overwrite"])
    prefix = normalize_s3_prefix(volume.s3.prefix)
    if prefix is not None:
        options.append(f"prefix {prefix}")
    if volume.s3.region:
        options.append(f"region {volume.s3.region}")
    options.extend(opt.strip() for opt in operator_options)
    options.extend(opt.strip() for opt in (volume.s3.options or []))
    return options


def _managed_labels(sandbox_id: str) -> dict:
    return {SANDBOX_MANAGED_VOLUMES_LABEL: "server", SANDBOX_ID_LABEL: sandbox_id}


def _access_modes(volume: Volume) -> List[str]:
    return list(_READ_ONLY_ACCESS_MODES if volume.read_only else _READ_WRITE_ACCESS_MODES)


def build_s3_pv_body(volume: Volume, sandbox_id: str, namespace: str, storage: StorageConfig) -> dict:
    """Static PersistentVolume for the Mountpoint CSI driver, pre-bound to its PVC via claimRef."""
    assert volume.s3 is not None
    name = s3_object_name(sandbox_id, volume.name)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": name, "labels": _managed_labels(sandbox_id)},
        "spec": {
            # Required by the API; Mountpoint ignores it.
            "capacity": {"storage": storage.volume_default_size},
            "accessModes": _access_modes(volume),
            # The driver has no controller service, so reclaim=Delete would
            # leave the PV Failed. The server deletes the PV explicitly.
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "",
            "claimRef": {"namespace": namespace, "name": name},
            "mountOptions": build_s3_mount_options(volume, storage.s3_mount_options),
            "csi": {
                "driver": storage.s3_csi_driver,
                "volumeHandle": name,
                "volumeAttributes": {"bucketName": volume.s3.bucket},
            },
        },
    }


def build_s3_pvc_body(volume: Volume, sandbox_id: str, namespace: str, storage: StorageConfig) -> dict:
    """PersistentVolumeClaim bound directly to the PV of the same name."""
    name = s3_object_name(sandbox_id, volume.name)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace, "labels": _managed_labels(sandbox_id)},
        "spec": {
            "accessModes": _access_modes(volume),
            "storageClassName": "",
            "volumeName": name,
            "resources": {"requests": {"storage": storage.volume_default_size}},
        },
    }


def extract_failed_mount_message(events_text: str) -> Optional[str]:
    """
    Pick the message of the last ``FailedMount`` line from the text produced
    by ``K8sDiagnosticsMixin.get_sandbox_events`` (format:
    ``[ts] TYPE REASON MESSAGE``). Returns None when there is none.
    """
    last: Optional[str] = None
    for line in events_text.splitlines():
        match = _EVENT_LINE_RE.match(line)
        if match and match.group(2) == "FailedMount":
            last = match.group(3).strip()
    return last
