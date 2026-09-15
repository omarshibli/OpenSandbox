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
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import HTTPException, status
from kubernetes.client import ApiException

from opensandbox_server.api.schema import Volume
from opensandbox_server.config import StorageConfig
from opensandbox_server.services.constants import (
    SANDBOX_ID_LABEL,
    SANDBOX_MANAGED_VOLUMES_LABEL,
    SandboxErrorCodes,
)

logger = logging.getLogger(__name__)

_READ_WRITE_ACCESS_MODES = ["ReadWriteMany"]
_READ_ONLY_ACCESS_MODES = ["ReadOnlyMany"]
# One line of ``get_sandbox_events`` output: "[<timestamp>] <TYPE> <REASON> <MESSAGE>".
# The timestamp may contain spaces, so anchor on the closing bracket.
_EVENT_LINE_RE = re.compile(r"^\[.*?\]\s+(\S+)\s+(\S+)\s+(.*)$")
# ``build_s3_pv_body`` pre-sets ``claimRef``, so between ``create_pv`` and
# ``create_pvc`` a healthy PV looks exactly like an orphan. The sweep only
# deletes a still-Available PV once it is older than this, which keeps a
# restarting replica from deleting another replica's in-flight PV.
S3_ORPHAN_PV_MIN_AGE_SECONDS = 600
# Phases that mean the PVC is definitively gone; age no longer matters.
_S3_ORPHAN_PV_TERMINAL_PHASES = ("Released", "Failed")


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


def _is_older_than(pv: Any, seconds: int) -> bool:
    """
    True when the PV's creationTimestamp is more than ``seconds`` in the past.
    A missing or unreadable timestamp counts as "too young to delete".
    """
    created = getattr(getattr(pv, "metadata", None), "creation_timestamp", None)
    if not isinstance(created, datetime):
        return False
    now = datetime.now(timezone.utc) if created.tzinfo is not None else datetime.now()
    return (now - created).total_seconds() > seconds


def has_s3_volumes(volumes: Optional[List[Volume]]) -> bool:
    return any(v.s3 is not None for v in (volumes or []))


