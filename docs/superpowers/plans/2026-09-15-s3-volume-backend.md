# S3 Volume Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `s3` volume backend that mounts an S3 bucket prefix into a sandbox on Kubernetes (EKS) with the Mountpoint for Amazon S3 CSI driver and no access keys.

**Architecture:** The API gains a `volumes[].s3` struct (`bucket`, `prefix`, `region`, `options`). On Kubernetes the server creates one static `PersistentVolume` (CSI driver `s3.csi.aws.com`) and one bound `PersistentVolumeClaim` per `s3` volume, then mounts the claim through the existing PVC code path. Both objects carry the existing managed-volume labels; the PVC gets `ownerReferences`, the PV is deleted by the server on cleanup and by a startup sweep. Docker and FastSandbox reject `s3`. Credentials come from one IAM role bound to the CSI driver ServiceAccount (EKS Pod Identity or IRSA), which is operator setup.

**Tech Stack:** Python 3 / FastAPI / Pydantic v2 / `kubernetes` client (server); `uv run pytest` for tests; OpenAPI spec in `specs/sandbox-lifecycle.yml`; SDKs in Python, TypeScript, Go, C#, Kotlin; Helm chart; VitePress docs.

**Spec:** `docs/superpowers/specs/2026-09-15-s3-volume-backend-design.md`

## Global Constraints

- Backend name is `s3`. Field names: `bucket`, `prefix`, `region`, `options`. No credential fields.
- `subPath` on an `s3` volume is rejected with `VOLUME::INVALID_SUB_PATH`.
- Reserved mount options owned by the server: `prefix`, `region`, `read-only`, `allow-delete`, `allow-overwrite`.
- Generated object names: `s3-<sandbox-id>-<volume-name>` for PV, PVC and `volumeHandle`.
- Labels on PV and PVC: `opensandbox.io/volume-managed-by=server`, `opensandbox.io/id=<sandbox-id>`.
- New `[storage]` config keys: `s3_csi_driver` (default `"s3.csi.aws.com"`), `s3_mount_options` (default `[]`), `s3_allowed_buckets` (default `[]`, empty means any bucket).
- New RBAC for the server: `persistentvolumes` (`create`, `delete`, `get`, `list`) and `storage.k8s.io/csidrivers` (`get`).
- Error codes: `VOLUME::INVALID_S3_BUCKET`, `VOLUME::INVALID_S3_PREFIX`, `VOLUME::INVALID_S3_REGION`, `VOLUME::INVALID_S3_OPTION`, plus the existing `VOLUME::INVALID_SUB_PATH`, `VOLUME::UNSUPPORTED_BACKEND`, `KUBERNETES::API_ERROR`, `KUBERNETES::POD_READY_TIMEOUT`.
- All server tests run from `server/` with `uv run pytest <path> -v`. Lint with `uv run ruff check`.
- Public interfaces stay additive. Do not edit generated SDK output as the only fix; regenerate from the spec.
- Follow existing style in each file. Every new Python file starts with the Apache license header copied from `server/opensandbox_server/services/k8s/volume_helper.py`.

---

## File structure

| File | Responsibility |
|---|---|
| `specs/sandbox-lifecycle.yml` | Public contract: `S3` schema, `Volume.s3` |
| `server/opensandbox_server/api/schema.py` | `S3` Pydantic model, `Volume.s3`, exactly-one-backend |
| `server/opensandbox_server/services/constants.py` | New error codes |
| `server/opensandbox_server/services/validators.py` | `ensure_valid_s3_volume`, reserved options, `subPath` rejection |
| `server/opensandbox_server/config.py` | `StorageConfig` S3 keys |
| `server/opensandbox_server/services/docker/volumes.py` | Docker gate: reject `s3` |
| `server/opensandbox_server/services/k8s/client.py` | PV and CSIDriver client methods |
| `server/opensandbox_server/services/k8s/s3_volume.py` (new) | Object builders, `S3VolumeProvisioner`, `FailedMount` extraction |
| `server/opensandbox_server/services/k8s/volume_helper.py` | `s3` branch in pod spec translation |
| `server/opensandbox_server/services/k8s/batchsandbox_provider.py`, `agent_sandbox_provider.py` | Pass `sandbox_id` to the translator |
| `server/opensandbox_server/services/k8s/kubernetes_service.py` | Wire provisioning, cleanup, timeout hint |
| `server/opensandbox_server/main.py` | Startup orphan sweep |
| `kubernetes/charts/opensandbox-server/templates/server.yaml`, `values.yaml` | RBAC and config comment |
| SDK model files (5 languages) | `S3` model and `Volume.s3` |
| `docs/examples/kubernetes-s3-volume-mount.md`, `docs/examples/index.md`, `docs/.vitepress/config.mts`, `docs/architecture/index.md`, `server/configuration.md` | Documentation |
| `oseps/0024-s3-volume-backend.md` (number: next free) | Proposal record |

---

### Task 1: API contract and Pydantic model

**Files:**
- Modify: `specs/sandbox-lifecycle.yml:1969-2008` (Volume), insert `S3` schema after `OSSFS` (ends at line 2133)
- Modify: `server/opensandbox_server/api/schema.py:298-415`
- Test: `server/tests/test_schema.py`

**Interfaces:**
- Produces: `class S3(BaseModel)` with `bucket: str`, `prefix: Optional[str]`, `region: Optional[str]`, `options: Optional[List[str]]`; `Volume.s3: Optional[S3]`. Later tasks import `S3` from `opensandbox_server.api.schema`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_schema.py` inside the module (top-level functions are fine; the file already imports `pytest`, `ValidationError`, and the schema models; add `S3` to the import list from `opensandbox_server.api.schema`):

```python
class TestS3Backend:
    def test_valid_s3_minimal(self):
        backend = S3(bucket="my-team-sandbox-logs")
        assert backend.bucket == "my-team-sandbox-logs"
        assert backend.prefix is None
        assert backend.region is None
        assert backend.options is None

    def test_valid_s3_full(self):
        backend = S3(
            bucket="my-team-sandbox-logs",
            prefix="sandboxes/task-001/",
            region="eu-west-1",
            options=["uid=1000", "gid=1000"],
        )
        assert backend.prefix == "sandboxes/task-001/"
        assert backend.region == "eu-west-1"
        assert backend.options == ["uid=1000", "gid=1000"]

    def test_s3_rejects_unknown_field(self):
        with pytest.raises(ValidationError):
            S3(bucket="my-team-sandbox-logs", accessKeyId="AKIA")  # type: ignore[call-arg]

    def test_s3_bucket_length_bounds(self):
        with pytest.raises(ValidationError):
            S3(bucket="ab")
        with pytest.raises(ValidationError):
            S3(bucket="a" * 64)

    def test_volume_with_s3_backend(self):
        volume = Volume(
            name="logs",
            s3=S3(bucket="my-team-sandbox-logs", prefix="sandboxes/task-001/"),
            mount_path="/mnt/logs",
        )
        assert volume.s3 is not None
        assert volume.s3.bucket == "my-team-sandbox-logs"
        assert volume.host is None and volume.pvc is None and volume.ossfs is None

    def test_volume_s3_plus_pvc_rejected(self):
        with pytest.raises(ValidationError, match="multiple"):
            Volume(
                name="logs",
                s3=S3(bucket="my-team-sandbox-logs"),
                pvc=PVC(claim_name="claim"),
                mount_path="/mnt/logs",
            )

    def test_serialization_s3_volume(self):
        volume = Volume(
            name="logs",
            s3=S3(bucket="my-team-sandbox-logs", region="eu-west-1"),
            mount_path="/mnt/logs",
            read_only=True,
        )
        dumped = volume.model_dump(by_alias=True, exclude_none=True)
        assert dumped == {
            "name": "logs",
            "s3": {"bucket": "my-team-sandbox-logs", "region": "eu-west-1"},
            "mountPath": "/mnt/logs",
            "readOnly": True,
        }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_schema.py -k S3Backend -v`
Expected: FAIL with `ImportError: cannot import name 'S3'`.

- [ ] **Step 3: Add the `S3` model and `Volume.s3`**

In `server/opensandbox_server/api/schema.py`, insert after the `OSSFS` class (after its `validate_inline_credentials` method) and before `class Volume`:

```python
class S3(BaseModel):
    """
    Amazon S3 mount backend.

    Kubernetes runtime only. The server creates a static PersistentVolume for
    the Mountpoint for Amazon S3 CSI driver and a bound PersistentVolumeClaim,
    then mounts the claim into the sandbox. Credentials come from the IAM role
    bound to the CSI driver ServiceAccount; the request carries none.
    """

    bucket: str = Field(
        ...,
        description="S3 bucket name.",
        min_length=3,
        max_length=63,
    )
    prefix: Optional[str] = Field(
        None,
        description=(
            "Optional key prefix inside the bucket to mount. "
            "Relative, no leading '/'. The server appends a trailing '/' if absent."
        ),
        max_length=1024,
    )
    region: Optional[str] = Field(
        None,
        description="Optional AWS region of the bucket, e.g. 'eu-west-1'. Detected by Mountpoint when absent.",
    )
    options: Optional[List[str]] = Field(
        None,
        description=(
            "Additional Mountpoint mount options as raw payloads without leading '-', "
            "e.g. 'uid=1000'. Server-owned options (prefix, region, read-only, "
            "allow-delete, allow-overwrite) are rejected."
        ),
    )

    class Config:
        populate_by_name = True
        extra = "forbid"
```

In `class Volume`, add after the `ossfs` field:

```python
    s3: Optional[S3] = Field(
        None,
        description="Amazon S3 mount backend (Kubernetes runtime only).",
    )
```

Replace the body of `validate_exactly_one_backend`:

```python
        backends = [self.host, self.pvc, self.ossfs, self.s3]
        specified = [b for b in backends if b is not None]
        if len(specified) == 0:
            raise ValueError("Exactly one backend (host, pvc, ossfs, s3) must be specified, but none was provided.")
        if len(specified) > 1:
            raise ValueError("Exactly one backend (host, pvc, ossfs, s3) must be specified, but multiple were provided.")
        return self
```

Add `S3` to the `Volume` docstring list of backends.

- [ ] **Step 4: Add the `S3` schema to the OpenAPI spec**

In `specs/sandbox-lifecycle.yml`, add to `Volume.properties` after `ossfs`:

```yaml
        s3:
          $ref: '#/components/schemas/S3'
