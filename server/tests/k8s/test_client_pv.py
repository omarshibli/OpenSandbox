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

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import ApiException

from opensandbox_server.services.k8s.client import K8sClient


@pytest.fixture
def client(k8s_runtime_config):
    with patch("kubernetes.config.load_kube_config"):
        c = K8sClient(k8s_runtime_config)
    c._core_v1_api = MagicMock()
    c._storage_v1_api = MagicMock()
    return c


def test_create_pv_calls_core_api(client):
    body = {"metadata": {"name": "s3-abc-logs"}}
    client.create_pv(body)
    client._core_v1_api.create_persistent_volume.assert_called_once_with(body=body)


def test_get_pv_returns_none_on_404(client):
    client._core_v1_api.read_persistent_volume.side_effect = ApiException(status=404)
    assert client.get_pv("missing") is None


def test_get_pv_reraises_other_errors(client):
    client._core_v1_api.read_persistent_volume.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        client.get_pv("forbidden")


def test_list_pvs_returns_items(client):
    result = MagicMock()
    result.items = ["a", "b"]
    client._core_v1_api.list_persistent_volume.return_value = result
    assert client.list_pvs(label_selector="k=v") == ["a", "b"]
    client._core_v1_api.list_persistent_volume.assert_called_once_with(label_selector="k=v")


def test_delete_pv_swallows_404(client):
    client._core_v1_api.delete_persistent_volume.side_effect = ApiException(status=404)
    client.delete_pv("gone")  # no raise


def test_get_csi_driver_returns_none_on_404(client):
    client._storage_v1_api.read_csi_driver.side_effect = ApiException(status=404)
    assert client.get_csi_driver("s3.csi.aws.com") is None


def test_get_csi_driver_returns_object(client):
    client._storage_v1_api.read_csi_driver.return_value = {"metadata": {"name": "s3.csi.aws.com"}}
    assert client.get_csi_driver("s3.csi.aws.com") == {"metadata": {"name": "s3.csi.aws.com"}}
