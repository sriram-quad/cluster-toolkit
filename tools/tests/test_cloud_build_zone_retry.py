#!/usr/bin/env python3
# Copyright 2026 Google LLC
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

"""Unit tests for Cluster Toolkit Cloud Build zone retry and exclusion logic.

How to run these tests:
  Direct execution:
    python3 tools/tests/test_cloud_build_zone_retry.py

  Via unittest module:
    python3 -m unittest tools/tests/test_cloud_build_zone_retry.py -v

  Via test runner discovery:
    python3 -m unittest discover tools/tests/

  Via pre-commit (automatically run on commit):
    pre-commit run pytest-check --all-files
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import yaml

UPDATE_SCRIPT = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../cloud-build/update_job_exclude_zones.py",
    )
)

SAMPLE_JOB_YAML = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: ml-a3-ultragpu-onspot-slurm-123456
  namespace: default
  labels:
    kueue.x-k8s.io/queue-name: local-queue-test-locks
spec:
  suspend: true
  template:
    spec:
      containers:
      - name: runner
        image: us-central1-docker.pkg.dev/proj/repo/test-runner:123
        env:
        - name: CHECK_LUSTRE
          value: "true"
        - name: REQUIRED_LUSTRE_GB
          value: "36000"
        - name: BUILD_ID
          value: "123456"
"""


