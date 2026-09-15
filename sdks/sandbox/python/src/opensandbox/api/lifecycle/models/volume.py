#
# Copyright 2026 Alibaba Group Holding Ltd.
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
#

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.host import Host
    from ..models.ossfs import OSSFS
    from ..models.pvc import PVC
    from ..models.s3 import S3


T = TypeVar("T", bound="Volume")


@_attrs_define
class Volume:
    """Storage mount definition for a sandbox. Each volume entry contains:
    - A unique name identifier
    - Exactly one backend struct (host, pvc, ossfs, s3) with backend-specific fields
    - Common mount settings (mountPath, readOnly, subPath)

        Attributes:
            name (str): Unique identifier for the volume within the sandbox.
                Must be a valid DNS label (lowercase alphanumeric, hyphens allowed, max 63 chars).
            mount_path (str): Absolute path inside the container where the volume is mounted.
                Must start with '/'.
            host (Host | Unset): Host path bind mount backend. Maps a directory on the host filesystem
                into the container. Only available when the runtime supports host mounts.

                Security note: Host paths are restricted by server-side allowlist.
                Users must specify paths under permitted prefixes.
            pvc (PVC | Unset): Platform-managed named volume backend. A runtime-neutral abstraction
                for referencing a platform-managed named volume. If `createIfNotExists`
                is true (the default) and the volume does not yet exist, it will be
                created automatically using the provisioning hints below.

                - Kubernetes: maps to a PersistentVolumeClaim in the same namespace.
                - Docker: maps to a Docker named volume (created via `docker volume create`).
            ossfs (OSSFS | Unset): Alibaba Cloud OSS mount backend via ossfs.

                The runtime mounts a host-side OSS path under `storage.ossfs_mount_root`
                and bind-mounts the resolved path into the sandbox container.
                Prefix selection is expressed via `Volume.subPath`.
                In Docker runtime, OSSFS backend requires OpenSandbox Server to run on a Linux host with FUSE support.
            s3 (S3 | Unset): Amazon S3 mount backend. Kubernetes runtime only.

                The server creates a static PersistentVolume for the Mountpoint for Amazon S3
                CSI driver and a bound PersistentVolumeClaim, then mounts the claim into the
                sandbox. Credentials come from the IAM role bound to the CSI driver
                ServiceAccount (EKS Pod Identity or IRSA); the request carries none.

                Mountpoint semantics: new files are written sequentially and appear on close;
                overwrite (truncate) and delete are allowed for read-write mounts; append to an
                existing object, random writes and rename are not supported.
            read_only (bool | Unset): If true, the volume is mounted as read-only. Defaults to false (read-write).
                 Default: False.
            sub_path (str | Unset): Optional subdirectory under the backend path to mount.
                For `ossfs` backend, this field is used as the bucket prefix.
                Not allowed for the `s3` backend; use `s3.prefix`.
                Must be a relative path without '..' components.
    """

    name: str
    mount_path: str
    host: Host | Unset = UNSET
    pvc: PVC | Unset = UNSET
    ossfs: OSSFS | Unset = UNSET
    s3: S3 | Unset = UNSET
    read_only: bool | Unset = False
    sub_path: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        mount_path = self.mount_path

        host: dict[str, Any] | Unset = UNSET
        if not isinstance(self.host, Unset):
            host = self.host.to_dict()

        pvc: dict[str, Any] | Unset = UNSET
        if not isinstance(self.pvc, Unset):
            pvc = self.pvc.to_dict()

        ossfs: dict[str, Any] | Unset = UNSET
        if not isinstance(self.ossfs, Unset):
            ossfs = self.ossfs.to_dict()

        s3: dict[str, Any] | Unset = UNSET
        if not isinstance(self.s3, Unset):
            s3 = self.s3.to_dict()

        read_only = self.read_only

        sub_path = self.sub_path

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "name": name,
                "mountPath": mount_path,
            }
        )
        if host is not UNSET:
            field_dict["host"] = host
        if pvc is not UNSET:
            field_dict["pvc"] = pvc
        if ossfs is not UNSET:
            field_dict["ossfs"] = ossfs
        if s3 is not UNSET:
            field_dict["s3"] = s3
        if read_only is not UNSET:
            field_dict["readOnly"] = read_only
        if sub_path is not UNSET:
            field_dict["subPath"] = sub_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.host import Host
        from ..models.ossfs import OSSFS
        from ..models.pvc import PVC
        from ..models.s3 import S3

        d = dict(src_dict)
        name = d.pop("name")

        mount_path = d.pop("mountPath")

        _host = d.pop("host", UNSET)
        host: Host | Unset
        if isinstance(_host, Unset):
            host = UNSET
        else:
            host = Host.from_dict(_host)

        _pvc = d.pop("pvc", UNSET)
        pvc: PVC | Unset
        if isinstance(_pvc, Unset):
            pvc = UNSET
        else:
            pvc = PVC.from_dict(_pvc)

        _ossfs = d.pop("ossfs", UNSET)
        ossfs: OSSFS | Unset
        if isinstance(_ossfs, Unset):
            ossfs = UNSET
        else:
            ossfs = OSSFS.from_dict(_ossfs)

        _s3 = d.pop("s3", UNSET)
        s3: S3 | Unset
        if isinstance(_s3, Unset):
            s3 = UNSET
        else:
            s3 = S3.from_dict(_s3)

        read_only = d.pop("readOnly", UNSET)

        sub_path = d.pop("subPath", UNSET)

        volume = cls(
            name=name,
            mount_path=mount_path,
            host=host,
            pvc=pvc,
            ossfs=ossfs,
            s3=s3,
            read_only=read_only,
            sub_path=sub_path,
        )

        return volume