```

Update the `Volume.description` bullet to read `Exactly one backend struct (host, pvc, ossfs, s3) with backend-specific fields` and add to the `subPath` description: `Not allowed for the `s3` backend; use `s3.prefix`.`

Insert after the `OSSFS` schema (after its `additionalProperties: false`):

```yaml
    S3:
      type: object
      description: |
        Amazon S3 mount backend. Kubernetes runtime only.

        The server creates a static PersistentVolume for the Mountpoint for Amazon S3
        CSI driver and a bound PersistentVolumeClaim, then mounts the claim into the
        sandbox. Credentials come from the IAM role bound to the CSI driver
        ServiceAccount (EKS Pod Identity or IRSA); the request carries none.

        Mountpoint semantics: new files are written sequentially and appear on close;
        overwrite (truncate) and delete are allowed for read-write mounts; append to an
        existing object, random writes and rename are not supported.
      required: [bucket]
      properties:
        bucket:
          type: string
          description: S3 bucket name (S3 bucket naming rules).
          minLength: 3
          maxLength: 63
        prefix:
          type: string
          description: |
            Optional key prefix inside the bucket to mount. Relative, no leading `/`,
            no `..` segments. The server appends a trailing `/` if absent.
          maxLength: 1024
        region:
          type: string
          description: Optional AWS region of the bucket (e.g., `eu-west-1`). Detected by Mountpoint when absent.
        options:
          type: array
          description: |
            Additional Mountpoint mount options as raw payloads without leading `-`
            (e.g., `uid=1000`). Server-owned options are rejected:
            `prefix`, `region`, `read-only`, `allow-delete`, `allow-overwrite`.
          items:
            type: string
      additionalProperties: false
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_schema.py -v`
Expected: all PASS, including the pre-existing volume tests.

- [ ] **Step 6: Commit**

```bash
git add specs/sandbox-lifecycle.yml server/opensandbox_server/api/schema.py server/tests/test_schema.py
git commit -m "feat(api): add s3 volume backend schema"
```

---

### Task 2: Error codes and request validation

**Files:**
- Modify: `server/opensandbox_server/services/constants.py:137-159`
- Modify: `server/opensandbox_server/services/validators.py` (imports at line 33-35, add functions after `ensure_valid_ossfs_volume` ending near line 574, edit `ensure_volumes_valid` at 676-761)
- Test: `server/tests/test_validators.py`

**Interfaces:**
- Produces: `SandboxErrorCodes.INVALID_S3_BUCKET`, `INVALID_S3_PREFIX`, `INVALID_S3_REGION`, `INVALID_S3_OPTION`; `S3_RESERVED_MOUNT_OPTIONS: frozenset[str]`; `s3_mount_option_name(option: str) -> str`; `ensure_valid_s3_mount_option(option: str) -> None`; `ensure_valid_s3_volume(s3: S3, allowed_buckets: Optional[List[str]] = None) -> None`; `ensure_volumes_valid(volumes, allowed_host_prefixes=None, allowed_s3_buckets=None)`.

- [ ] **Step 1: Write the failing tests**

Add `S3` to the schema import in `server/tests/test_validators.py` and import `ensure_valid_s3_volume`, `ensure_valid_s3_mount_option` from `opensandbox_server.services.validators`. Append:

```python
class TestS3VolumeValidation:
    def _volume(self, **s3_kwargs):
        return Volume(
            name="logs",
            s3=S3(bucket=s3_kwargs.pop("bucket", "my-team-sandbox-logs"), **s3_kwargs),
            mount_path="/mnt/logs",
        )

    def test_valid_s3_volume(self):
        assert ensure_volumes_valid([self._volume(prefix="sandboxes/task-001", region="eu-west-1")]) is None

    @pytest.mark.parametrize("bucket", ["UPPER-case", "has_underscore", "double..dot", "-leading", "trailing-"])
    def test_invalid_bucket_name(self, bucket):
        with pytest.raises(HTTPException) as exc:
            ensure_volumes_valid([self._volume(bucket=bucket)])
        assert exc.value.detail["code"] == SandboxErrorCodes.INVALID_S3_BUCKET

    def test_bucket_not_in_allowlist(self):
        with pytest.raises(HTTPException) as exc:
            ensure_volumes_valid([self._volume()], allowed_s3_buckets=["other-bucket"])
        assert exc.value.detail["code"] == SandboxErrorCodes.INVALID_S3_BUCKET

    def test_bucket_in_allowlist_passes(self):
        assert ensure_volumes_valid([self._volume()], allowed_s3_buckets=["my-team-sandbox-logs"]) is None

    def test_empty_allowlist_allows_any_bucket(self):
        assert ensure_volumes_valid([self._volume()], allowed_s3_buckets=[]) is None

    @pytest.mark.parametrize("prefix", ["/absolute", "a/../b", "has space/", "semi;colon", "dollar$x"])
    def test_invalid_prefix(self, prefix):
        with pytest.raises(HTTPException) as exc:
            ensure_volumes_valid([self._volume(prefix=prefix)])
        assert exc.value.detail["code"] == SandboxErrorCodes.INVALID_S3_PREFIX

    @pytest.mark.parametrize("region", ["EU-WEST-1", "eu-west", "us east 1", "eu-west-1;rm"])
    def test_invalid_region(self, region):
        with pytest.raises(HTTPException) as exc:
            ensure_volumes_valid([self._volume(region=region)])
        assert exc.value.detail["code"] == SandboxErrorCodes.INVALID_S3_REGION

    @pytest.mark.parametrize("region", ["eu-west-1", "us-east-2", "ap-southeast-3", "us-gov-west-1"])
    def test_valid_region(self, region):
        assert ensure_volumes_valid([self._volume(region=region)]) is None

    @pytest.mark.parametrize("option", ["--uid=1000", "-o allow-other", "uid=1000;id", "", "   "])
    def test_invalid_option_payload(self, option):
        with pytest.raises(HTTPException) as exc:
            ensure_volumes_valid([self._volume(options=[option])])
        assert exc.value.detail["code"] == SandboxErrorCodes.INVALID_S3_OPTION

    @pytest.mark.parametrize(
        "option",
        ["prefix other/", "prefix=other/", "region us-east-1", "read-only", "allow-delete", "allow-overwrite"],
    )
    def test_reserved_option_rejected(self, option):
        with pytest.raises(HTTPException) as exc:
            ensure_volumes_valid([self._volume(options=[option])])
        assert exc.value.detail["code"] == SandboxErrorCodes.INVALID_S3_OPTION
        assert "reserved" in exc.value.detail["message"]

    def test_allowed_options_pass(self):
        assert ensure_volumes_valid([self._volume(options=["uid=1000", "gid=1000", "allow-other"])]) is None

    def test_sub_path_rejected_for_s3(self):
        volume = Volume(
            name="logs",
            s3=S3(bucket="my-team-sandbox-logs"),
            mount_path="/mnt/logs",
            sub_path="task-001",
        )
        with pytest.raises(HTTPException) as exc:
            ensure_volumes_valid([volume])
        assert exc.value.detail["code"] == SandboxErrorCodes.INVALID_SUB_PATH
        assert "s3.prefix" in exc.value.detail["message"]

    def test_ensure_valid_s3_mount_option_accepts_plain_flag(self):
        assert ensure_valid_s3_mount_option("allow-other") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_validators.py -k S3Volume -v`
Expected: FAIL with `ImportError` on `ensure_valid_s3_volume`.

- [ ] **Step 3: Add error codes**

In `server/opensandbox_server/services/constants.py`, after `OSSFS_UNMOUNT_FAILED = "VOLUME::OSSFS_UNMOUNT_FAILED"` add:

```python
    INVALID_S3_BUCKET = "VOLUME::INVALID_S3_BUCKET"
    INVALID_S3_PREFIX = "VOLUME::INVALID_S3_PREFIX"
    INVALID_S3_REGION = "VOLUME::INVALID_S3_REGION"
    INVALID_S3_OPTION = "VOLUME::INVALID_S3_OPTION"
```

- [ ] **Step 4: Add the validators**

In `server/opensandbox_server/services/validators.py`, extend the `TYPE_CHECKING` import to include `S3`:

```python
    from opensandbox_server.api.schema import CredentialProxyConfig, NetworkPolicy, OSSFS, PlatformSpec, S3, Volume
```

Add module-level constants near the other regex constants at the top of the file (after the imports):

```python
# S3 bucket naming rules: 3-63 chars, lowercase letters, digits, dots, hyphens,
# starts and ends with a letter or digit, no consecutive dots.
_S3_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_S3_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_S3_SHELL_META_RE = re.compile(r"[;&|`$()<>\n\r\s]")
_S3_PREFIX_MAX_BYTES = 1024
S3_RESERVED_MOUNT_OPTIONS: frozenset[str] = frozenset(
    {"prefix", "region", "read-only", "allow-delete", "allow-overwrite"}
)
```

Add after `ensure_valid_ossfs_volume`:

```python
def s3_mount_option_name(option: str) -> str:
    """Return the option name of a raw Mountpoint option ('uid=1000' -> 'uid', 'prefix a/' -> 'prefix')."""
    return re.split(r"[= ]", option.strip(), maxsplit=1)[0]


def ensure_valid_s3_mount_option(option: str) -> None:
    """
    Validate one raw Mountpoint mount option.

    Rejects empty payloads, leading '-', shell metacharacters and the option
    names the server owns (see ``S3_RESERVED_MOUNT_OPTIONS``).
    """
    if not isinstance(option, str) or not option.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": SandboxErrorCodes.INVALID_S3_OPTION,
                "message": "S3 options must be non-empty strings.",
            },
        )
    normalized = option.strip()
    if normalized.startswith("-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": SandboxErrorCodes.INVALID_S3_OPTION,
                "message": (
                    "S3 options must be raw option payloads without '-' prefix "
                    "(e.g. 'uid=1000', 'allow-other')."
                ),
            },
        )
    if re.search(r"[;&|`$()<>\n\r]", normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": SandboxErrorCodes.INVALID_S3_OPTION,
                "message": f"S3 option '{normalized}' contains forbidden characters.",
            },
        )
    name = s3_mount_option_name(normalized)
    if name in S3_RESERVED_MOUNT_OPTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": SandboxErrorCodes.INVALID_S3_OPTION,
                "message": (
                    f"S3 option '{name}' is reserved and set by the server. "
                    f"Reserved options: {', '.join(sorted(S3_RESERVED_MOUNT_OPTIONS))}."
                ),
            },
        )


