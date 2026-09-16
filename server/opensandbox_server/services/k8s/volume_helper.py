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
Volume helper utilities for Kubernetes pod specs.
"""

import logging
from typing import Any, Dict, List, Optional

from opensandbox_server.api.schema import Volume
from opensandbox_server.services.k8s.s3_volume import s3_object_name

logger = logging.getLogger(__name__)


def _get_pvc_source_read_only_policies(volumes: List[Volume]) -> Dict[str, bool]:
    """Keep a PVC source read-only only when every mount of that claim is read-only."""
    policies: Dict[str, bool] = {}
    for vol in volumes:
        if vol.pvc is None:
            continue

        claim_name = vol.pvc.claim_name
        policies[claim_name] = policies.get(claim_name, True) and vol.read_only

    return policies


def apply_volumes_to_pod_spec(
    pod_spec: Dict[str, Any],
    volumes: List[Volume],
    sandbox_id: Optional[str] = None,
) -> None:
    """
    Apply user-specified volumes to a pod spec in-place.

    ``sandbox_id`` is required when any volume uses the ``s3`` backend: the
    claim name is derived from it (see ``s3_object_name``).
    """
    containers = pod_spec.get("containers", [])
    if not containers:
        logger.warning("No containers in pod spec, skipping volume mounts")
        return

    main_container = containers[0]
    mounts = main_container.get("volumeMounts", [])
    pod_volumes = pod_spec.get("volumes", [])

    existing_volume_names = {v.get("name") for v in pod_volumes if isinstance(v, dict)}
    pvc_to_volume_name: Dict[str, str] = {}
    pvc_source_read_only = _get_pvc_source_read_only_policies(volumes)

    for vol in volumes:
        vol_name = vol.name

        if vol_name in existing_volume_names:
            raise ValueError(
                f"Volume name '{vol_name}' conflicts with an internal volume. "
                "Please use a different volume name."
            )

        if vol.pvc is not None:
            pvc_claim_name = vol.pvc.claim_name

            if pvc_claim_name not in pvc_to_volume_name:
                pod_volumes.append({
                    "name": vol_name,
                    "persistentVolumeClaim": {
                        "claimName": pvc_claim_name,
                        "readOnly": pvc_source_read_only[pvc_claim_name],
                    },
                })
                pvc_to_volume_name[pvc_claim_name] = vol_name
                existing_volume_names.add(vol_name)

            mount = {
                "name": pvc_to_volume_name[pvc_claim_name],
                "mountPath": vol.mount_path,
                "readOnly": vol.read_only,
            }
            if vol.sub_path:
                mount["subPath"] = vol.sub_path
            mounts.append(mount)

            logger.info(
                f"Added PVC volume '{pvc_to_volume_name[pvc_claim_name]}' "
                f"(claim: {pvc_claim_name}, read_only={vol.read_only}) mounted "
                f"at '{vol.mount_path}' for sandbox"
            )
        elif vol.host is not None:
            host_path = vol.host.path

            pod_volumes.append({
                "name": vol_name,
                "hostPath": {
                    "path": host_path,
                    "type": "DirectoryOrCreate",
                },
            })

            mount = {
                "name": vol_name,
                "mountPath": vol.mount_path,
                "readOnly": vol.read_only,
            }
            if vol.sub_path:
                mount["subPath"] = vol.sub_path
            mounts.append(mount)

            logger.info(
                f"Added hostPath volume '{vol_name}' (path: {host_path}) mounted at '{vol.mount_path}' for sandbox"
            )
        elif vol.s3 is not None:
            if not sandbox_id:
                raise ValueError(
                    f"Volume '{vol_name}' uses the s3 backend, which requires sandbox_id "
                    "to derive the claim name."
                )

            claim_name = s3_object_name(sandbox_id, vol_name)

            pod_volumes.append({
                "name": vol_name,
                "persistentVolumeClaim": {
                    "claimName": claim_name,
                    "readOnly": vol.read_only,
                },
            })
            existing_volume_names.add(vol_name)

            mounts.append({
                "name": vol_name,
                "mountPath": vol.mount_path,
                "readOnly": vol.read_only,
            })

            logger.info(
                "Added s3 volume '%s' (bucket: %s, claim: %s, read_only=%s) mounted at '%s' for sandbox",
                vol_name,
                vol.s3.bucket,
                claim_name,
                vol.read_only,
                vol.mount_path,
            )
        else:
            raise ValueError(
                f"Volume '{vol_name}' has no supported backend specified. "
                "Supported backends: pvc, host, s3"
            )

    pod_spec["volumes"] = pod_volumes
    main_container["volumeMounts"] = mounts