class S3VolumeProvisioner:
    """
    Creates, reuses and removes the PV/PVC pair behind each ``s3`` volume.

    PVCs carry the same managed labels as server-created PVCs, so the
    existing label sweep and ``ownerReferences`` GC remove them. PVs are
    cluster-scoped and cannot be owned by a namespaced CR, so ``cleanup``
    deletes them explicitly and ``sweep_orphans`` catches leftovers at
    startup and on a timer.
    """

    def __init__(self, k8s_client: Any, storage: StorageConfig):
        self.k8s_client = k8s_client
        self.storage = storage
        self._driver_verified = False
        # True once this process touched an s3 object. Keeps ``cleanup`` from
        # LISTing PVs cluster-wide in deployments that never use s3 volumes.
        self._provisioned_any = False

    # -- driver -----------------------------------------------------------

    def ensure_driver_installed(self) -> None:
        """Fail with a clear error when the CSI driver is not installed. Positive result is cached."""
        if self._driver_verified:
            return
        driver_name = self.storage.s3_csi_driver
        try:
            driver = self.k8s_client.get_csi_driver(driver_name)
        except ApiException as e:
            if e.status == 403:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "code": SandboxErrorCodes.K8S_API_ERROR,
                        "message": (
                            f"Cannot verify CSI driver '{driver_name}': server lacks 'get' on "
                            "storage.k8s.io/csidrivers. Operator must grant the missing RBAC."
                        ),
                    },
                ) from e
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.K8S_API_ERROR,
                    "message": f"Failed to read CSI driver '{driver_name}': {e.reason or e}",
                },
            ) from e
        if driver is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": SandboxErrorCodes.UNSUPPORTED_VOLUME_BACKEND,
                    "message": (
                        f"The s3 volume backend requires the CSI driver '{driver_name}', which is not "
                        "installed in this cluster. Install the Mountpoint for Amazon S3 CSI driver "
                        "(EKS add-on 'aws-mountpoint-s3-csi-driver')."
                    ),
                },
            )
        self._driver_verified = True

    # -- provisioning -----------------------------------------------------

    def ensure(self, volumes: List[Volume], sandbox_id: str, namespace: str) -> List[str]:
        """
        Create the PV and PVC for every ``s3`` volume. Returns their claim
        names. On any failure, objects created by this call are deleted and
        the error is re-raised as an HTTPException.
        """
        created_pvs: List[str] = []
        created_pvcs: List[str] = []
        claims: List[str] = []
        try:
            for volume in volumes:
                if volume.s3 is None:
                    continue
                name = s3_object_name(sandbox_id, volume.name)
                if self._create_or_reuse_pv(build_s3_pv_body(volume, sandbox_id, namespace, self.storage), sandbox_id):
                    created_pvs.append(name)
                self._provisioned_any = True
                if self._create_or_reuse_pvc(
                    build_s3_pvc_body(volume, sandbox_id, namespace, self.storage), sandbox_id, namespace
                ):
                    created_pvcs.append(name)
                claims.append(name)
        except Exception:
            self._rollback(created_pvcs, created_pvs, namespace, sandbox_id)
            raise
        return claims

    def _create_or_reuse_pv(self, body: dict, sandbox_id: str) -> bool:
        """Returns True when the PV was created by this call, False when a same-sandbox leftover was reused."""
        name = body["metadata"]["name"]
        try:
            self.k8s_client.create_pv(body)
            logger.info(f"sandbox={sandbox_id} | created s3 PV '{name}'")
            return True
        except ApiException as e:
            if e.status == 409:
                existing = self.k8s_client.get_pv(name)
                self._assert_owned_by(existing, name, sandbox_id, kind="PersistentVolume")
                logger.info(f"sandbox={sandbox_id} | reusing existing s3 PV '{name}'")
                return False
            raise self._api_error("create", "persistentvolumes", name, e) from e

    def _create_or_reuse_pvc(self, body: dict, sandbox_id: str, namespace: str) -> bool:
        name = body["metadata"]["name"]
        try:
            self.k8s_client.create_pvc(namespace, body)
            logger.info(f"sandbox={sandbox_id} | created s3 PVC '{name}' in '{namespace}'")
            return True
        except ApiException as e:
            if e.status == 409:
                existing = self.k8s_client.get_pvc(namespace, name)
                self._assert_owned_by(existing, name, sandbox_id, kind="PersistentVolumeClaim")
                logger.info(f"sandbox={sandbox_id} | reusing existing s3 PVC '{name}'")
                return False
            raise self._api_error("create", "persistentvolumeclaims", name, e) from e

    @staticmethod
    def _assert_owned_by(obj: Any, name: str, sandbox_id: str, *, kind: str) -> None:
        if obj is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.K8S_API_ERROR,
                    "message": (
                        f"{kind} '{name}' reported a conflict on create but could not be "
                        "read back; retry the request."
                    ),
                },
            )
        labels = getattr(getattr(obj, "metadata", None), "labels", None) or {}
        if (
            labels.get(SANDBOX_ID_LABEL) != sandbox_id
            or labels.get(SANDBOX_MANAGED_VOLUMES_LABEL) != "server"
        ):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.K8S_API_ERROR,
                    "message": (
                        f"{kind} '{name}' already exists and is not managed by this sandbox "
                        f"('{sandbox_id}'). Refusing to reuse it."
                    ),
                },
            )

    @staticmethod
    def _api_error(verb: str, resource: str, name: str, e: ApiException) -> HTTPException:
        if e.status == 403:
            message = (
                f"Cannot {verb} {resource} '{name}': server lacks '{verb}' on {resource}. "
                "Operator must grant the missing RBAC."
            )
        else:
            message = f"Failed to {verb} {resource} '{name}': {e.reason or e}"
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": SandboxErrorCodes.K8S_API_ERROR, "message": message},
        )

    def _rollback(self, pvcs: List[str], pvs: List[str], namespace: str, sandbox_id: str) -> None:
        for name in pvcs:
            try:
                self.k8s_client.delete_pvc(namespace, name)
            except Exception as ex:
                logger.warning(f"sandbox={sandbox_id} | rollback: failed to delete s3 PVC '{name}': {ex}")
        for name in pvs:
            try:
                self.k8s_client.delete_pv(name)
            except Exception as ex:
                logger.warning(f"sandbox={sandbox_id} | rollback: failed to delete s3 PV '{name}': {ex}")

    # -- cleanup ----------------------------------------------------------

    def cleanup(self, sandbox_id: str) -> None:
        """
        Delete the PVs labeled for this sandbox. Best effort; never raises.

        Returns without a cluster-wide LIST when this process never saw an s3
        volume: a deployment without s3 volumes must not pay for (or need RBAC
        for) a PV list on every sandbox delete. Sandboxes provisioned before a
        restart are still cleaned up — their PVC goes with the label sweep and
        ``sweep_orphans`` removes the Released PV.
        """
        if not (self._driver_verified or self._provisioned_any):
            logger.debug(f"sandbox={sandbox_id} | no s3 volumes in this process; skipping s3 PV cleanup")
            return
        selector = f"{SANDBOX_MANAGED_VOLUMES_LABEL}=server,{SANDBOX_ID_LABEL}={sandbox_id}"
        try:
            pvs = self.k8s_client.list_pvs(label_selector=selector)
        except Exception as e:
            if getattr(e, "status", None) == 403:
                logger.debug(f"sandbox={sandbox_id} | no RBAC to list persistentvolumes; skipping s3 PV cleanup")
            else:
                logger.warning(f"sandbox={sandbox_id} | failed to list s3 PVs: {e}")
            return
        for pv in pvs:
            name = getattr(getattr(pv, "metadata", None), "name", None)
            if not name:
                continue
            try:
                self.k8s_client.delete_pv(name)
                logger.info(f"sandbox={sandbox_id} | deleted s3 PV '{name}'")
            except Exception as e:
                logger.warning(f"sandbox={sandbox_id} | failed to delete s3 PV '{name}': {e}")

    def sweep_orphans(self) -> int:
        """
        Delete server-managed PVs whose bound PVC no longer exists (for
        example, removed by ownerReference GC after a TTL expiry).

        A ``Bound`` PV is never deleted. A PV in any other phase is deleted
        only once the PVC is confirmed missing AND the PV is already
        ``Released``/``Failed`` or older than
        ``S3_ORPHAN_PV_MIN_AGE_SECONDS``, so a PV that ``ensure`` is still
        binding survives a concurrent sweep.

        Returns the number of PVs deleted. Best effort; never raises.
        """
        try:
            pvs = self.k8s_client.list_pvs(label_selector=f"{SANDBOX_MANAGED_VOLUMES_LABEL}=server")
        except Exception as e:
            logger.warning(f"s3 orphan sweep: failed to list PVs: {e}")
            return 0
        deleted = 0
        for pv in pvs:
            name = getattr(getattr(pv, "metadata", None), "name", None)
            claim_ref = getattr(getattr(pv, "spec", None), "claim_ref", None)
            claim_ns = getattr(claim_ref, "namespace", None)
            claim_name = getattr(claim_ref, "name", None)
            if not name or not claim_ns or not claim_name:
                continue
            phase = getattr(getattr(pv, "status", None), "phase", None)
            if phase == "Bound":
                continue
            try:
                if self.k8s_client.get_pvc(claim_ns, claim_name) is not None:
                    continue
                if phase not in _S3_ORPHAN_PV_TERMINAL_PHASES and not _is_older_than(
                    pv, S3_ORPHAN_PV_MIN_AGE_SECONDS
                ):
                    logger.debug(
                        f"s3 orphan sweep: keeping PV '{name}' (phase={phase}), younger than "
                        f"{S3_ORPHAN_PV_MIN_AGE_SECONDS}s and possibly still being bound"
                    )
                    continue
                self.k8s_client.delete_pv(name)
                deleted += 1
                logger.info(f"s3 orphan sweep: deleted PV '{name}' (PVC {claim_ns}/{claim_name} is gone)")
            except Exception as e:
                logger.warning(f"s3 orphan sweep: failed for PV '{name}': {e}")
        return deleted