def ensure_valid_s3_volume(s3: "S3", allowed_buckets: Optional[List[str]] = None) -> None:
    """
    Validate S3 backend fields.

    Args:
        s3: S3 backend model.
        allowed_buckets: Operator allowlist. Empty or None allows any bucket.

    Raises:
        HTTPException: When any S3 field is invalid.
    """
    bucket = s3.bucket.strip()
    if not _S3_BUCKET_RE.match(bucket) or ".." in bucket:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": SandboxErrorCodes.INVALID_S3_BUCKET,
                "message": (
                    f"S3 bucket '{s3.bucket}' is not a valid bucket name "
                    "(3-63 lowercase letters, digits, dots or hyphens; must start and end with a letter or digit)."
                ),
            },
        )
    if allowed_buckets and bucket not in allowed_buckets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": SandboxErrorCodes.INVALID_S3_BUCKET,
                "message": f"S3 bucket '{bucket}' is not in the server allowlist (storage.s3_allowed_buckets).",
            },
        )

    if s3.prefix is not None and s3.prefix != "":
        prefix = s3.prefix
        if prefix.startswith("/"):
            reason = "must be relative (no leading '/')"
        elif any(part == ".." for part in prefix.split("/")):
            reason = "must not contain '..' segments"
        elif _S3_SHELL_META_RE.search(prefix):
            reason = "contains forbidden characters or whitespace"
        elif len(prefix.encode("utf-8")) > _S3_PREFIX_MAX_BYTES:
            reason = f"exceeds {_S3_PREFIX_MAX_BYTES} bytes"
        else:
            reason = None
        if reason is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": SandboxErrorCodes.INVALID_S3_PREFIX,
                    "message": f"S3 prefix '{prefix}' {reason}.",
                },
            )

    if s3.region is not None and not _S3_REGION_RE.match(s3.region):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": SandboxErrorCodes.INVALID_S3_REGION,
                "message": f"S3 region '{s3.region}' is not a valid AWS region (e.g. 'eu-west-1').",
            },
        )

    if s3.options is not None:
        for option in s3.options:
            ensure_valid_s3_mount_option(option)
```

Edit `ensure_volumes_valid`:

Signature:

```python
def ensure_volumes_valid(
    volumes: Optional[List["Volume"]],
    allowed_host_prefixes: Optional[List[str]] = None,
    allowed_s3_buckets: Optional[List[str]] = None,
) -> None:
```

Add `allowed_s3_buckets` to the docstring Args. Replace the `ensure_valid_sub_path(volume.sub_path)` call with:

```python
        # Validate subPath (s3 uses s3.prefix instead)
        if volume.s3 is not None and volume.sub_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": SandboxErrorCodes.INVALID_SUB_PATH,
                    "message": (
                        f"Volume '{volume.name}': subPath is not supported for the s3 backend. "
                        "Use s3.prefix to select a key prefix."
                    ),
                },
            )
        ensure_valid_sub_path(volume.sub_path)
```

Add `volume.s3 is not None,` to the `backends_specified` sum, change both `(host, pvc, ossfs)` messages to `(host, pvc, ossfs, s3)`, and add after the ossfs dispatch:

```python
        if volume.s3 is not None:
            ensure_valid_s3_volume(volume.s3, allowed_s3_buckets)
```

Add `"ensure_valid_s3_volume"`, `"ensure_valid_s3_mount_option"`, `"s3_mount_option_name"`, `"S3_RESERVED_MOUNT_OPTIONS"` to `__all__`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_validators.py tests/test_schema.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add server/opensandbox_server/services/constants.py server/opensandbox_server/services/validators.py server/tests/test_validators.py
git commit -m "feat(server): validate s3 volume backend fields"
```

---

### Task 3: Storage config keys

**Files:**
- Modify: `server/opensandbox_server/config.py:798-824` (`StorageConfig`)
- Modify: `server/configuration.md:284-294`
- Test: `server/tests/test_config.py` (create the class below if the file has no storage tests; the file exists for other config sections)

**Interfaces:**
- Produces: `StorageConfig.s3_csi_driver: str`, `StorageConfig.s3_mount_options: list[str]`, `StorageConfig.s3_allowed_buckets: list[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_config.py` (add `import pytest` and `from opensandbox_server.config import StorageConfig` if missing):

```python
class TestStorageConfigS3:
    def test_defaults(self):
        cfg = StorageConfig()
        assert cfg.s3_csi_driver == "s3.csi.aws.com"
        assert cfg.s3_mount_options == []
        assert cfg.s3_allowed_buckets == []

    def test_operator_mount_options_reject_reserved(self):
        with pytest.raises(ValueError, match="reserved"):
            StorageConfig(s3_mount_options=["prefix foo/"])

    def test_operator_mount_options_reject_dash_prefix(self):
        with pytest.raises(ValueError, match="'-' prefix"):
            StorageConfig(s3_mount_options=["--uid=1000"])

    def test_operator_mount_options_accept_plain(self):
        cfg = StorageConfig(s3_mount_options=["uid=1000", "gid=1000"])
        assert cfg.s3_mount_options == ["uid=1000", "gid=1000"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_config.py -k StorageConfigS3 -v`
Expected: FAIL with `AttributeError: 's3_csi_driver'`.

- [ ] **Step 3: Add the fields**

In `server/opensandbox_server/config.py`, `class StorageConfig`, add after `ossfs_mount_root`:

```python
    s3_csi_driver: str = Field(
        default="s3.csi.aws.com",
        description=(
            "Name of the CSIDriver object used for s3 volumes (Mountpoint for Amazon S3 CSI driver). "
            "The server checks that it exists before creating s3 volumes."
        ),
    )
    s3_mount_options: list[str] = Field(
        default_factory=list,
        description=(
            "Operator-provided Mountpoint mount options added to every s3 volume "
            "(e.g. 'uid=1000'). Raw payloads without leading '-'. Server-owned options "
            "(prefix, region, read-only, allow-delete, allow-overwrite) are rejected."
        ),
    )
    s3_allowed_buckets: list[str] = Field(
        default_factory=list,
        description=(
            "Allowlist of S3 bucket names permitted for s3 volumes. "
            "If empty, any bucket is allowed; the IAM role bound to the CSI driver is the boundary."
        ),
    )

    @field_validator("s3_mount_options")
    @classmethod
    def _validate_s3_mount_options(cls, options: list[str]) -> list[str]:
        reserved = {"prefix", "region", "read-only", "allow-delete", "allow-overwrite"}
        for option in options:
            normalized = option.strip()
            if not normalized:
                raise ValueError("storage.s3_mount_options entries must be non-empty")
            if normalized.startswith("-"):
                raise ValueError(
                    f"storage.s3_mount_options entry '{option}' must not have a '-' prefix"
                )
            name = normalized.split("=", 1)[0].split(" ", 1)[0]
            if name in reserved:
                raise ValueError(
                    f"storage.s3_mount_options entry '{option}' uses reserved option '{name}'"
                )
        return options
```

Make sure `field_validator` is imported from `pydantic` at the top of `config.py` (check the existing import line and add it if absent).

- [ ] **Step 4: Document the keys**

In `server/configuration.md`, in the `[storage]` table, add rows:

```markdown
| `s3_csi_driver` | string | `"s3.csi.aws.com"` | Name of the `CSIDriver` object used for **s3** volumes (Mountpoint for Amazon S3 CSI driver). Checked before the server creates s3 volumes. |
| `s3_mount_options` | list of strings | `[]` | Operator Mountpoint options added to every s3 volume (e.g. `uid=1000`). Raw payloads without leading `-`; server-owned options are rejected. |
| `s3_allowed_buckets` | list of strings | `[]` | Allowlist of S3 buckets for s3 volumes. Empty means any bucket; the IAM role is the boundary. |
```

Change the sentence below the table to: ``Sandbox **volume** models (`host`, `pvc`, `ossfs`, `s3`) in API requests are documented in the OpenAPI specs and OSEPs; this table only covers **server** storage settings.`` Update the section intro at line 286 to mention S3 mounts.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/opensandbox_server/config.py server/configuration.md server/tests/test_config.py
git commit -m "feat(server): add s3 storage config keys"
```

---

### Task 4: Docker runtime rejects `s3`

**Files:**
- Modify: `server/opensandbox_server/services/docker/volumes.py:78-95`
- Test: `server/tests/test_docker_service.py`

- [ ] **Step 1: Write the failing test**

Add `S3` to the schema imports at the top of `server/tests/test_docker_service.py`. Add inside the same test class that contains `test_host_path_not_in_allowlist_rejected` (around line 3926):

```python
    @pytest.mark.asyncio
    async def test_s3_volume_unsupported_on_docker(self, mock_docker):
        """The s3 backend is Kubernetes-only; Docker must reject it before any side effect."""
        mock_client = MagicMock()
        mock_client.containers.list.return_value = []
        mock_docker.from_env.return_value = mock_client

        service = DockerSandboxService(config=_app_config())

        request = CreateSandboxRequest(
            image=ImageSpec(uri="python:3.11"),
            timeout=120,
            resourceLimits=ResourceLimits(root={}),
            env={},
            metadata={},
            entrypoint=["python"],
            volumes=[
                Volume(
                    name="logs",
                    s3=S3(bucket="my-team-sandbox-logs"),
                    mount_path="/mnt/logs",
                )
            ],
        )

        with pytest.raises(HTTPException) as exc_info:
            await service.create_sandbox(request)

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert exc_info.value.detail["code"] == SandboxErrorCodes.UNSUPPORTED_VOLUME_BACKEND
        mock_client.api.create_container.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_docker_service.py -k s3_volume_unsupported -v`
Expected: FAIL. The request passes shared validation and the runtime does not raise the expected code.

- [ ] **Step 3: Add the gate**

In `server/opensandbox_server/services/docker/volumes.py`, in `_validate_volumes`, change the shared validation call to pass the allowlist:

```python
        ensure_volumes_valid(
            request.volumes,
            allowed_host_prefixes=allowed_prefixes,
            allowed_s3_buckets=self.app_config.storage.s3_allowed_buckets,
        )
```

Add a branch in the per-volume loop after the `ossfs` branch:

```python
                elif volume.s3 is not None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "code": SandboxErrorCodes.UNSUPPORTED_VOLUME_BACKEND,
                            "message": (
                                f"Volume '{volume.name}': the s3 backend is supported only on the "
                                "Kubernetes runtime."
                            ),
                        },
                    )
```

Confirm `HTTPException`, `status` and `SandboxErrorCodes` are already imported in this file (they are used by the host and pvc validators).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_docker_service.py -k "volume" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/opensandbox_server/services/docker/volumes.py server/tests/test_docker_service.py
git commit -m "feat(docker): reject s3 volume backend on the Docker runtime"
```

