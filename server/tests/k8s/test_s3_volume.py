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

from typing import Any

from opensandbox_server.api.schema import S3, Volume
from opensandbox_server.config import StorageConfig
from opensandbox_server.services.constants import SANDBOX_ID_LABEL, SANDBOX_MANAGED_VOLUMES_LABEL
from opensandbox_server.services.k8s.s3_volume import (
    build_s3_mount_options,
    build_s3_pv_body,
    build_s3_pvc_body,
    extract_failed_mount_message,
    normalize_s3_prefix,
    s3_object_name,
)

SANDBOX_ID = "abc123"
NS = "sandboxes"


def _volume(**kwargs: Any) -> Volume:
    s3_kwargs: dict[str, Any] = {"bucket": "my-team-sandbox-logs"}
    for key in ("prefix", "region", "options"):
        if key in kwargs:
            s3_kwargs[key] = kwargs.pop(key)
    volume_kwargs: dict[str, Any] = {"name": "logs", "mount_path": "/mnt/logs", **kwargs}
    return Volume(s3=S3(**s3_kwargs), **volume_kwargs)


class TestNaming:
    def test_object_name(self):
        assert s3_object_name("abc123", "logs") == "s3-abc123-logs"

    def test_uuid_sandbox_id_stays_under_limit(self):
        name = s3_object_name("0f2c6a5e-6d8f-4d3e-9a5c-1b2c3d4e5f60", "a" * 63)
        assert len(name) <= 253


class TestPrefix:
    def test_none_stays_none(self):
        assert normalize_s3_prefix(None) is None

    def test_empty_stays_none(self):
        assert normalize_s3_prefix("") is None

    def test_trailing_slash_added(self):
        assert normalize_s3_prefix("sandboxes/task-001") == "sandboxes/task-001/"

    def test_trailing_slash_kept(self):
        assert normalize_s3_prefix("sandboxes/task-001/") == "sandboxes/task-001/"


class TestMountOptions:
    def test_read_write_defaults(self):
        assert build_s3_mount_options(_volume(), []) == ["allow-other", "allow-delete", "allow-overwrite"]

    def test_read_only(self):
        assert build_s3_mount_options(_volume(read_only=True), []) == ["allow-other", "read-only"]

    def test_prefix_and_region_after_defaults(self):
        opts = build_s3_mount_options(_volume(prefix="sandboxes/task-001", region="eu-west-1"), [])
        assert opts == [
            "allow-other",
            "allow-delete",
            "allow-overwrite",
            "prefix sandboxes/task-001/",
            "region eu-west-1",
        ]

    def test_operator_then_request_options_last(self):
        opts = build_s3_mount_options(_volume(options=["uid=2000"]), ["uid=1000", "gid=1000"])
        assert opts[-3:] == ["uid=1000", "gid=1000", "uid=2000"]


class TestPvBody:
    def test_read_write_pv(self):
        storage = StorageConfig()
        body = build_s3_pv_body(_volume(prefix="sandboxes/task-001/", region="eu-west-1"), SANDBOX_ID, NS, storage)
        assert body == {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {
                "name": "s3-abc123-logs",
                "labels": {SANDBOX_MANAGED_VOLUMES_LABEL: "server", SANDBOX_ID_LABEL: "abc123"},
            },
            "spec": {
                "capacity": {"storage": "1Gi"},
                "accessModes": ["ReadWriteMany"],
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": "",
                "claimRef": {"namespace": NS, "name": "s3-abc123-logs"},
                "mountOptions": [
                    "allow-other",
                    "allow-delete",
                    "allow-overwrite",
                    "prefix sandboxes/task-001/",
                    "region eu-west-1",
                ],
                "csi": {
                    "driver": "s3.csi.aws.com",
                    "volumeHandle": "s3-abc123-logs",
                    "volumeAttributes": {"bucketName": "my-team-sandbox-logs"},
                },
            },
        }

    def test_read_only_pv_uses_read_only_many(self):
        body = build_s3_pv_body(_volume(read_only=True), SANDBOX_ID, NS, StorageConfig())
        assert body["spec"]["accessModes"] == ["ReadOnlyMany"]
        assert body["spec"]["mountOptions"] == ["allow-other", "read-only"]

    def test_driver_name_and_capacity_from_config(self):
        storage = StorageConfig(s3_csi_driver="custom.csi", volume_default_size="5Gi")
        body = build_s3_pv_body(_volume(), SANDBOX_ID, NS, storage)
        assert body["spec"]["csi"]["driver"] == "custom.csi"
        assert body["spec"]["capacity"] == {"storage": "5Gi"}


class TestPvcBody:
    def test_read_write_pvc(self):
        body = build_s3_pvc_body(_volume(), SANDBOX_ID, NS, StorageConfig())
        assert body == {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": "s3-abc123-logs",
                "namespace": NS,
                "labels": {SANDBOX_MANAGED_VOLUMES_LABEL: "server", SANDBOX_ID_LABEL: "abc123"},
            },
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": "",
                "volumeName": "s3-abc123-logs",
                "resources": {"requests": {"storage": "1Gi"}},
            },
        }

    def test_read_only_pvc_mirrors_pv(self):
        body = build_s3_pvc_body(_volume(read_only=True), SANDBOX_ID, NS, StorageConfig())
        assert body["spec"]["accessModes"] == ["ReadOnlyMany"]


class TestFailedMount:
    def test_returns_last_failed_mount_line_message(self):
        text = "\n".join([
            "[t1] Normal   Scheduled            Successfully assigned",
            "[t2] Warning  FailedMount          MountVolume.SetUp failed for volume \"logs\": access denied",
            "[t3] Warning  FailedMount          MountVolume.SetUp failed for volume \"logs\": no such bucket",
        ])
        assert extract_failed_mount_message(text) == 'MountVolume.SetUp failed for volume "logs": no such bucket'

    def test_handles_timestamps_with_spaces(self):
        text = "[2026-09-15 10:00:00+00:00] Warning  FailedMount          MountVolume.SetUp failed: access denied"
        assert extract_failed_mount_message(text) == "MountVolume.SetUp failed: access denied"

    def test_none_when_no_failed_mount(self):
        assert extract_failed_mount_message("[t1] Normal   Pulled   image pulled") is None
        assert extract_failed_mount_message("(no events)") is None
