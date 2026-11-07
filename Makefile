.DEFAULT_GOAL := help
.NOTPARALLEL:
BUILD_JOBS ?= 2
.PHONY: help doctor check-quick check setup-host build qemu kernel rootfs tierlab tierlab-test tierlab-sanitize upstream-selftest boot-test migrate-test policy-test offline-test trace-test demo

help:
	@printf '%s\n' 'make build       Build pinned QEMU, kernel, tierlab and rootfs' 'make boot-test   Three clean Guest boots (G0)' 'make migrate-test CXL online and real page migration (G1/G2)' 'make policy-test  Native policy tests and Guest B0/B1/B2 (G3)' 'make offline-test Drain, offline, teardown (G4)' 'make trace-test   Upstream selftest and syscall/trace/query (G5)' 'make demo         Replay verified saved evidence, no build or VM' 'make check-quick Scaffold and Python tests' 'make check       Host, C++ tests and real M0–M4 Guest gates' 'make doctor      Host prerequisites'

doctor:
	python3 scripts/doctor.py --report results/host/environment.json

check-quick:
	python3 .paef/scripts/quality_gate.py --mode quick

check:
	python3 .paef/scripts/quality_gate.py --mode full

setup-host:
	sudo bash scripts/setup_host.sh "$$(id -un)"

qemu:
	python3 scripts/build.py qemu --jobs $(BUILD_JOBS)

kernel:
	python3 scripts/build.py kernel --jobs $(BUILD_JOBS)

rootfs:
	sudo bash scripts/build_rootfs.sh

tierlab:
	cmake -S . -B build/tierlab -G Ninja -DCMAKE_BUILD_TYPE=Debug
	flock -x build/heavy.lock cmake --build build/tierlab --parallel $(BUILD_JOBS)

tierlab-test: tierlab
	ctest --test-dir build/tierlab --output-on-failure --no-tests=error

tierlab-sanitize:
	cmake -S . -B build/tierlab-sanitize -G Ninja -DCMAKE_BUILD_TYPE=Debug -DTIERLAB_SANITIZERS=ON
	flock -x build/heavy.lock cmake --build build/tierlab-sanitize --parallel $(BUILD_JOBS)
	ctest --test-dir build/tierlab-sanitize --output-on-failure --no-tests=error

upstream-selftest:
	python3 scripts/build.py selftest --jobs $(BUILD_JOBS)

build: qemu kernel tierlab upstream-selftest rootfs

boot-test:
	python3 scripts/run.py boot --repeat 3

migrate-test: tierlab
	python3 scripts/run.py migrate --repeat 1

policy-test: tierlab-test
	python3 scripts/run.py policy --repeat 1

offline-test: tierlab-test
	python3 scripts/run.py offline --repeat 1

trace-test: tierlab upstream-selftest
	python3 scripts/run.py trace --repeat 1

demo:
	python3 scripts/run.py demo