---

### Task 5: Kubernetes client methods for PVs and CSIDriver

**Files:**
- Modify: `server/opensandbox_server/services/k8s/client.py` (import at line 26; add methods after `patch_pvc`, before the Secret operations block near line 440)
- Test: `server/tests/k8s/test_client_pv.py` (new)

**Interfaces:**
- Produces on `K8sClient`: `create_pv(body: dict) -> Any`, `get_pv(name: str) -> Optional[Any]` (None on 404), `list_pvs(label_selector: str = "") -> List[Any]`, `delete_pv(name: str) -> None` (404 swallowed), `get_csi_driver(name: str) -> Optional[Any]` (None on 404), `get_storage_v1_api() -> StorageV1Api`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/k8s/test_client_pv.py`:

```python
# (Apache license header, copied from volume_helper.py)

from unittest.mock import MagicMock

import pytest
from kubernetes.client import ApiException

from opensandbox_server.services.k8s.client import K8sClient


@pytest.fixture
def client(k8s_runtime_config):
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
```

The `k8s_runtime_config` fixture already exists in `server/tests/k8s/fixtures/k8s_fixtures.py` and is loaded by `server/tests/k8s/conftest.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/k8s/test_client_pv.py -v`
Expected: FAIL with `AttributeError: 'K8sClient' object has no attribute 'create_pv'`.

- [ ] **Step 3: Add the client methods**

In `server/opensandbox_server/services/k8s/client.py`:

Extend the import on line 26 to include `StorageV1Api`:

```python
from kubernetes.client import ApiException, CoreV1Api, CustomObjectsApi, NodeV1Api, StorageV1Api, V1APIResourceList
```

In `__init__`, next to `self._core_v1_api: Optional[CoreV1Api] = None`, add:

```python
        self._storage_v1_api: Optional[StorageV1Api] = None
```

After `get_core_v1_api`, add:

```python
    def get_storage_v1_api(self) -> StorageV1Api:
        if self._storage_v1_api is None:
            self._storage_v1_api = client.StorageV1Api()
        return self._storage_v1_api
```

After `patch_pvc`, add:

```python
    # ------------------------------------------------------------------
    # PersistentVolume and CSIDriver operations (cluster-scoped)
    # ------------------------------------------------------------------

    def create_pv(self, body: Any) -> Any:
        """Create a PersistentVolume."""
        if self._write_limiter:
            self._write_limiter.acquire()
        return self.get_core_v1_api().create_persistent_volume(body=body)

    def get_pv(self, name: str) -> Optional[Any]:
        """Read a PersistentVolume by name. Returns None on 404."""
        if self._read_limiter:
            self._read_limiter.acquire()
        try:
            return self.get_core_v1_api().read_persistent_volume(name=name)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_pvs(self, label_selector: str = "") -> List[Any]:
        """List PersistentVolumes, returning the items list."""
        if self._read_limiter:
            self._read_limiter.acquire()
        result = self.get_core_v1_api().list_persistent_volume(label_selector=label_selector)
        return list(getattr(result, "items", []) or [])

    def delete_pv(self, name: str) -> None:
        """Delete a PersistentVolume by name. 404 is swallowed."""
        if self._write_limiter:
            self._write_limiter.acquire()
        try:
            self.get_core_v1_api().delete_persistent_volume(name=name)
        except ApiException as e:
            if e.status == 404:
                return
            raise

    def get_csi_driver(self, name: str) -> Optional[Any]:
        """Read a storage.k8s.io/v1 CSIDriver by name. Returns None on 404."""
        if self._read_limiter:
            self._read_limiter.acquire()
        try:
            return self.get_storage_v1_api().read_csi_driver(name=name)
        except ApiException as e:
            if e.status == 404:
                return None
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/k8s/test_client_pv.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/opensandbox_server/services/k8s/client.py server/tests/k8s/test_client_pv.py
git commit -m "feat(k8s): add PersistentVolume and CSIDriver client methods"
```

---

### Task 6: S3 object builders

**Files:**
- Create: `server/opensandbox_server/services/k8s/s3_volume.py`
- Test: `server/tests/k8s/test_s3_volume.py` (new)

**Interfaces:**
- Produces (all in `s3_volume.py`):
  - `s3_object_name(sandbox_id: str, volume_name: str) -> str`
  - `normalize_s3_prefix(prefix: Optional[str]) -> Optional[str]`
  - `build_s3_mount_options(volume: Volume, operator_options: list[str]) -> list[str]`
  - `build_s3_pv_body(volume: Volume, sandbox_id: str, namespace: str, storage: StorageConfig) -> dict`
  - `build_s3_pvc_body(volume: Volume, sandbox_id: str, namespace: str, storage: StorageConfig) -> dict`
  - `extract_failed_mount_message(events_text: str) -> Optional[str]`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/k8s/test_s3_volume.py`:

```python
# (Apache license header, copied from volume_helper.py)

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


def _volume(**kwargs) -> Volume:
    s3_kwargs = {"bucket": "my-team-sandbox-logs"}
    for key in ("prefix", "region", "options"):
        if key in kwargs:
            s3_kwargs[key] = kwargs.pop(key)
    return Volume(name="logs", s3=S3(**s3_kwargs), mount_path="/mnt/logs", **kwargs)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/k8s/test_s3_volume.py -v`
Expected: FAIL with `ModuleNotFoundError: opensandbox_server.services.k8s.s3_volume`.

- [ ] **Step 3: Create the module with the builders**

Create `server/opensandbox_server/services/k8s/s3_volume.py`:

```python
# (Apache license header, copied from volume_helper.py)

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
from typing import Any, List, Optional

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/k8s/test_s3_volume.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/opensandbox_server/services/k8s/s3_volume.py server/tests/k8s/test_s3_volume.py
git commit -m "feat(k8s): add s3 volume PV/PVC builders"
```

---

### Task 7: Pod spec translation for `s3`

**Files:**
- Modify: `server/opensandbox_server/services/k8s/volume_helper.py:40-127`
- Modify: `server/opensandbox_server/services/k8s/batchsandbox_provider.py:274`
- Modify: `server/opensandbox_server/services/k8s/agent_sandbox_provider.py:164`
- Test: `server/tests/k8s/test_batchsandbox_provider.py`

**Interfaces:**
- Consumes: `s3_object_name` from Task 6.
- Produces: `apply_volumes_to_pod_spec(pod_spec, volumes, sandbox_id: Optional[str] = None)`. Raises `ValueError` when an `s3` volume is present and `sandbox_id` is None.

- [ ] **Step 1: Write the failing tests**

Add `S3` to the schema imports at the top of `server/tests/k8s/test_batchsandbox_provider.py`. Add to the class that contains `test_apply_volumes_to_pod_spec_empty_volumes`:

```python
    def test_apply_volumes_to_pod_spec_s3_volume(self, mock_k8s_client):
        pod_spec = {"containers": [{"name": "main", "volumeMounts": []}], "volumes": []}
        volumes = [Volume(name="logs", s3=S3(bucket="my-team-sandbox-logs", prefix="p/"), mount_path="/mnt/logs")]

        apply_volumes_to_pod_spec(pod_spec, volumes, sandbox_id="abc123")

        assert pod_spec["volumes"] == [
            {"name": "logs", "persistentVolumeClaim": {"claimName": "s3-abc123-logs", "readOnly": False}}
        ]
        assert pod_spec["containers"][0]["volumeMounts"] == [
            {"name": "logs", "mountPath": "/mnt/logs", "readOnly": False}
        ]

    def test_apply_volumes_to_pod_spec_s3_read_only(self, mock_k8s_client):
        pod_spec = {"containers": [{"name": "main", "volumeMounts": []}], "volumes": []}
        volumes = [Volume(name="data", s3=S3(bucket="datasets"), mount_path="/mnt/data", read_only=True)]

        apply_volumes_to_pod_spec(pod_spec, volumes, sandbox_id="abc123")

        assert pod_spec["volumes"][0]["persistentVolumeClaim"] == {"claimName": "s3-abc123-data", "readOnly": True}
        assert pod_spec["containers"][0]["volumeMounts"][0]["readOnly"] is True
        assert "subPath" not in pod_spec["containers"][0]["volumeMounts"][0]

    def test_apply_volumes_to_pod_spec_two_s3_volumes(self, mock_k8s_client):
        pod_spec = {"containers": [{"name": "main", "volumeMounts": []}], "volumes": []}
        volumes = [
            Volume(name="logs", s3=S3(bucket="b1"), mount_path="/mnt/logs"),
            Volume(name="data", s3=S3(bucket="b2"), mount_path="/mnt/data"),
        ]

        apply_volumes_to_pod_spec(pod_spec, volumes, sandbox_id="abc123")

        assert [v["persistentVolumeClaim"]["claimName"] for v in pod_spec["volumes"]] == [
            "s3-abc123-logs",
            "s3-abc123-data",
        ]

    def test_apply_volumes_to_pod_spec_s3_requires_sandbox_id(self, mock_k8s_client):
        pod_spec = {"containers": [{"name": "main", "volumeMounts": []}], "volumes": []}
        volumes = [Volume(name="logs", s3=S3(bucket="b1"), mount_path="/mnt/logs")]

        with pytest.raises(ValueError, match="sandbox_id"):
            apply_volumes_to_pod_spec(pod_spec, volumes)

    def test_create_workload_with_s3_volume_mounts_generated_claim(self, mock_k8s_client):
        provider = BatchSandboxProvider(mock_k8s_client)
        mock_k8s_client.create_custom_object.return_value = {"metadata": {"name": "test-id", "uid": "test-uid"}}
        expires_at = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)

        provider.create_workload(
            sandbox_id="test-id",
            namespace="test-ns",
            image_spec=ImageSpec(uri="python:3.11"),
            entrypoint=["/bin/bash"],
            env={},
            resource_limits={},
            labels={},
            expires_at=expires_at,
            execd_image="execd:latest",
            volumes=[Volume(name="logs", s3=S3(bucket="b1"), mount_path="/mnt/logs")],
        )

        body = mock_k8s_client.create_custom_object.call_args.kwargs.get("body") or mock_k8s_client.create_custom_object.call_args.args[-1]
        pod_spec = body["spec"]["template"]["spec"]
        claims = [v["persistentVolumeClaim"]["claimName"] for v in pod_spec["volumes"] if "persistentVolumeClaim" in v]
        assert "s3-test-id-logs" in claims
```