class TestCloudBuildZoneRetry(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_extract_failed_zone_from_lustre_exhaustion_log(self):
        """Test extracting failed zone from Managed Lustre exhaustion logs."""
        log_content = (
            "module.homefs.google_lustre_instance.lustre_instance: Creating...\n"
            "Error: Error waiting for creating Instance: Error code 8, message: "
            "resource exhausted: not enough resources available to fulfill the "
            "request in us-west1-b\n"
            "with module.homefs.google_lustre_instance.lustre_instance,\n"
        )
        log_file = os.path.join(self.test_dir, "job_logs.txt")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(log_content)

        pattern = (
            r".*resource exhausted: not enough resources available to fulfill "
            r"the request in \([a-z0-9-]*\).*"
        )
        cmd = f"sed -n 's/{pattern}/\\1/p' {log_file} | tail -n 1"
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "us-west1-b")

    def test_check_retriable_error_matches_lustre_internal_resource_exhaustion(self):
        """Test check_retriable_error.sh matches Lustre internal resource exhaustion."""
        cloud_build_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../cloud-build")
        )
        check_script = os.path.join(cloud_build_dir, "check_retriable_error.sh")
        error_msg = (
            "Error waiting for creating Instance: Error code 8, message: "
            "resource exhausted: system limit for internal resources has been reached\n"
        )
        log_file = os.path.join(self.test_dir, "lustre_stockout_log.txt")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(error_msg)

        res = subprocess.run(
            ["bash", check_script, log_file],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)

    def test_extract_failed_zone_from_spot_stockout_log(self):
        """Test extracting zone from 'Deploying in ZONE:' only on capacity error."""
        log_content = (
            "Deploying in ZONE: us-south1-b, MODEL: SPOT\n"
            "Starting terraform deployment...\n"
            "google_compute_region_instance_group_manager: "
            "ZONE_RESOURCE_POOL_EXHAUSTED\n"
        )
        log_file = os.path.join(self.test_dir, "job_logs.txt")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(log_content)

        extract_pattern = (
            r".*resource exhausted: not enough resources available to fulfill "
            r"the request in \([a-z0-9-]*\).*"
        )
        zone_pattern = r".*Deploying in ZONE: \([a-z0-9-]*\).*"
        capacity_pattern = (
            "ZONE_RESOURCE_POOL_EXHAUSTED|"
            "not enough resources available|"
            "resource exhausted|"
            "stockout"
        )
        script = f"""
        FAILED_ZONE=$(sed -n 's/{extract_pattern}/\\1/p' {log_file} |
            tail -n 1 || true)
        if [ -z "$FAILED_ZONE" ]; then
            if grep -q -i -E "{capacity_pattern}" {log_file}; then
                FAILED_ZONE=$(sed -n 's/{zone_pattern}/\\1/p' {log_file} |
                    tail -n 1 || true)
            fi
        fi
        echo "$FAILED_ZONE"
        """
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "us-south1-b")

    def test_no_zone_extraction_on_non_capacity_retriable_error(self):
        """Verify that non-capacity transient errors do not extract healthy zones."""
        log_content = (
            "Deploying in ZONE: us-central1-a, MODEL: SPOT\n"
            "Error: googleapi: Error 429: Rate Limit Exceeded, RATE_LIMIT_EXCEEDED\n"
        )
        log_file = os.path.join(self.test_dir, "job_logs.txt")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(log_content)

        extract_pattern = (
            r".*resource exhausted: not enough resources available to fulfill "
            r"the request in \([a-z0-9-]*\).*"
        )
        zone_pattern = r".*Deploying in ZONE: \([a-z0-9-]*\).*"
        capacity_pattern = (
            "ZONE_RESOURCE_POOL_EXHAUSTED|"
            "not enough resources available|"
            "resource exhausted|"
            "stockout"
        )
        script = f"""
        FAILED_ZONE=$(sed -n 's/{extract_pattern}/\\1/p' {log_file} |
            tail -n 1 || true)
        if [ -z "$FAILED_ZONE" ]; then
            if grep -q -i -E "{capacity_pattern}" {log_file}; then
                FAILED_ZONE=$(sed -n 's/{zone_pattern}/\\1/p' {log_file} |
                    tail -n 1 || true)
            fi
        fi
        echo "$FAILED_ZONE"
        """
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "")

    def test_extract_failed_zone_case_insensitive_variants(self):
        """Test case-insensitive detection of all capacity exhaustion variants."""
        cases = [
            (
                "Deploying in ZONE: us-central1-a, MODEL: SPOT\n"
                "Error: googleapi: Error code 8, message: "
                "resource exhausted: system limit for internal resources "
                "has been reached\n",
                "us-central1-a",
            ),
            (
                "Deploying in ZONE: us-east4-c, MODEL: SPOT\n"
                "google_compute_region_instance_group_manager: "
                "zone_resource_pool_exhausted\n",
                "us-east4-c",
            ),
            (
                "Deploying in ZONE: us-west4-b, MODEL: SPOT\n"
                "Error: Instance group manager experienced a STOCKOUT "
                "during creation\n",
                "us-west4-b",
            ),
            (
                "Deploying in ZONE: us-west1-c, MODEL: SPOT\n"
                "Compute Engine does NOT ENOUGH RESOURCES AVAILABLE "
                "in the specified zone.\n",
                "us-west1-c",
            ),
            (
                "Deploying in ZONE: europe-west4-a, MODEL: SPOT\n"
                "Error: Resource Exhausted when creating compute instance.\n",
                "europe-west4-a",
            ),
            (
                "Deploying in ZONE: us-central1-a, MODEL: SPOT\n"
                "Error: Rate Limit Exceeded (HTTP 429)\n",
                "",
            ),
            (
                "Deploying in ZONE: us-central1-a, MODEL: SPOT\n"
                "Error: 503 Service Unavailable\n",
                "",
            ),
        ]

        extract_pattern = (
            r".*resource exhausted: not enough resources available to fulfill "
            r"the request in \([a-z0-9-]*\).*"
        )
        zone_pattern = r".*Deploying in ZONE: \([a-z0-9-]*\).*"
        capacity_pattern = (
            "ZONE_RESOURCE_POOL_EXHAUSTED|"
            "not enough resources available|"
            "resource exhausted|"
            "stockout"
        )
        for log_snippet, expected_zone in cases:
            log_file = os.path.join(self.test_dir, "test_job_logs.txt")
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(log_snippet)

            script = f"""
            FAILED_ZONE=$(sed -n 's/{extract_pattern}/\\1/p' {log_file} |
                tail -n 1 || true)
            if [ -z "$FAILED_ZONE" ]; then
                if grep -q -i -E "{capacity_pattern}" {log_file}; then
                    FAILED_ZONE=$(sed -n 's/{zone_pattern}/\\1/p' {log_file} |
                        tail -n 1 || true)
                fi
            fi
            echo "$FAILED_ZONE"
            """
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, check=True
            )
            self.assertEqual(
                result.stdout.strip(),
                expected_zone,
                f"Failed for log snippet:\n{log_snippet}",
            )

    def test_update_job_exclude_zones_basic_injection_and_update(self):
        """Test update_job_exclude_zones.py injecting and updating EXCLUDE_ZONES."""
        job_file = os.path.join(self.test_dir, "job.yaml")
        with open(job_file, "w", encoding="utf-8") as f:
            f.write(SAMPLE_JOB_YAML)

        # 1. First injection
        subprocess.run(
            [
                "python3",
                UPDATE_SCRIPT,
                "--inject",
                "--file",
                job_file,
                "--zones",
                "us-west1-b",
            ],
            check=True,
        )

        with open(job_file, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)

        runner = next(
            c
            for c in parsed["spec"]["template"]["spec"]["containers"]
            if c["name"] == "runner"
        )
        entry = next((e for e in runner["env"] if e["name"] == "EXCLUDE_ZONES"), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["value"], "us-west1-b")

        # 2. Extraction
        res = subprocess.run(
            ["python3", UPDATE_SCRIPT, "--extract", "--file", job_file],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(res.stdout.strip(), "us-west1-b")

        # 3. Update with accumulated zones
        subprocess.run(
            [
                "python3",
                UPDATE_SCRIPT,
                "--inject",
                "--file",
                job_file,
                "--zones",
                "us-west1-b us-south1-b",
            ],
            check=True,
        )

        with open(job_file, "r", encoding="utf-8") as f:
            parsed_retry = yaml.safe_load(f)

        runner_retry = next(
            c
            for c in parsed_retry["spec"]["template"]["spec"]["containers"]
            if c["name"] == "runner"
        )
        entry_retry = next(
            (e for e in runner_retry["env"] if e["name"] == "EXCLUDE_ZONES"),
            None,
        )
        self.assertIsNotNone(entry_retry)
        self.assertEqual(entry_retry["value"], "us-west1-b us-south1-b")

    def test_update_job_exclude_zones_preserves_metadata_and_sidecars(self):
        """Test metadata labels and sidecars are not corrupted."""
        complex_job = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: test-job
  labels:
    env: staging
    app: test
spec:
  template:
    spec:
      containers:
      - name: sidecar
        image: gcr.io/sidecar:latest
        env:
        - name: SIDECAR_CONFIG
          value: "default"
      - name: runner
        image: gcr.io/runner:latest
        env:
        - name: CHECK_LUSTRE
          value: "true"
      volumes:
      - name: scratch
        emptyDir: {}
"""
        job_file = os.path.join(self.test_dir, "job.yaml")
        with open(job_file, "w", encoding="utf-8") as f:
            f.write(complex_job)

        subprocess.run(
            [
                "python3",
                UPDATE_SCRIPT,
                "--inject",
                "--file",
                job_file,
                "--zones",
                "us-west1-a us-west1-b",
            ],
            check=True,
        )

        with open(job_file, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)

        # Verify metadata label was NOT modified or confused with container env
        self.assertEqual(parsed["metadata"]["labels"]["env"], "staging")

        # Verify sidecar container env was NOT injected
        sidecar = next(
            c
            for c in parsed["spec"]["template"]["spec"]["containers"]
            if c["name"] == "sidecar"
        )
        self.assertFalse(any(e["name"] == "EXCLUDE_ZONES" for e in sidecar["env"]))

        # Verify runner container received EXCLUDE_ZONES
        runner = next(
            c
            for c in parsed["spec"]["template"]["spec"]["containers"]
            if c["name"] == "runner"
        )
        entry = next((e for e in runner["env"] if e["name"] == "EXCLUDE_ZONES"), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["value"], "us-west1-a us-west1-b")

    def test_update_job_exclude_zones_inverted_key_order(self):
        """Test extraction and update when value: precedes name: in YAML."""
        inverted_job = """\
apiVersion: batch/v1
kind: Job
spec:
  template:
    spec:
      containers:
      - name: runner
        image: gcr.io/runner:latest
        env:
        - value: "us-central1-a us-central1-b"
          name: EXCLUDE_ZONES
        - name: CHECK_LUSTRE
          value: "true"
"""
        job_file = os.path.join(self.test_dir, "job.yaml")
        with open(job_file, "w", encoding="utf-8") as f:
            f.write(inverted_job)

        # Extract pre-existing
        res = subprocess.run(
            ["python3", UPDATE_SCRIPT, "--extract", "--file", job_file],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(res.stdout.strip(), "us-central1-a us-central1-b")

        # Update
        subprocess.run(
            [
                "python3",
                UPDATE_SCRIPT,
                "--inject",
                "--file",
                job_file,
                "--zones",
                "us-central1-a us-central1-b us-west1-b",
            ],
            check=True,
        )

        with open(job_file, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)

        runner = next(
            c
            for c in parsed["spec"]["template"]["spec"]["containers"]
            if c["name"] == "runner"
        )
        entry = next((e for e in runner["env"] if e["name"] == "EXCLUDE_ZONES"), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["value"], "us-central1-a us-central1-b us-west1-b")

    def test_update_job_exclude_zones_missing_env_fallback(self):
        """Test injection when runner container has no env block at all."""
        no_env_job = """\
apiVersion: batch/v1
kind: Job
spec:
  template:
    spec:
      containers:
      - name: runner
        image: gcr.io/runner:latest
        command: ["bash"]
"""
        job_file = os.path.join(self.test_dir, "job.yaml")
        with open(job_file, "w", encoding="utf-8") as f:
            f.write(no_env_job)

        subprocess.run(
            [
                "python3",
                UPDATE_SCRIPT,
                "--inject",
                "--file",
                job_file,
                "--zones",
                "europe-west2-c",
            ],
            check=True,
        )

        with open(job_file, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)

        runner = next(
            c
            for c in parsed["spec"]["template"]["spec"]["containers"]
            if c["name"] == "runner"
        )
        self.assertEqual(len(runner["env"]), 1)
        self.assertEqual(runner["env"][0]["name"], "EXCLUDE_ZONES")
        self.assertEqual(runner["env"][0]["value"], "europe-west2-c")

    def test_zone_exclusion_matching_token_safety(self):
        """Test that token-padded pattern matching correctly isolates zone names."""
        script = """
        check_zone() {
            local ZONE=$1
            local EXCLUDE_ZONES=$2
            if [[ " ${EXCLUDE_ZONES//,/ } " == *" ${ZONE} "* ]]; then
                echo "EXCLUDED"
            else
                echo "ALLOWED"
            fi
        }

        # Exact match in space-delimited list
        check_zone "us-west1-b" "us-west1-a us-west1-b us-west1-c"
        # Exact match in comma-delimited list
        check_zone "us-west1-b" "us-west1-a,us-west1-b,us-west1-c"
        # Partial prefix should NOT match (us-west1-b must not match us-west1-ba)
        check_zone "us-west1-ba" "us-west1-b"
        # Partial suffix should NOT match (us-west1-b must not match us-west1)
        check_zone "us-west1" "us-west1-b"
        # Single element match
        check_zone "europe-west2-c" "europe-west2-c"
        """
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        outputs = result.stdout.strip().splitlines()
        self.assertEqual(outputs[0], "EXCLUDED")
        self.assertEqual(outputs[1], "EXCLUDED")
        self.assertEqual(outputs[2], "ALLOWED")
        self.assertEqual(outputs[3], "ALLOWED")
        self.assertEqual(outputs[4], "EXCLUDED")

    def test_find_available_zone_lustre_no_defaults_and_filtering(self):
        """Test Lustre has no default exclusions and filters when configured."""
        script = """
        filter_zones() {
            local CHECK_LUSTRE=$1
            local LUSTRE_EXCLUDE_ZONES=$2
            local EXCLUDE_ZONES=$3
            local ZONES=(
                "us-west1-a" "us-west1-b" "us-west1-c"
                "us-south1-b" "europe-west2-c" "us-central1-a"
            )
            local KEPT=()

            for ZONE in "${ZONES[@]}"; do
                if [[ -n "${EXCLUDE_ZONES:-}" ]]; then
                    if [[ " ${EXCLUDE_ZONES//,/ } " == *" ${ZONE} "* ]]; then
                        continue
                    fi
                fi

                if [[ "${CHECK_LUSTRE:-false}" == "true" ]]; then
                    if [[ -n "${LUSTRE_EXCLUDE_ZONES:-}" && \
                          "${LUSTRE_EXCLUDE_ZONES}" != "none" ]]; then
                        if [[ " ${LUSTRE_EXCLUDE_ZONES//,/ } " == *" ${ZONE} "* ]]; then
                            continue
                        fi
                    fi
                fi

                KEPT+=("${ZONE}")
            done
            echo "${KEPT[*]}"
        }

        # Case 1: CHECK_LUSTRE=true, no exclusions -> all survive
        filter_zones "true" "" ""

        # Case 2: CHECK_LUSTRE=false, no exclusions -> all survive
        filter_zones "false" "" ""

        # Case 3: CHECK_LUSTRE=true, LUSTRE_EXCLUDE_ZONES="none" -> all survive
        filter_zones "true" "none" ""

        # Case 4: Space-delimited LUSTRE_EXCLUDE_ZONES -> filtered out
        filter_zones "true" "us-west1-a us-west1-b" ""

        # Case 5: Comma-delimited LUSTRE_EXCLUDE_ZONES -> filtered out
        filter_zones "true" "us-south1-b,europe-west2-c" ""

        # Case 6: Custom EXCLUDE_ZONES with CHECK_LUSTRE=false -> filtered out
        filter_zones "false" "" "us-central1-a"

        # Case 7: Both EXCLUDE_ZONES and LUSTRE_EXCLUDE_ZONES set -> both filtered
        filter_zones "true" "us-west1-a" "us-central1-a"
        """
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        )
        outputs = result.stdout.strip().splitlines()

        all_zones = (
            "us-west1-a us-west1-b us-west1-c us-south1-b europe-west2-c us-central1-a"
        )
        self.assertEqual(outputs[0], all_zones)
        self.assertEqual(outputs[1], all_zones)
        self.assertEqual(outputs[2], all_zones)
        self.assertEqual(
            outputs[3], "us-west1-c us-south1-b europe-west2-c us-central1-a"
        )
        self.assertEqual(
            outputs[4], "us-west1-a us-west1-b us-west1-c us-central1-a"
        )
        self.assertEqual(
            outputs[5], "us-west1-a us-west1-b us-west1-c us-south1-b europe-west2-c"
        )
        self.assertEqual(
            outputs[6], "us-west1-b us-west1-c us-south1-b europe-west2-c"
        )

    def test_script_dir_and_job_file_portability(self):
        """Test SCRIPT_DIR dynamic resolution and custom JOB_FILE/JOB_LOGS."""
        repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        cloud_build_dir = os.path.join(repo_dir, "tools", "cloud-build")
        submit_script = os.path.join(cloud_build_dir, "submit_and_monitor_kueue_job.sh")

        # 1. Verify exact portable constructs in submit_and_monitor_kueue_job.sh
        with open(submit_script, "r", encoding="utf-8") as f:
            submit_content = f.read()

        self.assertIn(
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            submit_content,
        )
        self.assertIn(
            'JOB_FILE="${JOB_FILE:-/workspace/job.yaml}"',
            submit_content,
        )
        self.assertIn(
            'JOB_LOGS="${JOB_LOGS:-/workspace/job_logs.txt}"',
            submit_content,
        )
        self.assertIn(
            'kubectl apply -f "$JOB_FILE"',
            submit_content,
        )
        self.assertIn(
            'bash "${SCRIPT_DIR}/check_retriable_error.sh" "$JOB_LOGS"',
            submit_content,
        )
        self.assertIn(
            'python3 "${SCRIPT_DIR}/update_job_exclude_zones.py" --inject'
            ' --file "$JOB_FILE" --zones "$ACCUMULATED_EXCLUDE_ZONES"',
            submit_content,
        )

        # 2. Verify BASH_SOURCE resolution behavior when invoked from arbitrary CWD
        sub_dir = os.path.join(self.test_dir, "nested_bin")
        os.makedirs(sub_dir)
        dir_probe_script = os.path.join(sub_dir, "probe_dir.sh")
        with open(dir_probe_script, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
                'echo "$SCRIPT_DIR"\n'
            )

        probe_res = subprocess.run(
            ["bash", dir_probe_script],
            cwd=self.test_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(probe_res.stdout.strip(), sub_dir)

        # 3. Test fallback when JOB_FILE and JOB_LOGS are unset
        res_default = subprocess.run(
            [
                "bash",
                "-c",
                'JOB_FILE="${JOB_FILE:-/workspace/job.yaml}"; '
                'JOB_LOGS="${JOB_LOGS:-/workspace/job_logs.txt}"; '
                'echo "$JOB_FILE|$JOB_LOGS"',
            ],
            env={
                k: v
                for k, v in os.environ.items()
                if k not in ("JOB_FILE", "JOB_LOGS")
            },
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            res_default.stdout.strip(),
            "/workspace/job.yaml|/workspace/job_logs.txt",
        )

        # 4. Test SCRIPT_DIR helpers and custom JOB_FILE / JOB_LOGS from arbitrary CWD
        custom_job_file = os.path.join(self.test_dir, "custom_job.yaml")
        with open(custom_job_file, "w", encoding="utf-8") as f:
            f.write(SAMPLE_JOB_YAML)

        custom_job_logs = os.path.join(self.test_dir, "logs", "custom_job_logs.txt")

        runner_script = os.path.join(self.test_dir, "run_portability_test.sh")
        with open(runner_script, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                "set -e\n"
                f'SCRIPT_DIR="{cloud_build_dir}"\n'
                'JOB_FILE="${JOB_FILE:-/workspace/job.yaml}"\n'
                'JOB_LOGS="${JOB_LOGS:-/workspace/job_logs.txt}"\n'
                'mkdir -p "$(dirname "$JOB_LOGS")"\n'
                'if [ -f "$JOB_FILE" ]; then\n'
                '    JOB_NAME=$(grep -m 1 -E \'^ *name:\' "$JOB_FILE" | '
                'awk \'{print $2}\' | tr -d \'"\' | tr -d "\'")\n'
                'fi\n'
                'echo "JOB_NAME=$JOB_NAME"\n'
                'ACCUMULATED_EXCLUDE_ZONES=""\n'
                'if [ -f "$JOB_FILE" ]; then\n'
                '    ACCUMULATED_EXCLUDE_ZONES=$(python3 '
                '"${SCRIPT_DIR}/update_job_exclude_zones.py" '
                '--extract --file "$JOB_FILE" 2>/dev/null || true)\n'
                'fi\n'
                'echo "INITIAL_EXCLUDE=$ACCUMULATED_EXCLUDE_ZONES"\n'
                'python3 "${SCRIPT_DIR}/update_job_exclude_zones.py" '
                '--inject --file "$JOB_FILE" --zones "us-west1-b us-central1-a"\n'
                'echo "resource exhausted" > "$JOB_LOGS"\n'
                'bash "${SCRIPT_DIR}/check_retriable_error.sh" "$JOB_LOGS"\n'
            )

        env = dict(os.environ, JOB_FILE=custom_job_file, JOB_LOGS=custom_job_logs)
        run_res = subprocess.run(
            ["bash", runner_script],
            cwd=self.test_dir,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("JOB_NAME=ml-a3-ultragpu-onspot-slurm-123456", run_res.stdout)
        self.assertIn("INITIAL_EXCLUDE=", run_res.stdout)
        self.assertTrue(os.path.exists(custom_job_logs))

        # Verify custom_job_file was correctly updated
        with open(custom_job_file, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
        runner = next(
            c
            for c in parsed["spec"]["template"]["spec"]["containers"]
            if c["name"] == "runner"
        )
        entry = next((e for e in runner["env"] if e["name"] == "EXCLUDE_ZONES"), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["value"], "us-west1-b us-central1-a")


CLOUD_BUILD_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../cloud-build")
)

# Excerpt of the real ml-tpu-v5p-onspot-slurm failure (build 42ef2f88): the TPU
# probe succeeded in us-central1-a, then the Managed Lustre instance stocked out.
LUSTRE_STOCKOUT_JOB_LOG = """\
INFO: Found 1 TPU nodes: v5psl-42ef2f-01
Deploying in ZONE: us-central1-a, MODEL: SPOT
Managed Lustre ZONE: us-central1-a
2026-11-02T21:40:37Z module.homefs.google_lustre_instance.lustre_instance: Creating...
2026-11-02T21:42:49Z Error: Error waiting to create Instance: Error waiting for \
Creating Instance: Error code 8, message: resource exhausted: not enough resources \
available to fulfill the request in us-central1-a

  with module.homefs.google_lustre_instance.lustre_instance,
  on ../_modules/embedded/modules/file-system/managed-lustre/main.tf line 98
"""

TPU_STOCKOUT_JOB_LOG = """\
Deploying in ZONE: us-west1-c, MODEL: STANDARD
Error: Error creating Node: googleapi: Error 429: ZONE_RESOURCE_POOL_EXHAUSTED
  with module.slurm_controller.google_tpu_v2_vm.tpu,
"""

FAKE_GCLOUD = """\
#!/bin/bash
args="$*"
zone=$(echo "$args" | sed -n 's/.*--zone=\\([^ ]*\\).*/\\1/p')
case "$args" in
"storage cat"*) cat "$FAKE_OPTIONS" ;;
"lustre locations list"*) cat "$FAKE_LUSTRE_ZONES" ;;
"compute tpus tpu-vm create"*)
	if [[ " $FAKE_TPU_OK_ZONES " == *" $zone "* ]]; then
		echo "$zone" >>"$FAKE_STATE"
	else
		echo "There is no more capacity in the zone \\"$zone\\"" >&2
		exit 1
	fi
	;;
