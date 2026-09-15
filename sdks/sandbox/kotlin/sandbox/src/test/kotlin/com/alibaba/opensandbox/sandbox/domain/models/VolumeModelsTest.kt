/*
 * Copyright 2025 Alibaba Group Holding Ltd.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.alibaba.opensandbox.sandbox.domain.models

import com.alibaba.opensandbox.sandbox.domain.models.sandboxes.Host
import com.alibaba.opensandbox.sandbox.domain.models.sandboxes.OSSFS
import com.alibaba.opensandbox.sandbox.domain.models.sandboxes.PVC
import com.alibaba.opensandbox.sandbox.domain.models.sandboxes.S3
import com.alibaba.opensandbox.sandbox.domain.models.sandboxes.Volume
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class VolumeModelsTest {
    @Test
    fun `Host should require absolute path`() {
        val backend = Host.of("/data/shared")
        assertEquals("/data/shared", backend.path)
    }

    @Test
    fun `Host should accept windows path with backslashes`() {
        val backend = Host.of("D:\\sandbox-mnt\\ReMe")
        assertEquals("D:\\sandbox-mnt\\ReMe", backend.path)
    }

    @Test
    fun `Host should accept windows path with forward slashes`() {
        val backend = Host.of("D:/sandbox-mnt/ReMe")
        assertEquals("D:/sandbox-mnt/ReMe", backend.path)
    }

    @Test
    fun `Host should accept windows drive root`() {
        val backend = Host.of("Z:\\")
        assertEquals("Z:\\", backend.path)
    }

    @Test
    fun `Host should reject relative path`() {
        assertThrows(IllegalArgumentException::class.java) {
            Host.of("relative/path")
        }
    }

    @Test
    fun `PVC should accept valid claim name`() {
        val backend = PVC.of("my-pvc")
        assertEquals("my-pvc", backend.claimName)
    }

    @Test
    fun `PVC should reject blank claim name`() {
        assertThrows(IllegalArgumentException::class.java) {
            PVC.of("   ")
        }
    }

    @Test
    fun `Volume with host backend should be created correctly`() {
        val volume =
            Volume.builder()
                .name("data")
                .host(Host.of("/data/shared"))
                .mountPath("/mnt/data")
                .build()

        assertEquals("data", volume.name)
        assertNotNull(volume.host)
        assertEquals("/data/shared", volume.host?.path)
        assertNull(volume.pvc)
        assertEquals("/mnt/data", volume.mountPath)
        assertFalse(volume.readOnly) // default is read-write
        assertNull(volume.subPath)
    }

    @Test
    fun `Volume with PVC backend should be created correctly`() {
        val volume =
            Volume.builder()
                .name("models")
                .pvc(PVC.of("shared-models"))
                .mountPath("/mnt/models")
                .readOnly(true)
                .subPath("v1")
                .build()

        assertEquals("models", volume.name)
        assertNull(volume.host)
        assertNotNull(volume.pvc)
        assertEquals("shared-models", volume.pvc?.claimName)
        assertEquals("/mnt/models", volume.mountPath)
        assertTrue(volume.readOnly)
        assertEquals("v1", volume.subPath)
    }

    @Test
    fun `OSSFS should require inline credentials and default to version 2_0`() {
        val backend =
            OSSFS.builder()
                .bucket("bucket-a")
                .endpoint("oss-cn-hangzhou.aliyuncs.com")
                .accessKeyId("ak")
                .accessKeySecret("sk")
                .build()

        assertEquals("bucket-a", backend.bucket)
        assertEquals("oss-cn-hangzhou.aliyuncs.com", backend.endpoint)
        assertEquals("ak", backend.accessKeyId)
        assertEquals("sk", backend.accessKeySecret)
        assertEquals(OSSFS.VERSION_2_0, backend.version)
        assertNull(backend.options)
    }

    @Test
    fun `Volume with OSSFS backend should be created correctly`() {
        val volume =
            Volume.builder()
                .name("oss")
                .ossfs(
                    OSSFS.builder()
                        .bucket("bucket-a")
                        .endpoint("oss-cn-hangzhou.aliyuncs.com")
                        .accessKeyId("ak")
                        .accessKeySecret("sk")
                        .version(OSSFS.VERSION_1_0)
                        .options("allow_other", "max_stat_cache_size=0")
                        .build(),
                )
                .mountPath("/mnt/oss")
                .subPath("prefix")
                .build()

        assertEquals("oss", volume.name)
        assertNull(volume.host)
        assertNull(volume.pvc)
        assertNotNull(volume.ossfs)
        assertEquals("bucket-a", volume.ossfs?.bucket)
        assertEquals(OSSFS.VERSION_1_0, volume.ossfs?.version)
        assertEquals(listOf("allow_other", "max_stat_cache_size=0"), volume.ossfs?.options)
        assertEquals("/mnt/oss", volume.mountPath)
        assertFalse(volume.readOnly)
        assertEquals("prefix", volume.subPath)
    }

    @Test
    fun `Volume should reject blank name`() {
        assertThrows(IllegalArgumentException::class.java) {
            Volume.builder()
                .name("   ")
                .host(Host.of("/data"))
                .mountPath("/mnt")
                .build()
        }
    }

    @Test
    fun `Volume should require absolute mount path`() {
        assertThrows(IllegalArgumentException::class.java) {
            Volume.builder()
                .name("test")
                .host(Host.of("/data"))
                .mountPath("relative/path")
                .build()
        }
    }

    @Test
    fun `Volume should reject no backend specified`() {
        assertThrows(IllegalArgumentException::class.java) {
            Volume.builder()
                .name("test")
                .mountPath("/mnt")
                .build()
        }
    }

    @Test
    fun `Volume should reject multiple backends specified`() {
        assertThrows(IllegalArgumentException::class.java) {
            Volume.builder()
                .name("test")
                .host(Host.of("/data"))
                .pvc(PVC.of("my-pvc"))
                .ossfs(
                    OSSFS.builder()
                        .bucket("bucket-a")
                        .endpoint("oss-cn-hangzhou.aliyuncs.com")
                        .accessKeyId("ak")
                        .accessKeySecret("sk")
                        .build(),
                )
                .mountPath("/mnt")
                .build()
        }
    }

    @Test
    fun `Volume should require name`() {
        assertThrows(IllegalArgumentException::class.java) {
            Volume.builder()
                .host(Host.of("/data"))
                .mountPath("/mnt")
                .build()
        }
    }

    @Test
    fun `Volume should require mount path`() {
        assertThrows(IllegalArgumentException::class.java) {
            Volume.builder()
                .name("test")
                .host(Host.of("/data"))
                .build()
        }
    }

    @Test
    fun `Volume readOnly defaults to false`() {
        val volume =
            Volume.builder()
                .name("test")
                .host(Host.of("/data"))
                .mountPath("/mnt")
                .build()

        assertFalse(volume.readOnly)
    }

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
}