Look at `test_create_workload_with_pvc_volume` (line 2919) for how the existing test reads the created body; match that access pattern if it differs from the line above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/k8s/test_batchsandbox_provider.py -k s3 -v`
Expected: FAIL. `apply_volumes_to_pod_spec` rejects `s3` with "Supported backends: pvc, host" and does not accept `sandbox_id`.

- [ ] **Step 3: Add the `s3` branch**

In `server/opensandbox_server/services/k8s/volume_helper.py`:

Add import:

```python
from typing import Any, Dict, List, Optional

from opensandbox_server.api.schema import Volume
from opensandbox_server.services.k8s.s3_volume import s3_object_name
```

Change the signature:

```python
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
```

Insert a new branch before the final `else:` that raises:

```python
        elif vol.s3 is not None:
            if not sandbox_id:
                raise ValueError(
                    f"Volume '{vol_name}' uses the s3 backend, which requires sandbox_id to derive the claim name."
                )
            claim_name = s3_object_name(sandbox_id, vol_name)
            pod_volumes.append({
                "name": vol_name,
                "persistentVolumeClaim": {
                    "claimName": claim_name,
                    "readOnly": vol.read_only,
                },
            })
            mounts.append({
                "name": vol_name,
                "mountPath": vol.mount_path,
                "readOnly": vol.read_only,
            })
            existing_volume_names.add(vol_name)
            logger.info(
                "Added s3 volume '%s' (bucket: %s, claim: %s, read_only=%s) mounted at '%s' for sandbox",
                vol_name,
                vol.s3.bucket,
                claim_name,
                vol.read_only,
                vol.mount_path,
            )
```

Change the final error message to `"Supported backends: pvc, host, s3"`.

In `batchsandbox_provider.py` line 274 and `agent_sandbox_provider.py` line 164, change the call to:

```python
            apply_volumes_to_pod_spec(pod_spec, volumes, sandbox_id=sandbox_id)
```

Confirm the enclosing method has `sandbox_id` in scope (the `create_workload` signature takes `sandbox_id`). If the local name differs, use the local name.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/k8s/test_batchsandbox_provider.py -v`
Expected: PASS, including all pre-existing volume tests.

- [ ] **Step 5: Commit**

```bash
git add server/opensandbox_server/services/k8s/volume_helper.py server/opensandbox_server/services/k8s/batchsandbox_provider.py server/opensandbox_server/services/k8s/agent_sandbox_provider.py server/tests/k8s/test_batchsandbox_provider.py
git commit -m "feat(k8s): mount s3 volumes through generated PVC in pod spec"
```

---

### Task 8: `S3VolumeProvisioner`

**Files:**
- Modify: `server/opensandbox_server/services/k8s/s3_volume.py`
- Test: `server/tests/k8s/test_s3_volume.py`

**Interfaces:**
- Consumes: `K8sClient.create_pv`, `get_pv`, `list_pvs`, `delete_pv`, `get_csi_driver`, `create_pvc`, `get_pvc` (Task 5 and existing).
- Produces: `class S3VolumeProvisioner`:
  - `__init__(self, k8s_client, storage: StorageConfig)`
  - `ensure_driver_installed(self) -> None` raises `HTTPException` 400 `UNSUPPORTED_VOLUME_BACKEND` when missing, 500 `K8S_API_ERROR` on 403.
  - `ensure(self, volumes: List[Volume], sandbox_id: str, namespace: str) -> List[str]` returns claim names of all `s3` volumes.
  - `cleanup(self, sandbox_id: str) -> None` deletes labeled PVs (PVCs are swept by the existing label sweep).
  - `sweep_orphans(self) -> int` deletes server-managed PVs whose bound PVC is gone; returns the number of PVs deleted.
  - `has_s3_volumes(volumes: Optional[List[Volume]]) -> bool` module function.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/k8s/test_s3_volume.py` (add imports: `from unittest.mock import MagicMock`, `import pytest`, `from fastapi import HTTPException`, `from kubernetes.client import ApiException`, `from opensandbox_server.services.constants import SandboxErrorCodes`, and `S3VolumeProvisioner, has_s3_volumes` from the module):

```python
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
            Volume(name="logs", s3=S3(bucket="b1"), mount_path="/mnt/logs"),
            Volume(name="data", s3=S3(bucket="b2"), mount_path="/mnt/data"),
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/k8s/test_s3_volume.py -v`
Expected: FAIL with `ImportError` on `S3VolumeProvisioner`.

- [ ] **Step 3: Implement the provisioner**

Append to `server/opensandbox_server/services/k8s/s3_volume.py` (add `from fastapi import HTTPException, status`, `from kubernetes.client import ApiException`, and `SandboxErrorCodes` to the imports):

```python
def has_s3_volumes(volumes: Optional[List[Volume]]) -> bool:
    return any(v.s3 is not None for v in (volumes or []))


class S3VolumeProvisioner:
    """
    Creates, reuses and removes the PV/PVC pair behind each ``s3`` volume.

    PVCs carry the same managed labels as server-created PVCs, so the
    existing label sweep and ``ownerReferences`` GC remove them. PVs are
    cluster-scoped and cannot be owned by a namespaced CR, so ``cleanup``
    deletes them explicitly and ``sweep_orphans`` catches leftovers at
    startup.
    """

    def __init__(self, k8s_client: Any, storage: StorageConfig):
        self.k8s_client = k8s_client
        self.storage = storage
        self._driver_verified = False

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
                if self._create_or_reuse_pvc(build_s3_pvc_body(volume, sandbox_id, namespace, self.storage), sandbox_id, namespace):
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
        labels = getattr(getattr(obj, "metadata", None), "labels", None) or {}
        if obj is None or labels.get(SANDBOX_ID_LABEL) != sandbox_id or labels.get(SANDBOX_MANAGED_VOLUMES_LABEL) != "server":
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
        """Delete the PVs labeled for this sandbox. Best effort; never raises."""
        selector = f"{SANDBOX_MANAGED_VOLUMES_LABEL}=server,{SANDBOX_ID_LABEL}={sandbox_id}"
        try:
            pvs = self.k8s_client.list_pvs(label_selector=selector)
        except Exception as e:
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
        example, removed by ownerReference GC while the server was down).
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
            try:
                if self.k8s_client.get_pvc(claim_ns, claim_name) is not None:
                    continue
                self.k8s_client.delete_pv(name)
                deleted += 1
                logger.info(f"s3 orphan sweep: deleted PV '{name}' (PVC {claim_ns}/{claim_name} is gone)")
            except Exception as e:
                logger.warning(f"s3 orphan sweep: failed for PV '{name}': {e}")
        return deleted
```

Note: `_assert_owned_by` accepts the `existing` object returned by `get_pv`/`get_pvc`; the test double sets `metadata.labels` as a dict.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/k8s/test_s3_volume.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

Run: `cd server && uv run ruff check opensandbox_server/services/k8s/s3_volume.py`
Expected: no errors.

```bash
git add server/opensandbox_server/services/k8s/s3_volume.py server/tests/k8s/test_s3_volume.py
git commit -m "feat(k8s): provision and clean up s3 volume PV/PVC pairs"
```

---

### Task 9: Wire the provisioner into the Kubernetes service and startup

**Files:**
- Modify: `server/opensandbox_server/services/k8s/kubernetes_service.py` (imports ~line 99; `__init__` ~line 133-160; `create_sandbox` lines 896-931 and 1069-1072; `_cleanup_managed_pvcs` ending ~line 1290)
- Modify: `server/opensandbox_server/main.py:160-182`
- Test: `server/tests/k8s/test_kubernetes_service.py`

**Interfaces:**
- Consumes: `S3VolumeProvisioner`, `has_s3_volumes`, `extract_failed_mount_message` (Task 8, Task 6).
- Produces: `KubernetesSandboxService._s3_volumes: S3VolumeProvisioner`; `KubernetesSandboxService._s3_failed_mount_hint(sandbox_id) -> Optional[str]`.

- [ ] **Step 1: Write the failing tests**

Add `S3` to the schema imports in `server/tests/k8s/test_kubernetes_service.py`. Append:

```python
class TestS3VolumeWiring:
    def _s3_volume(self):
        return Volume(name="logs", s3=S3(bucket="my-team-sandbox-logs"), mount_path="/mnt/logs")

    def test_cleanup_managed_pvcs_also_cleans_s3_pvs(self, k8s_service):
        k8s_service.k8s_client.list_pvcs.return_value = []
        k8s_service._s3_volumes = MagicMock()

        k8s_service._cleanup_managed_pvcs("sandbox-xyz")

        k8s_service._s3_volumes.cleanup.assert_called_once_with("sandbox-xyz")

    def test_failed_mount_hint_uses_pod_events(self, k8s_service):
        k8s_service.get_sandbox_events = MagicMock(
            return_value='[t] Warning  FailedMount  MountVolume.SetUp failed for volume "logs": access denied'
        )
        assert k8s_service._s3_failed_mount_hint("sandbox-xyz") == 'MountVolume.SetUp failed for volume "logs": access denied'

    def test_failed_mount_hint_swallows_errors(self, k8s_service):
        k8s_service.get_sandbox_events = MagicMock(side_effect=RuntimeError("no pod"))
        assert k8s_service._s3_failed_mount_hint("sandbox-xyz") is None

    @pytest.mark.asyncio
    async def test_create_checks_driver_and_provisions_before_workload(self, k8s_service):
        k8s_service._s3_volumes = MagicMock()
        k8s_service._s3_volumes.ensure.return_value = ["s3-id-logs"]
        k8s_service._attach_pvc_owner_references = MagicMock()
        k8s_service.workload_provider.create_workload.side_effect = ValueError("stop here")

        request = CreateSandboxRequest(
            image=ImageSpec(uri="python:3.11"),
            timeout=60,
            resourceLimits=ResourceLimits(root={}),
            entrypoint=["python"],
            volumes=[self._s3_volume()],
        )
        with pytest.raises(HTTPException):
            await k8s_service.create_sandbox(request)

        k8s_service._s3_volumes.ensure_driver_installed.assert_called_once()
        k8s_service._s3_volumes.ensure.assert_called_once()
        args = k8s_service._s3_volumes.ensure.call_args.args
        assert args[0] == request.volumes
        assert isinstance(args[1], str) and args[2] == k8s_service._resolve_namespace()

    @pytest.mark.asyncio
    async def test_create_without_s3_does_not_touch_driver(self, k8s_service):
        k8s_service._s3_volumes = MagicMock()
        k8s_service.workload_provider.create_workload.side_effect = ValueError("stop here")

        request = CreateSandboxRequest(
            image=ImageSpec(uri="python:3.11"),
            timeout=60,
            resourceLimits=ResourceLimits(root={}),
            entrypoint=["python"],
        )
        with pytest.raises(HTTPException):
            await k8s_service.create_sandbox(request)

        k8s_service._s3_volumes.ensure_driver_installed.assert_not_called()
        k8s_service._s3_volumes.ensure.assert_not_called()
