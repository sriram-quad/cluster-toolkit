#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Classifies which resource type caused a capacity / resource-exhaustion failure
# in a Kueue job log, so the retry harness can exclude the zone only for the
# resource that actually ran out.
#
# Prints exactly one of:
#   lustre  - a Managed Lustre instance (google_lustre_instance) was exhausted.
#             The compute zone (TPU/VM) may still be healthy and must NOT be
#             added to EXCLUDE_ZONES; use LUSTRE_EXCLUDE_ZONES instead.
#   compute - any other capacity failure (VM/TPU stockout, etc.).
#
# Usage: classify_exhausted_resource.sh <log_file>
LOG_FILE=$1
if [ -z "$LOG_FILE" ] || [ ! -f "$LOG_FILE" ]; then
	echo "Usage: $0 <log_file>" >&2
	exit 2
fi

# Terraform prints the failing resource a couple of lines after the error, e.g.
#   Error: ... resource exhausted: not enough resources available ... in us-west1-c
#
#     with module.homefs.google_lustre_instance.lustre_instance,
if grep -i -E -A6 "ZONE_RESOURCE_POOL_EXHAUSTED|not enough resources available|resource exhausted|stockout" "$LOG_FILE" |
	grep -q "google_lustre_instance"; then
	echo "lustre"
else
	echo "compute"
fi