"compute tpus tpu-vm list"*)
	if [ -f "$FAKE_STATE" ] && grep -qx "$zone" "$FAKE_STATE"; then
		echo "v5psl-abcdef-01"
	fi
	;;
"compute tpus tpu-vm delete"*) sed -i "/^$zone\\$/d" "$FAKE_STATE" ;;
*)
	echo "unexpected gcloud call: $args" >&2
	exit 1
	;;
esac
"""


class TestLustreAwareZoneRetry(unittest.TestCase):
    """Regression tests for b/580866570 (TPU v5e/v5p Slurm + Managed Lustre)."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.bin_dir = os.path.join(self.test_dir, "bin")
        os.makedirs(self.bin_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _write(self, name, content, executable=False):
        path = os.path.join(self.test_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if executable:
            os.chmod(path, 0o755)
        return path

    def _runner_env(self, job_file):
        with open(job_file, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
        runner = next(
            c
            for c in parsed["spec"]["template"]["spec"]["containers"]
            if c["name"] == "runner"
        )
        return {e["name"]: e["value"] for e in runner["env"]}

    def test_update_job_exclude_zones_custom_var(self):
        """--var LUSTRE_EXCLUDE_ZONES must not touch EXCLUDE_ZONES."""
        job_file = self._write("job.yaml", SAMPLE_JOB_YAML)
        subprocess.run(
            [
                "python3", UPDATE_SCRIPT, "--inject", "--var",
                "LUSTRE_EXCLUDE_ZONES", "--file", job_file, "--zones",
                "us-central1-a",
            ],
            check=True,
        )
        env = self._runner_env(job_file)
        self.assertEqual(env["LUSTRE_EXCLUDE_ZONES"], "us-central1-a")
        self.assertNotIn("EXCLUDE_ZONES", env)

        def extract(*extra):
            return subprocess.run(
                ["python3", UPDATE_SCRIPT, "--extract", "--file", job_file, *extra],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

        self.assertEqual(extract("--var", "LUSTRE_EXCLUDE_ZONES"), "us-central1-a")
        self.assertEqual(extract(), "")

        # Updating one list keeps the other intact.
        subprocess.run(
            ["python3", UPDATE_SCRIPT, "--inject", "--file", job_file,
             "--zones", "europe-west4-b"],
            check=True,
        )
        subprocess.run(
            ["python3", UPDATE_SCRIPT, "--inject", "--var", "LUSTRE_EXCLUDE_ZONES",
             "--file", job_file, "--zones", "us-central1-a us-west1-c"],
            check=True,
        )
        env = self._runner_env(job_file)
        self.assertEqual(env["EXCLUDE_ZONES"], "europe-west4-b")
        self.assertEqual(env["LUSTRE_EXCLUDE_ZONES"], "us-central1-a us-west1-c")

    def test_update_job_exclude_zones_rejects_invalid_var(self):
        job_file = self._write("job.yaml", SAMPLE_JOB_YAML)
        res = subprocess.run(
            ["python3", UPDATE_SCRIPT, "--extract", "--var", "BAD NAME",
             "--file", job_file],
            capture_output=True, text=True,
        )
        self.assertNotEqual(res.returncode, 0)

    def test_classify_exhausted_resource(self):
        script = os.path.join(CLOUD_BUILD_DIR, "classify_exhausted_resource.sh")
        cases = {
            LUSTRE_STOCKOUT_JOB_LOG: "lustre",
            TPU_STOCKOUT_JOB_LOG: "compute",
            "resource exhausted\n": "compute",
        }
        for i, (log, expected) in enumerate(cases.items()):
            log_file = self._write(f"log_{i}.txt", log)
            res = subprocess.run(
                ["bash", script, log_file], capture_output=True, text=True,
                check=True,
            )
            self.assertEqual(res.stdout.strip(), expected, msg=log)

    def _run_find_zone(self, extra_env):
        self._write("bin/gcloud", FAKE_GCLOUD, executable=True)
        env = dict(
            os.environ,
            PATH=f"{self.bin_dir}:{os.environ['PATH']}",
            FAKE_OPTIONS=self._write(
                "options.txt", "us-east5-a\neurope-west4-b\nus-central1-a\n"
            ),
            FAKE_LUSTRE_ZONES=self._write(
                "lustre_zones.txt", "us-central1-a\nus-central1-b\neurope-west4-b\n"
            ),
            FAKE_STATE=os.path.join(self.test_dir, "tpu_state"),
            FAKE_TPU_OK_ZONES="us-central1-a",
            PROJECT_ID="proj",
            BUILD_ID="abcdef123",
            INSTANCE_PREFIX="v5psl",
            NUM_NODES="1",
            MACHINE_TYPE="tpu",
            ACCELERATOR_TYPE="v5p-8",
            TPU_RUNTIME_VERSION="v2-alpha-tpuv5",
            OPTIONS_GCS_PATH="gs://bucket/opts.txt",
            CHECK_LUSTRE="true",
            REQUIRED_LUSTRE_GB="0",
        )
        env.update(extra_env)
        script = os.path.join(CLOUD_BUILD_DIR, "find_available_zone.sh")
        return subprocess.run(
            ["bash", "-c",
             f'source "{script}" && '
             'echo "RESULT=$ZONE|$LUSTRE_ZONE|$PROVISIONING_MODEL"'],
            env=env, capture_output=True, text=True,
        )

    def test_find_zone_reservation_skips_probe(self):
        """Reserved capacity deploys even when every zone is stocked out."""
        res = self._run_find_zone(
            {
                "TPU_RESERVATION_NAME": "ctk-v5p-daily",
                "TPU_RESERVATION_ZONE": "us-central1-a",
                "FAKE_TPU_OK_ZONES": "",
                "LUSTRE_CROSS_ZONE": "true",
                "LUSTRE_EXCLUDE_ZONES": "us-central1-a",
            }
        )
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)
        self.assertIn("skipping capacity probe", res.stdout)
        self.assertIn("RESULT=us-central1-a|us-central1-b|RESERVATION", res.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "tpu_state")))

    def test_find_zone_reservation_requires_zone(self):
        res = self._run_find_zone({"TPU_RESERVATION_NAME": "ctk-v5p-daily"})
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("TPU_RESERVATION_ZONE must be set", res.stderr)

    def test_find_zone_cross_zone_lustre_keeps_tpu_zone(self):
        """A Lustre stockout zone no longer discards the only TPU-capable zone."""
        res = self._run_find_zone(
            {"LUSTRE_EXCLUDE_ZONES": "us-central1-a", "LUSTRE_CROSS_ZONE": "true"}
        )
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)
        self.assertIn("Deploying in ZONE: us-central1-a", res.stdout)
        self.assertIn("Managed Lustre ZONE: us-central1-b", res.stdout)
        self.assertIn("RESULT=us-central1-a|us-central1-b", res.stdout)

    def test_find_zone_same_zone_lustre_by_default(self):
        res = self._run_find_zone({})
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)
        self.assertIn("RESULT=us-central1-a|us-central1-a", res.stdout)

    def test_find_zone_without_cross_zone_honours_lustre_exclusion(self):
        """Legacy behaviour (no LUSTRE_CROSS_ZONE) still skips the zone."""
        res = self._run_find_zone({"LUSTRE_EXCLUDE_ZONES": "us-central1-a"})
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Couldn't find a zone to deploy", res.stderr)

    def test_find_zone_cross_zone_never_leaves_region(self):
        res = self._run_find_zone(
            {
                "LUSTRE_EXCLUDE_ZONES": "us-central1-a us-central1-b",
                "LUSTRE_CROSS_ZONE": "true",
            }
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(
            "No eligible Managed Lustre zone in region us-central1", res.stdout
        )

    def _run_harness(self, job_logs_per_attempt):
        """Runs submit_and_monitor_kueue_job.sh with stubbed kubectl/gcloud."""
        harness_dir = os.path.join(self.test_dir, "cloud-build")
        os.makedirs(harness_dir)
        for name in (
            "submit_and_monitor_kueue_job.sh",
            "check_retriable_error.sh",
            "classify_exhausted_resource.sh",
            "update_job_exclude_zones.py",
        ):
            shutil.copy(os.path.join(CLOUD_BUILD_DIR, name), harness_dir)
        for i, log in enumerate(job_logs_per_attempt, start=1):
            self._write(f"attempt_{i}.log", log)
        # Fake monitor: prints this attempt's log; fails unless it is the last.
        self._write(
            "cloud-build/monitor_kueue_job.sh",
            "#!/bin/bash\n"
            f'n=$(cat "{self.test_dir}/attempt" 2>/dev/null || echo 0)\n'
            "n=$((n + 1))\n"
            f'echo "$n" > "{self.test_dir}/attempt"\n'
            f'cat "{self.test_dir}/attempt_$n.log"\n'
            f'[ "$n" -ge {len(job_logs_per_attempt)} ]\n',
            executable=True,
        )
        for tool in ("gcloud", "kubectl", "sleep"):
            self._write(f"bin/{tool}", "#!/bin/bash\nexit 0\n", executable=True)
        job_file = self._write("job.yaml", SAMPLE_JOB_YAML)
        env = dict(
            os.environ,
            PATH=f"{self.bin_dir}:{os.environ['PATH']}",
            JOB_FILE=job_file,
            JOB_LOGS=os.path.join(self.test_dir, "job_logs.txt"),
        )
        res = subprocess.run(
            ["bash", os.path.join(harness_dir, "submit_and_monitor_kueue_job.sh"),
             "ml-tpu-v5p-onspot-slurm", "42ef2f88"],
            env=env, capture_output=True, text=True,
        )
        return res, self._runner_env(job_file)

    def test_harness_lustre_stockout_uses_lustre_exclude_zones(self):
        """Replays build 42ef2f88: Lustre stockout must not ban the TPU zone."""
        res, env = self._run_harness([LUSTRE_STOCKOUT_JOB_LOG, "ok\n"])
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)
        self.assertIn("Managed Lustre capacity exhaustion in zone: us-central1-a",
                      res.stdout)
        self.assertEqual(env.get("LUSTRE_EXCLUDE_ZONES"), "us-central1-a")
        self.assertNotIn("EXCLUDE_ZONES", env)

    def test_harness_compute_stockout_still_uses_exclude_zones(self):
        res, env = self._run_harness([TPU_STOCKOUT_JOB_LOG, "ok\n"])
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)
        self.assertEqual(env.get("EXCLUDE_ZONES"), "us-west1-c")
        self.assertNotIn("LUSTRE_EXCLUDE_ZONES", env)


if __name__ == "__main__":
    unittest.main()
