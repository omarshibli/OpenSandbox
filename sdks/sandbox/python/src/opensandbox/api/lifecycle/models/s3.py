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
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="S3")


@_attrs_define
class S3:
    """Amazon S3 mount backend. Kubernetes runtime only.

    The server creates a static PersistentVolume for the Mountpoint for Amazon S3
    CSI driver and a bound PersistentVolumeClaim, then mounts the claim into the
    sandbox. Credentials come from the IAM role bound to the CSI driver
    ServiceAccount (EKS Pod Identity or IRSA); the request carries none.

    Mountpoint semantics: new files are written sequentially and appear on close;
    overwrite (truncate) and delete are allowed for read-write mounts; append to an
    existing object, random writes and rename are not supported.

        Attributes:
            bucket (str): S3 bucket name (S3 bucket naming rules).
            prefix (str | Unset): Optional key prefix inside the bucket to mount. Relative, no leading `/`,
                no `..` segments. The server appends a trailing `/` if absent.
            region (str | Unset): Optional AWS region of the bucket (e.g., `eu-west-1`). Detected by Mountpoint when absent.
            options (list[str] | Unset): Additional Mountpoint mount options as raw payloads without leading `-`
                (e.g., `uid=1000`). Server-owned options are rejected:
                `prefix`, `region`, `read-only`, `allow-delete`, `allow-overwrite`.
    """

    bucket: str
    prefix: str | Unset = UNSET
    region: str | Unset = UNSET
    options: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        bucket = self.bucket

        prefix = self.prefix

        region = self.region

        options: list[str] | Unset = UNSET
        if not isinstance(self.options, Unset):
            options = self.options

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "bucket": bucket,
            }
        )
        if prefix is not UNSET:
            field_dict["prefix"] = prefix
        if region is not UNSET:
            field_dict["region"] = region
        if options is not UNSET:
            field_dict["options"] = options

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        bucket = d.pop("bucket")

        prefix = d.pop("prefix", UNSET)

        region = d.pop("region", UNSET)

        options = cast(list[str], d.pop("options", UNSET))

        s3 = cls(
            bucket=bucket,
            prefix=prefix,
            region=region,
            options=options,
        )

        return s3