```

Check the existing imports at the top of the test file for `CreateSandboxRequest`, `ImageSpec`, `ResourceLimits`, `Volume`; add any that are missing from `opensandbox_server.api.schema`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/k8s/test_kubernetes_service.py -k S3VolumeWiring -v`
Expected: FAIL with `AttributeError` on `_s3_failed_mount_hint` and no call to `_s3_volumes`.

- [ ] **Step 3: Wire the service**

In `server/opensandbox_server/services/k8s/kubernetes_service.py`:

Add import after the `volume_helper`/`client` imports:

```python
from opensandbox_server.services.k8s.s3_volume import (
    S3VolumeProvisioner,
    extract_failed_mount_message,
    has_s3_volumes,
)
```

In `__init__`, directly after `self.k8s_client = K8sClient(self.app_config.kubernetes)`:

```python
            self._s3_volumes = S3VolumeProvisioner(self.k8s_client, self.app_config.storage)
```

In `create_sandbox`, change the shared validation call:

```python
            ensure_volumes_valid(
                request.volumes,
                self.app_config.storage.allowed_host_paths,
                allowed_s3_buckets=self.app_config.storage.s3_allowed_buckets,
            )
```

Immediately after that call (before the poolRef check), add:

```python
            # Fail fast with a clear error when the cluster has no S3 CSI
            # driver — before any side effect.
            if has_s3_volumes(request.volumes):
                await asyncio.to_thread(self._s3_volumes.ensure_driver_installed)
```

After the `_ensure_pvc_volumes` block:

```python
            if has_s3_volumes(request.volumes):
                managed_pvcs_may_exist = True
                s3_claims = await asyncio.to_thread(
                    self._s3_volumes.ensure, request.volumes, sandbox_id, self._resolve_namespace()
                )
                created_managed_pvcs.extend(s3_claims)
```

In the `except HTTPException as e:` block that follows `_wait_for_sandbox_ready` (line ~1069, the one that performs rollback), add as the first statement:

```python
                detail = e.detail if isinstance(e.detail, dict) else None
                if (
                    detail is not None
                    and detail.get("code") == SandboxErrorCodes.K8S_POD_READY_TIMEOUT
                    and has_s3_volumes(request.volumes)
                ):
                    hint = await asyncio.to_thread(self._s3_failed_mount_hint, sandbox_id)
                    if hint:
                        detail["message"] = f"{detail['message']} Last FailedMount event: {hint}"
```

Add a method next to `_cleanup_managed_pvcs`:

```python
    def _s3_failed_mount_hint(self, sandbox_id: str) -> Optional[str]:
        """Last FailedMount event message for the sandbox pod, or None. Never raises."""
        try:
            return extract_failed_mount_message(self.get_sandbox_events(sandbox_id))
        except Exception as e:
            logger.debug(f"sandbox={sandbox_id} | could not read pod events for FailedMount hint: {e}")
            return None
```

At the very end of `_cleanup_managed_pvcs` (after the PVC loop, at the same indentation as the `for pvc in pvcs:` loop), add:

```python
        # PVs are cluster-scoped and have no ownerReference; delete them here.
        self._s3_volumes.cleanup(sandbox_id)
```

Careful: `_cleanup_managed_pvcs` has early `return` statements in its list-failure branches. Move the S3 cleanup call into a `try/finally` around the PVC list-and-delete body so that it runs even when the PVC list fails:

```python
        try:
            ... existing list + delete loop ...
        finally:
            self._s3_volumes.cleanup(sandbox_id)
```

- [ ] **Step 4: Add the startup sweep**

In `server/opensandbox_server/main.py`, inside `lifespan`, after the `await validate_secure_runtime_on_startup(...)` call and still inside the `try:` block, add:

```python
        if k8s_client is not None:
            from opensandbox_server.services.k8s.s3_volume import S3VolumeProvisioner

            try:
                deleted = await asyncio.to_thread(
                    S3VolumeProvisioner(k8s_client, app_config.storage).sweep_orphans
                )
                if deleted:
                    logger.info("Startup sweep removed %d orphaned s3 PersistentVolumes", deleted)
            except Exception as sweep_exc:
                logger.warning("Startup sweep of s3 PersistentVolumes failed: %s", sweep_exc)
```

Add `import asyncio` at the top of `main.py` if it is not already imported.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/k8s -v`
Expected: PASS, including pre-existing `TestEnsurePvcVolumes` and cleanup tests.

Run: `cd server && uv run ruff check && uv run pyright opensandbox_server/services/k8s/s3_volume.py opensandbox_server/services/k8s/kubernetes_service.py`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add server/opensandbox_server/services/k8s/kubernetes_service.py server/opensandbox_server/main.py server/tests/k8s/test_kubernetes_service.py
git commit -m "feat(k8s): wire s3 volume provisioning, cleanup and startup sweep"
```

---

### Task 10: Helm RBAC and config comment

**Files:**
- Modify: `kubernetes/charts/opensandbox-server/templates/server.yaml:32-37`
- Modify: `kubernetes/charts/opensandbox-server/values.yaml:150-175` (`configToml`)

- [ ] **Step 1: Add RBAC rules**

In `server.yaml`, after the `persistentvolumeclaims` rule, add:

```yaml
  # s3 volume backend: static PVs for the Mountpoint S3 CSI driver, and a
  # one-time CSIDriver presence check.
  - apiGroups: [""]
    resources: ["persistentvolumes"]
    verbs: ["create", "delete", "get", "list"]
  - apiGroups: ["storage.k8s.io"]
    resources: ["csidrivers"]
    verbs: ["get"]
```

- [ ] **Step 2: Add a commented storage block**

In `values.yaml` `configToml`, after the `[kubernetes]` block and before `[egress]`, add:

```toml
  # [storage]
  # s3 volume backend (Kubernetes runtime). Requires the Mountpoint for
  # Amazon S3 CSI driver and an IAM role bound to its ServiceAccount.
  # s3_csi_driver = "s3.csi.aws.com"
  # s3_mount_options = ["uid=1000", "gid=1000"]
  # s3_allowed_buckets = []
```

- [ ] **Step 3: Render the chart**

Run: `helm template test kubernetes/charts/opensandbox-server | grep -A2 -E 'persistentvolumes|csidrivers'`
Expected: both rules appear. If `helm` is not installed, note it in the handoff.

- [ ] **Step 4: Commit**

```bash
git add kubernetes/charts/opensandbox-server/templates/server.yaml kubernetes/charts/opensandbox-server/values.yaml
git commit -m "feat(helm): grant PV and CSIDriver RBAC for s3 volumes"
```

---

### Task 11: Python SDK

**Files:**
- Modify: `sdks/sandbox/python/src/opensandbox/models/sandboxes.py:509-630`
- Modify: `sdks/sandbox/python/src/opensandbox/adapters/converter/sandbox_model_converter.py:115-172`
- Regenerate: `sdks/sandbox/python/src/opensandbox/api/lifecycle/models/` via `uv run python scripts/generate_api.py` (run from `sdks/sandbox/python`)
- Export: `sdks/sandbox/python/src/opensandbox/models/__init__.py` (add `S3` where `OSSFS` is exported)
- Test: `sdks/sandbox/python/tests/test_models_stability.py`, `sdks/sandbox/python/tests/test_converters_and_error_handling.py`

**Interfaces:**
- Produces: `opensandbox.models.sandboxes.S3` (Pydantic), `Volume.s3`; generated `opensandbox.api.lifecycle.models.s3.S3`.

- [ ] **Step 1: Write the failing tests**

In `test_models_stability.py`, add `S3` to the models import and append:

```python
def test_s3_backend_minimal() -> None:
    backend = S3(bucket="my-team-sandbox-logs")
    assert backend.bucket == "my-team-sandbox-logs"
    assert backend.prefix is None
    assert backend.region is None
    assert backend.options is None


def test_volume_with_s3_backend() -> None:
    vol = Volume(name="logs", s3=S3(bucket="b", prefix="p/"), mountPath="/mnt/logs")
    assert vol.s3 is not None and vol.s3.prefix == "p/"
    assert vol.host is None and vol.pvc is None and vol.ossfs is None


def test_volume_rejects_s3_with_pvc() -> None:
    with pytest.raises(ValueError, match="multiple"):
        Volume(name="x", s3=S3(bucket="b"), pvc=PVC(claimName="c"), mountPath="/x")
```

In `test_converters_and_error_handling.py`, next to the existing ossfs converter test (line ~746), append:

```python
def test_sandbox_model_converter_maps_s3_volume() -> None:
    from opensandbox.models.sandboxes import S3, Volume

    volume = Volume(
        name="logs",
        s3=S3(bucket="b", prefix="p/", region="eu-west-1", options=["uid=1000"]),
        mount_path="/mnt/logs",
        read_only=True,
    )
    dumped = SandboxModelConverter.to_api_volume(volume).to_dict()
    assert dumped["s3"] == {"bucket": "b", "prefix": "p/", "region": "eu-west-1", "options": ["uid=1000"]}
    assert dumped["readOnly"] is True
    assert "ossfs" not in dumped and "pvc" not in dumped and "host" not in dumped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd sdks/sandbox/python && uv run pytest tests/test_models_stability.py tests/test_converters_and_error_handling.py -k s3 -v`
Expected: FAIL with `ImportError: cannot import name 'S3'`.

- [ ] **Step 3: Regenerate the API client from the spec**

Run: `cd sdks/sandbox/python && uv run python scripts/generate_api.py`
Expected: a new file `src/opensandbox/api/lifecycle/models/s3.py` with class `S3` (attrs `bucket`, `prefix`, `region`, `options`) and `volume.py` gains `s3`. Confirm `models/__init__.py` exports `S3`.

- [ ] **Step 4: Add the domain model and converter**

In `models/sandboxes.py`, after `class OSSFS`:

```python
class S3(BaseModel):
    """Amazon S3 mount backend (Kubernetes runtime only, credentials from the cluster IAM role)."""

    bucket: str = Field(description="S3 bucket name.")
    prefix: str | None = Field(
        default=None,
        description="Optional key prefix to mount, relative, no leading '/'.",
    )
    region: str | None = Field(default=None, description="Optional AWS region, e.g. 'eu-west-1'.")
    options: list[str] | None = Field(
        default=None,
        description="Additional Mountpoint mount options without leading '-'.",
    )
    model_config = ConfigDict(populate_by_name=True)
```

