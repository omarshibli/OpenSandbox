// Copyright 2026 Alibaba Group Holding Ltd.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package opensandbox

import (
	"encoding/json"
	"testing"
)

func TestVolumeS3Serialization(t *testing.T) {
	vol := Volume{
		Name:      "logs",
		S3:        &S3{Bucket: "my-team-sandbox-logs", Prefix: "sandboxes/task-001/", Region: "eu-west-1", Options: []string{"uid=1000"}},
		MountPath: "/mnt/logs",
		ReadOnly:  true,
	}
	data, err := json.Marshal(vol)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"name":"logs","s3":{"bucket":"my-team-sandbox-logs","prefix":"sandboxes/task-001/","region":"eu-west-1","options":["uid=1000"]},"mountPath":"/mnt/logs","readOnly":true}`
	if string(data) != want {
		t.Fatalf("got %s\nwant %s", data, want)
	}
}

func TestVolumeS3OmitsEmptyOptionalFields(t *testing.T) {
	data, err := json.Marshal(Volume{Name: "logs", S3: &S3{Bucket: "b"}, MountPath: "/mnt/logs"})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"name":"logs","s3":{"bucket":"b"},"mountPath":"/mnt/logs"}`
	if string(data) != want {
		t.Fatalf("got %s\nwant %s", data, want)
	}
}
