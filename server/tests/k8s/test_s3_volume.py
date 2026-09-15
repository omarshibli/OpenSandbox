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
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from kubernetes.client import ApiException

from opensandbox_server.api.schema import S3, Volume
from opensandbox_server.config import StorageConfig
from opensandbox_server.services.constants import (
    SANDBOX_ID_LABEL,
    SANDBOX_MANAGED_VOLUMES_LABEL,
    SandboxErrorCodes,
)
from opensandbox_server.services.k8s.s3_volume import (
    S3VolumeProvisioner,
    build_s3_mount_options,
    build_s3_pv_body,
    build_s3_pvc_body,
    extract_failed_mount_message,
    has_s3_volumes,
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


def _pv(name: str, sandbox_id: str, claim_ns: str = NS):
    pv = MagicMock()
    pv.metadata.name = name
    pv.metadata.labels = {SANDBOX_MANAGED_VOLUMES_LABEL: "server", SANDBOX_ID_LABEL: sandbox_id}
    pv.spec.claim_ref.namespace = claim_ns
    pv.spec.claim_ref.name = name
    return pv


@pytest.fixture
def provisioner():
    client = MagicMock()
    client.get_csi_driver.return_value = {"metadata": {"name": "s3.csi.aws.com"}}
    client.get_pv.return_value = None
    client.get_pvc.return_value = None
    return S3VolumeProvisioner(client, StorageConfig())


class TestHasS3Volumes:
    def test_true_when_any_s3(self):
        assert has_s3_volumes([_volume()]) is True

    def test_false_for_none_or_other(self):
        assert has_s3_volumes(None) is False
        assert has_s3_volumes([]) is False


class TestDriverCheck:
    def test_missing_driver_is_unsupported_backend(self, provisioner):
        provisioner.k8s_client.get_csi_driver.return_value = None
        with pytest.raises(HTTPException) as exc:
            provisioner.ensure_driver_installed()
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == SandboxErrorCodes.UNSUPPORTED_VOLUME_BACKEND
        assert "s3.csi.aws.com" in exc.value.detail["message"]

    def test_result_is_cached_after_success(self, provisioner):
        provisioner.ensure_driver_installed()
        provisioner.ensure_driver_installed()
        assert provisioner.k8s_client.get_csi_driver.call_count == 1

    def test_forbidden_is_api_error_naming_rbac(self, provisioner):
        provisioner.k8s_client.get_csi_driver.side_effect = ApiException(status=403)
        with pytest.raises(HTTPException) as exc:
            provisioner.ensure_driver_installed()
        assert exc.value.status_code == 500
        assert exc.value.detail["code"] == SandboxErrorCodes.K8S_API_ERROR
        assert "csidrivers" in exc.value.detail["message"]


class TestEnsure:
    def test_creates_pv_then_pvc_and_returns_claims(self, provisioner):
        claims = provisioner.ensure([_volume()], SANDBOX_ID, NS)

        assert claims == ["s3-abc123-logs"]
        pv_body = provisioner.k8s_client.create_pv.call_args.args[0]
        assert pv_body["metadata"]["name"] == "s3-abc123-logs"
        ns, pvc_body = provisioner.k8s_client.create_pvc.call_args.args
        assert ns == NS
        assert pvc_body["metadata"]["name"] == "s3-abc123-logs"

    def test_skips_non_s3_volumes(self, provisioner):
        from opensandbox_server.api.schema import PVC

        pvc_volume = Volume(name="d", pvc=PVC(claim_name="c"), mount_path="/d")
        assert provisioner.ensure([pvc_volume], SANDBOX_ID, NS) == []
        provisioner.k8s_client.create_pv.assert_not_called()

    def test_conflict_with_matching_label_is_reused(self, provisioner):
        provisioner.k8s_client.create_pv.side_effect = ApiException(status=409)
        provisioner.k8s_client.get_pv.return_value = _pv("s3-abc123-logs", SANDBOX_ID)

        claims = provisioner.ensure([_volume()], SANDBOX_ID, NS)

        assert claims == ["s3-abc123-logs"]
        provisioner.k8s_client.create_pvc.assert_called_once()

    def test_conflict_with_other_sandbox_label_fails(self, provisioner):
        provisioner.k8s_client.create_pv.side_effect = ApiException(status=409)
        provisioner.k8s_client.get_pv.return_value = _pv("s3-abc123-logs", "other-sandbox")

        with pytest.raises(HTTPException) as exc:
            provisioner.ensure([_volume()], SANDBOX_ID, NS)
        assert exc.value.status_code == 500
        assert exc.value.detail["code"] == SandboxErrorCodes.K8S_API_ERROR
        provisioner.k8s_client.create_pvc.assert_not_called()

    def test_pvc_failure_rolls_back_pv(self, provisioner):
        provisioner.k8s_client.create_pvc.side_effect = ApiException(status=500, reason="boom")

        with pytest.raises(HTTPException) as exc:
            provisioner.ensure([_volume()], SANDBOX_ID, NS)

        assert exc.value.detail["code"] == SandboxErrorCodes.K8S_API_ERROR
        provisioner.k8s_client.delete_pv.assert_called_once_with("s3-abc123-logs")

    def test_second_volume_failure_rolls_back_first(self, provisioner):
        provisioner.k8s_client.create_pv.side_effect = [None, ApiException(status=500, reason="boom")]
        volumes = [
            Volume(name="logs", s3=S3(bucket="bucket-one"), mount_path="/mnt/logs"),
            Volume(name="data", s3=S3(bucket="bucket-two"), mount_path="/mnt/data"),
        ]

        with pytest.raises(HTTPException):
            provisioner.ensure(volumes, SANDBOX_ID, NS)

        provisioner.k8s_client.delete_pvc.assert_called_once_with(NS, "s3-abc123-logs")
        provisioner.k8s_client.delete_pv.assert_called_once_with("s3-abc123-logs")

    def test_forbidden_create_names_missing_rbac(self, provisioner):
        provisioner.k8s_client.create_pv.side_effect = ApiException(status=403)
        with pytest.raises(HTTPException) as exc:
            provisioner.ensure([_volume()], SANDBOX_ID, NS)
        assert "persistentvolumes" in exc.value.detail["message"]


class TestCleanup:
    def test_deletes_labeled_pvs(self, provisioner):
        provisioner.k8s_client.list_pvs.return_value = [_pv("s3-abc123-logs", SANDBOX_ID)]

        provisioner.cleanup(SANDBOX_ID)

        provisioner.k8s_client.list_pvs.assert_called_once_with(
            label_selector=f"{SANDBOX_MANAGED_VOLUMES_LABEL}=server,{SANDBOX_ID_LABEL}={SANDBOX_ID}"
        )
        provisioner.k8s_client.delete_pv.assert_called_once_with("s3-abc123-logs")

    def test_errors_are_swallowed(self, provisioner):
        provisioner.k8s_client.list_pvs.side_effect = ApiException(status=500)
        provisioner.cleanup(SANDBOX_ID)  # no raise


class TestSweepOrphans:
    def test_deletes_pv_whose_pvc_is_gone(self, provisioner):
        provisioner.k8s_client.list_pvs.return_value = [_pv("s3-abc123-logs", SANDBOX_ID)]
        provisioner.k8s_client.get_pvc.return_value = None

        deleted = provisioner.sweep_orphans()

        assert deleted == 1
        provisioner.k8s_client.get_pvc.assert_called_once_with(NS, "s3-abc123-logs")
        provisioner.k8s_client.delete_pv.assert_called_once_with("s3-abc123-logs")

    def test_keeps_pv_whose_pvc_exists(self, provisioner):
        provisioner.k8s_client.list_pvs.return_value = [_pv("s3-abc123-logs", SANDBOX_ID)]
        provisioner.k8s_client.get_pvc.return_value = MagicMock()

        assert provisioner.sweep_orphans() == 0
        provisioner.k8s_client.delete_pv.assert_not_called()

    def test_only_lists_server_managed_pvs(self, provisioner):
        provisioner.k8s_client.list_pvs.return_value = []
        provisioner.sweep_orphans()
        provisioner.k8s_client.list_pvs.assert_called_once_with(label_selector=f"{SANDBOX_MANAGED_VOLUMES_LABEL}=server")