In `class Volume`, after `ossfs`:

```python
    s3: S3 | None = Field(
        default=None,
        description="Amazon S3 mount backend (Kubernetes runtime only).",
    )
```

Update the exactly-one validator: `backends = [self.host, self.pvc, self.ossfs, self.s3]` and both messages to `(host, pvc, ossfs, s3)`. Update the class docstring.

In `sandbox_model_converter.py`, `to_api_volume`: add the import `from opensandbox.api.lifecycle.models.s3 import S3 as ApiS3`, then after the `api_ossfs` block:

```python
        api_s3 = UNSET
        if volume.s3 is not None and not isinstance(volume.s3, Unset):
            api_s3 = ApiS3(
                bucket=volume.s3.bucket,
                prefix=volume.s3.prefix if volume.s3.prefix is not None else UNSET,
                region=volume.s3.region if volume.s3.region is not None else UNSET,
                options=volume.s3.options if volume.s3.options is not None else UNSET,
            )
```

and pass `s3=api_s3,` to `ApiVolume(...)`.

Export `S3` from `models/__init__.py` next to `OSSFS`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd sdks/sandbox/python && uv run pytest tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sdks/sandbox/python
git commit -m "feat(sdk-python): add s3 volume backend model"
```

---

### Task 12: TypeScript SDK

**Files:**
- Modify: `sdks/sandbox/javascript/src/models/sandboxes.ts:335-400`
- Modify: `sdks/sandbox/javascript/src/sandbox.ts:368-378`
- Modify: `sdks/sandbox/javascript/src/index.ts:71`
- Regenerate: `sdks/sandbox/javascript/src/api/lifecycle.ts` via `pnpm run gen:api`
- Test: `sdks/sandbox/javascript/tests/sandbox.create.test.mjs`

- [ ] **Step 1: Write the failing test**

Append to `tests/sandbox.create.test.mjs`, next to the OSSFS test (line 325):

```javascript
test("Sandbox.create passes S3 volume to request", async () => {
  const { adapterFactory, recordedRequests } = createAdapterFactory();

  await Sandbox.create({
    adapterFactory,
    connectionConfig: { domain: "http://127.0.0.1:8080" },
    image: "python:3.12",
    skipHealthCheck: true,
    volumes: [
      {
        name: "logs",
        s3: { bucket: "my-team-sandbox-logs", prefix: "sandboxes/task-001/", region: "eu-west-1" },
        mountPath: "/mnt/logs",
      },
    ],
  });

  assert.equal(recordedRequests.length, 1);
  assert.equal(recordedRequests[0].volumes[0].s3.bucket, "my-team-sandbox-logs");
  assert.equal(recordedRequests[0].volumes[0].s3.prefix, "sandboxes/task-001/");
  assert.equal(recordedRequests[0].volumes[0].ossfs, undefined);
});

test("Sandbox.create rejects volume with s3 and pvc", async () => {
  const { adapterFactory } = createAdapterFactory();

  await assert.rejects(
    Sandbox.create({
      adapterFactory,
      connectionConfig: { domain: "http://127.0.0.1:8080" },
      image: "python:3.12",
      skipHealthCheck: true,
      volumes: [
        { name: "x", s3: { bucket: "b" }, pvc: { claimName: "c" }, mountPath: "/x" },
      ],
    }),
    /must specify exactly one backend \(host, pvc, ossfs, s3\)/
  );
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd sdks/sandbox/javascript && pnpm run test`
Expected: the two new tests FAIL (the request records no `s3`, and the message lacks `s3`). The build step runs `gen:api` and `tsup`.

- [ ] **Step 3: Add the model, validation and export**

In `src/models/sandboxes.ts`, after the `OSSFS` interface:

```typescript
/**
 * Amazon S3 mount backend. Kubernetes runtime only.
 *
 * The server creates a static PersistentVolume for the Mountpoint for Amazon S3
 * CSI driver and mounts it into the sandbox. Credentials come from the IAM role
 * bound to the CSI driver ServiceAccount; the request carries none.
 */
export interface S3 extends Record<string, unknown> {
  /** S3 bucket name. */
  bucket: string;
  /** Optional key prefix to mount, relative, no leading "/". */
  prefix?: string;
  /** Optional AWS region, e.g. "eu-west-1". */
  region?: string;
  /** Additional Mountpoint mount options without leading "-". */
  options?: string[];
}
```

In `Volume`, after `ossfs?: OSSFS;`:

```typescript
  /**
   * Amazon S3 mount backend (mutually exclusive with host, pvc, ossfs). Kubernetes runtime only.
   */
  s3?: S3;
```

Update the `Volume` doc comment list of backends.

In `src/sandbox.ts` line 368, change to `[vol.host, vol.pvc, vol.ossfs, vol.s3]` and both messages to `(host, pvc, ossfs, s3)`.

In `src/index.ts`, export `S3` next to `OSSFS`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd sdks/sandbox/javascript && pnpm run test`
Expected: PASS. Confirm `src/api/lifecycle.ts` now contains `S3` under `components["schemas"]` (generated).

- [ ] **Step 5: Commit**

```bash
git add sdks/sandbox/javascript
git commit -m "feat(sdk-js): add s3 volume backend model"
```

---

### Task 13: Go and C# SDKs

**Files:**
- Modify: `sdks/sandbox/go/types.go:95-131`
- Test: `sdks/sandbox/go/types_s3_test.go` (new)
- Modify: `sdks/sandbox/csharp/src/OpenSandbox/Models/Sandboxes.cs:600-680`
- Test: `sdks/sandbox/csharp/tests/OpenSandbox.Tests/ModelsTests.cs`

- [ ] **Step 1: Write the failing Go test**

Create `sdks/sandbox/go/types_s3_test.go` (use the same package name as `types.go`):

```go
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
```

- [ ] **Step 2: Run Go test to verify it fails**

Run: `cd sdks/sandbox/go && go test ./... -run TestVolumeS3 -v`
Expected: compile error `undefined: S3`.

- [ ] **Step 3: Add the Go type**

In `types.go`, add `S3 *S3 \`json:"s3,omitempty"\`` to `Volume` after `OSSFS`, and after the `OSSFS` struct:

```go
// S3 represents an Amazon S3 mount backend (Kubernetes runtime only).
// Credentials come from the IAM role bound to the CSI driver; none are sent.
type S3 struct {
	Bucket  string   `json:"bucket"`
	Prefix  string   `json:"prefix,omitempty"`
	Region  string   `json:"region,omitempty"`
	Options []string `json:"options,omitempty"`
}
```

Run `gofmt -w types.go` so the struct tags align.

- [ ] **Step 4: Run Go tests to verify they pass**

Run: `cd sdks/sandbox/go && go test ./... -run TestVolumeS3 -v`
Expected: PASS.

- [ ] **Step 5: Write the failing C# test**

In `ModelsTests.cs`, after `Volume_WithOssfs_ShouldSerializeExpectedPayload`:

```csharp
    [Fact]
    public void Volume_WithS3_ShouldSerializeExpectedPayload()
    {
        var request = new CreateSandboxRequest
        {
            Image = new ImageSpec { Uri = "python:3.11" },
            ResourceLimits = new Dictionary<string, string>(),
            Entrypoint = new List<string> { "python" },
            Volumes = new List<Volume>
            {
                new()
                {
                    Name = "logs",
                    MountPath = "/mnt/logs",
                    S3 = new S3
                    {
                        Bucket = "my-team-sandbox-logs",
                        Prefix = "sandboxes/task-001/",
                        Region = "eu-west-1",
                        Options = new List<string> { "uid=1000" }
                    }
                }
            }
        };

        string json = JsonSerializer.Serialize(request);

        json.Should().Contain("\"s3\":");
        json.Should().Contain("\"bucket\":\"my-team-sandbox-logs\"");
        json.Should().Contain("\"prefix\":\"sandboxes/task-001/\"");
        json.Should().Contain("\"region\":\"eu-west-1\"");
        json.Should().NotContain("\"ossfs\":");
        json.Should().NotContain("accessKeyId");
    }
```

- [ ] **Step 6: Run C# test to verify it fails**

Run: `cd sdks/sandbox/csharp && dotnet test --filter Volume_WithS3 `
Expected: compile error: `S3` not found.

- [ ] **Step 7: Add the C# model**

In `Sandboxes.cs`, after `class OSSFS`:

```csharp
/// <summary>
/// Amazon S3 mount backend. Kubernetes runtime only; credentials come from the
/// IAM role bound to the CSI driver ServiceAccount.
/// </summary>
public class S3
{
    /// <summary>Gets or sets the S3 bucket name.</summary>
    [JsonPropertyName("bucket")]
    public required string Bucket { get; set; }

    /// <summary>Gets or sets the optional key prefix to mount (relative, no leading '/').</summary>
    [JsonPropertyName("prefix")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Prefix { get; set; }

    /// <summary>Gets or sets the optional AWS region, e.g. "eu-west-1".</summary>
    [JsonPropertyName("region")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Region { get; set; }

    /// <summary>Gets or sets additional Mountpoint mount options without leading '-'.</summary>
    [JsonPropertyName("options")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public IReadOnlyList<string>? Options { get; set; }
}
```

In `class Volume`, after `Ossfs`:

```csharp
    /// <summary>
    /// Gets or sets the Amazon S3 backend configuration (Kubernetes runtime only).
    /// </summary>
    [JsonPropertyName("s3")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public S3? S3 { get; set; }
```

Match how the existing `Host`, `Pvc` and `Ossfs` properties handle null serialization (if they already rely on a global `JsonIgnoreCondition`, drop the per-property attribute to stay consistent). Update the `Volume` summary to list `S3`.

- [ ] **Step 8: Run C# tests to verify they pass**

Run: `cd sdks/sandbox/csharp && dotnet test`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add sdks/sandbox/go sdks/sandbox/csharp
git commit -m "feat(sdk-go,sdk-csharp): add s3 volume backend model"
```

---

### Task 14: Kotlin SDK

**Files:**
- Modify: `sdks/sandbox/kotlin/sandbox/src/main/kotlin/com/alibaba/opensandbox/sandbox/domain/models/sandboxes/SandboxModels.kt:498-700`
- Modify: `sdks/sandbox/kotlin/sandbox/src/main/kotlin/com/alibaba/opensandbox/sandbox/infrastructure/adapters/converter/SandboxModelConverter.kt:220-260`
- Regenerate: `./gradlew :sandbox-api:generateLifecycleApi` (from `sdks/sandbox/kotlin`)
- Test: `sdks/sandbox/kotlin/sandbox/src/test/kotlin/com/alibaba/opensandbox/sandbox/domain/models/VolumeModelsTest.kt`

- [ ] **Step 1: Write the failing tests**

Add `import com.alibaba.opensandbox.sandbox.domain.models.sandboxes.S3` and append to `VolumeModelsTest`:

```kotlin
    @Test
    fun `S3 should require bucket and keep optional fields null`() {
        val backend = S3.builder().bucket("my-team-sandbox-logs").build()
        assertEquals("my-team-sandbox-logs", backend.bucket)
        assertNull(backend.prefix)
        assertNull(backend.region)
        assertNull(backend.options)
    }

    @Test
    fun `S3 builder should reject blank bucket`() {
        assertThrows(IllegalArgumentException::class.java) { S3.builder().bucket("  ").build() }
    }

    @Test
    fun `Volume with S3 backend should be created correctly`() {
        val volume =
            Volume.builder()
                .name("logs")
                .s3(
                    S3.builder()
                        .bucket("my-team-sandbox-logs")
                        .prefix("sandboxes/task-001/")
                        .region("eu-west-1")
                        .options("uid=1000", "gid=1000")
                        .build(),
                )
                .mountPath("/mnt/logs")
                .readOnly(true)
                .build()

        assertNotNull(volume.s3)
        assertEquals("sandboxes/task-001/", volume.s3?.prefix)
        assertEquals(listOf("uid=1000", "gid=1000"), volume.s3?.options)
        assertNull(volume.ossfs)
        assertTrue(volume.readOnly)
    }

    @Test
    fun `Volume should reject S3 together with PVC`() {
        assertThrows(IllegalArgumentException::class.java) {
            Volume.builder()
                .name("x")
                .s3(S3.builder().bucket("b").build())
                .pvc(PVC.of("c"))
                .mountPath("/x")
                .build()
        }
    }
```

If `PVC.of` does not exist, use the PVC builder shape already used in the same test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd sdks/sandbox/kotlin && ./gradlew :sandbox:test --tests '*VolumeModelsTest*'`
Expected: compile error: unresolved reference `S3`.

- [ ] **Step 3: Regenerate the API models**

Run: `cd sdks/sandbox/kotlin && ./gradlew :sandbox-api:generateLifecycleApi`
Expected: generated `com.alibaba.opensandbox.sandbox.api.models.S3` and `ApiVolume.s3`.

- [ ] **Step 4: Add the domain model and converter**

In `SandboxModels.kt`, after `class OSSFS`:

```kotlin
/**
 * Amazon S3 mount backend. Kubernetes runtime only; credentials come from the
 * IAM role bound to the CSI driver ServiceAccount.
 *
 * @property bucket S3 bucket name
 * @property prefix Optional key prefix to mount (relative, no leading '/')
 * @property region Optional AWS region, e.g. `eu-west-1`
 * @property options Additional Mountpoint mount options without leading '-'
 */
class S3 private constructor(
    val bucket: String,
    val prefix: String?,
    val region: String?,
    val options: List<String>?,
) {
    companion object {
        @JvmStatic
        fun builder(): Builder = Builder()
    }

    class Builder {
        private var bucket: String? = null
        private var prefix: String? = null
        private var region: String? = null
        private var options: List<String>? = null

        fun bucket(bucket: String): Builder {
            require(bucket.isNotBlank()) { "S3 bucket cannot be blank" }
            this.bucket = bucket
            return this
        }

        fun prefix(prefix: String): Builder {
            this.prefix = prefix
            return this
        }

        fun region(region: String): Builder {
            this.region = region
            return this
        }

        fun options(options: List<String>): Builder {
            this.options = options
            return this
        }

        fun options(vararg options: String): Builder = options(options.toList())

        fun build(): S3 {
            val bucketValue = bucket ?: throw IllegalArgumentException("S3 bucket must be specified")
            return S3(bucket = bucketValue, prefix = prefix, region = region, options = options)
        }
    }
}
```

In `class Volume`: add `val s3: S3?` to the constructor after `ossfs`, a `private var s3: S3? = null` in `Builder`, a `fun s3(s3: S3): Builder`, include `s3` in `listOfNotNull(host, pvc, ossfs, s3)`, update both messages to `(host, pvc, ossfs, s3)`, and pass `s3 = s3` in the constructor call. Update the KDoc.

In `SandboxModelConverter.kt`, add `import com.alibaba.opensandbox.sandbox.domain.models.sandboxes.S3` and `import com.alibaba.opensandbox.sandbox.api.models.S3 as ApiS3`, then:

```kotlin
    /**
     * Converts Domain S3 -> API S3
     */
    fun S3.toApiS3(): ApiS3 {
        return ApiS3(
            bucket = this.bucket,
            prefix = this.prefix,
            region = this.region,
            options = this.options,
        )
    }
```

and in `Volume.toApiVolume()` add `s3 = this.s3?.toApiS3(),`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd sdks/sandbox/kotlin && ./gradlew :sandbox:test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sdks/sandbox/kotlin
git commit -m "feat(sdk-kotlin): add s3 volume backend model"
```

---

### Task 15: Documentation and OSEP

**Files:**
- Create: `docs/examples/kubernetes-s3-volume-mount.md`
- Modify: `docs/examples/index.md:58-66`
- Modify: `docs/.vitepress/config.mts:244` (add a nav item after the Kubernetes PVC entry)
- Modify: `docs/architecture/index.md:280-284`
- Create: `oseps/00NN-s3-volume-backend.md` with `oseps/init-osep.sh`

- [ ] **Step 1: Write the example page**

Create `docs/examples/kubernetes-s3-volume-mount.md`:

````markdown
---
title: Kubernetes S3
description: Mount an Amazon S3 bucket prefix into OpenSandbox containers on EKS with no access keys.
---

# Kubernetes S3 Volume Mount

This example mounts an S3 bucket prefix at a path inside a sandbox that runs on Amazon EKS. The server uses the [Mountpoint for Amazon S3 CSI driver](https://github.com/awslabs/mountpoint-s3-csi-driver). Credentials come from an IAM role bound to the driver ServiceAccount. The API request carries no keys.

The `s3` backend is available on the Kubernetes runtime only. The Docker runtime rejects it with `VOLUME::UNSUPPORTED_BACKEND`.

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
```

## Create a sandbox with an S3 volume

(Use the same `Sandbox.create` call shape as `docs/examples/kubernetes-pvc-volume-mount.md`; only the volume differs.)

```python
from opensandbox import Sandbox
from opensandbox.models.sandboxes import S3, Volume

sandbox = Sandbox.create(
    image="python:3.12",
    volumes=[
        Volume(
            name="logs",
            s3=S3(bucket="my-team-sandbox-logs", prefix="sandboxes/task-001/", region="eu-west-1"),
            mount_path="/mnt/logs",
        )
    ],
)
sandbox.commands.run("echo hello > /mnt/logs/step-1.stdout")
```

The object `sandboxes/task-001/step-1.stdout` appears in the bucket when the file is closed.

## What the server creates

For each `s3` volume the server creates a `PersistentVolume` named `s3-<sandbox-id>-<volume-name>` (CSI driver `s3.csi.aws.com`, `bucketName`, mount options) and a `PersistentVolumeClaim` of the same name, then mounts the claim. Both objects carry `opensandbox.io/volume-managed-by=server` and `opensandbox.io/id=<sandbox-id>`. The server removes them when the sandbox is deleted or expires, and sweeps orphaned PVs at startup.

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
````

- [ ] **Step 2: Add index, nav and architecture entries**

In `docs/examples/index.md`, add a row after the Kubernetes PVC row:

```markdown
| [Kubernetes S3](/examples/kubernetes-s3-volume-mount) | Amazon S3 mounts on EKS with no access keys |
```

In `docs/.vitepress/config.mts`, after the item with `link: "/examples/kubernetes-pvc-volume-mount"`, add an item with the same shape and `text: "Kubernetes S3"`, `link: "/examples/kubernetes-s3-volume-mount"`.

In `docs/architecture/index.md`, after the `ossfs` bullet add:

```markdown
- `s3`: mount an Amazon S3 bucket prefix through the Mountpoint for Amazon S3 CSI driver (Kubernetes runtime only, no access keys).
```

Replace the sentence `Volume mounts are supported for Docker and Kubernetes container workloads; the FastSandbox adapter currently rejects volume mounts.` with:

```markdown
Backend support by runtime: Docker supports `host`, `pvc` and `ossfs`; Kubernetes supports `host`, `pvc` and `s3`. The FastSandbox adapter rejects volume mounts.
```

- [ ] **Step 3: Build the docs**

Run: `cd docs && pnpm install --frozen-lockfile && pnpm run build`
Expected: build succeeds with no dead links.

- [ ] **Step 4: Create the OSEP**

Run: `cd oseps && ./init-osep.sh --status implementing --author "@<your-github-handle>" "S3 Volume Backend"`

Fill the generated file from the spec `docs/superpowers/specs/2026-09-15-s3-volume-backend-design.md`: Summary, Motivation, Goals, Non-Goals, Proposal (API shape), Design Details (server flow, Kubernetes objects, identity, semantics, errors), Test Plan (unit tests + manual EKS procedure), Alternatives (FUSE sidecar, preStart hook, operator-created PVs). Copy the text, do not leave template comments. Add the new OSEP to the index in `docs/community/oseps.md` following the existing row format.

- [ ] **Step 5: Commit**

```bash
git add docs oseps
git commit -m "docs: add Kubernetes S3 volume mount guide and OSEP"
```

---

### Task 16: Full verification

- [ ] **Step 1: Server suite**

Run: `cd server && uv run ruff check && uv run pytest -q`
Expected: all PASS.

- [ ] **Step 2: Type check**

Run: `cd server && uv run pyright`
Expected: no new errors compared to `main` (compare counts if the baseline is non-zero).

- [ ] **Step 3: SDK suites**

Run each: `cd sdks/sandbox/python && uv run pytest -q`; `cd sdks/sandbox/javascript && pnpm run test`; `cd sdks/sandbox/go && go test ./...`; `cd sdks/sandbox/csharp && dotnet test`; `cd sdks/sandbox/kotlin && ./gradlew :sandbox:test`.
Expected: all PASS.

- [ ] **Step 4: Handoff notes**

In the final report, state explicitly: the Kind e2e suite does not cover `s3`; the manual EKS procedure in the docs page was not run unless it was.
