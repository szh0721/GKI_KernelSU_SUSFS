#!/usr/bin/env python3
"""Prepare Droidspaces patches and use LineageOS's existing build targets."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "config" / "lineage-mondrian.json"
CORE_CONFIG = (
    "SYSVIPC", "POSIX_MQUEUE", "IPC_NS", "PID_NS", "DEVTMPFS", "USER_NS",
)
NETWORK_CONFIG = (
    "NETFILTER_XT_MATCH_ADDRTYPE",
    "IP6_NF_NAT", "IP6_NF_TARGET_MASQUERADE", "MACVLAN",
    "IP_NF_TARGET_REJECT", "IP6_NF_TARGET_REJECT",
    "NETFILTER_XT_TARGET_LOG", "NETFILTER_XT_MATCH_RECENT",
    "IP_SET", "IP_SET_HASH_IP", "IP_SET_HASH_NET", "NETFILTER_XT_SET",
)
IMAGES = ("boot.img", "vendor_boot.img", "vendor_dlkm.img", "dtbo.img", "recovery.img")
BUILD_SCRIPT = """\
set -eo pipefail
source build/envsetup.sh
breakfast mondrian userdebug
m kernel
m bootimage vendorbootimage vendor_dlkmimage dtboimage recoveryimage
"""


def run(*args, cwd=None, data=None):
    result = subprocess.run(
        args, cwd=cwd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"{' '.join(map(str, args))}\n{message}")
    return result.stdout


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n")


def validate_ref(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Use a complete, lowercase 40-character kernel commit.")
    return value


def clean_source(source, expected_ref):
    actual = run("git", "rev-parse", "HEAD", cwd=source).decode().strip()
    if actual != expected_ref:
        raise ValueError(f"Kernel HEAD is {actual}; bundle requires {expected_ref}.")
    if run("git", "status", "--porcelain", "--untracked-files=normal", cwd=source):
        raise ValueError("Kernel source has local changes; use a clean checkout.")


def kernel_version(source):
    makefile = (source / "Makefile").read_text()
    values = []
    for name in ("VERSION", "PATCHLEVEL", "SUBLEVEL"):
        match = re.search(rf"^{name}\s*=\s*(\d+)\s*$", makefile, re.MULTILINE)
        if not match:
            raise ValueError(f"Cannot read {name} from kernel Makefile.")
        values.append(match[1])
    if values[:2] != ["5", "10"]:
        raise ValueError("This device profile supports the mondrian 5.10 tree only.")
    return ".".join(values)


def selected_configs(slot, networking, ntsync):
    if slot == "off":
        if networking or ntsync:
            raise ValueError("Networking/NTSync require Droidspaces to be enabled.")
        return []
    configs = list(CORE_CONFIG)
    if networking:
        configs.extend(NETWORK_CONFIG)
    if ntsync:
        configs.append("NTSYNC")
    return [f"CONFIG_{name}=y" for name in configs]


def patch_sources(profile, slot, ntsync):
    if slot == "off":
        return []
    slot_name = "_".join(slot)
    base = "Documentation/resources/kernel-patches/GKI/below-kernel-6.12"
    patches = [
        (profile["droidspaces"], f"{base}/001.GKI-below-6.12-fix_sysvipc_kabi_{slot_name}.patch"),
        (profile["droidspaces"], f"{base}/002.5.10_or_lower_use_android_abi_padding_for_posix_mqueue.patch"),
    ]
    if ntsync:
        patches.extend([
            (profile["ntsync"], "NTsync/ntsync_base.patch"),
            (profile["ntsync"], "NTsync/ntsync_compat_android12-5.10.patch"),
        ])
    return patches


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "lineage-droidspaces-builder"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise


def patch_paths(patch):
    output = run("git", "apply", "--numstat", "-z", data=patch)
    paths = []
    for entry in output.split(b"\0"):
        if not entry:
            continue
        path = Path(entry.decode().split("\t", 2)[2])
        if path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
            raise ValueError(f"Unsupported patch path: {path}")
        paths.append(path)
    if not paths:
        raise ValueError("Downloaded patch contains no changes.")
    return paths


def compose_patch(source, patches):
    """Check a complete series in isolation before touching the Android tree."""
    paths = sorted({path for _, content in patches for path in patch_paths(content)})
    with tempfile.TemporaryDirectory(prefix="lineage-patch-check-") as temporary:
        scratch = Path(temporary)
        run("git", "init", "-q", str(scratch))
        for path in paths:
            original = source / path
            if original.is_file():
                (scratch / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, scratch / path)
        run("git", "add", ".", cwd=scratch)
        run(
            "git", "-c", "user.name=Patch Validator", "-c",
            "user.email=patch-validator@localhost", "-c", "commit.gpgsign=false",
            "commit", "-q", "--allow-empty", "-m", "Patch validation baseline", cwd=scratch,
        )
        for name, content in patches:
            try:
                run("git", "apply", "--check", "--whitespace=nowarn", cwd=scratch, data=content)
                run("git", "apply", "--whitespace=nowarn", cwd=scratch, data=content)
            except RuntimeError as error:
                raise ValueError(
                    f"Patch does not apply completely: {name}\n{error}\n"
                    "No kernel source was changed. Select another slot or port the patch."
                ) from error
        run("git", "add", "-A", cwd=scratch)
        combined = run("git", "diff", "--cached", "--binary", "--full-index", cwd=scratch)
    if combined:
        run("git", "apply", "--check", "--whitespace=nowarn", cwd=source, data=combined)
    return combined, [str(path) for path in paths]


def prepare(args):
    profile = load_json(PROFILE)
    source = args.kernel_source.resolve()
    kernel_ref = validate_ref(args.kernel_ref or profile["kernel_ref"])
    configs = selected_configs(args.droidspaces, args.networking, args.ntsync)
    clean_source(source, kernel_ref)
    version = kernel_version(source)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Bundle output must be a new or empty directory.")

    patches = []
    provenance = []
    for origin, path in patch_sources(profile, args.droidspaces, args.ntsync):
        url = f"https://raw.githubusercontent.com/{origin['repository']}/{origin['ref']}/{path}"
        content = download(url)
        patches.append((Path(path).name, content))
        provenance.append({"url": url, "sha256": sha256(content)})
    combined, paths = compose_patch(source, patches)
    fragment = ("# Generated Droidspaces configuration for LineageOS mondrian.\n"
                + "\n".join(configs) + "\n").encode()
    manifest = {
        "schema": 1, "device": profile["device"],
        "kernel_repository": profile["kernel_repository"],
        "kernel_ref": kernel_ref, "kernel_version": version,
        "kernel_source": profile["kernel_source"], "board_config": profile["board_config"],
        "selection": {"droidspaces": args.droidspaces, "ntsync": args.ntsync,
                      "networking": args.networking},
        "configs": configs, "paths": paths, "patch_sources": provenance,
        "files": {"kernel.patch": sha256(combined), "droidspaces.config": sha256(fragment)},
        "abi_status": "not_compared", "device_tested": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "kernel.patch").write_bytes(combined)
    (output / "droidspaces.config").write_bytes(fragment)
    write_json(output / "manifest.json", manifest)
    print(f"Verified patch series for {version} at {kernel_ref}.")
    print(f"Bundle: {output}")


def checked_bundle(bundle):
    manifest = load_json(bundle / "manifest.json")
    profile = load_json(PROFILE)
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported bundle schema.")
    for key in ("device", "kernel_repository", "kernel_source", "board_config"):
        if manifest[key] != profile[key]:
            raise ValueError(f"Bundle does not match this device profile: {key}")
    validate_ref(manifest["kernel_ref"])
    for name in ("kernel.patch", "droidspaces.config"):
        if file_sha256(bundle / name) != manifest["files"][name]:
            raise ValueError(f"Bundle checksum mismatch: {name}")
    selection = manifest["selection"]
    expected = selected_configs(
        selection["droidspaces"], selection["networking"], selection["ntsync"]
    )
    if manifest["configs"] != expected:
        raise ValueError("Bundle config does not match its selected features.")
    patch = (bundle / "kernel.patch").read_bytes()
    actual_paths = set(map(str, patch_paths(patch))) if patch else set()
    if actual_paths != set(manifest["paths"]):
        raise ValueError("Bundle patch paths do not match its manifest.")
    return manifest


def verify_config(config_path, expected):
    actual = set(config_path.read_text().splitlines())
    missing = [line for line in expected if line not in actual]
    if missing:
        raise ValueError("Final kernel configuration lost selected options: " + ", ".join(missing))
    if "CONFIG_KSU_SUSFS=y" in actual:
        raise ValueError("SUSFS is enabled in this tree; this profile does not enable it.")


def compare_abi(baseline, current):
    def symbols(path):
        result = {}
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) < 2 or not fields[0].startswith("0x"):
                raise ValueError(f"Malformed symbol version line in {path}: {line}")
            if fields[1] in result and result[fields[1]] != fields[0]:
                raise ValueError(f"Conflicting CRCs for {fields[1]} in {path}")
            result[fields[1]] = fields[0]
        if not result:
            raise ValueError(f"No exported symbols found in {path}")
        return result

    old, new = symbols(baseline), symbols(current)
    changed = sorted(name for name in old.keys() & new.keys() if old[name] != new[name])
    removed = sorted(old.keys() - new.keys())
    return {
        "status": "unchanged" if not changed and not removed else "changed",
        "baseline_sha256": file_sha256(baseline), "current_sha256": file_sha256(current),
        "changed": changed, "removed": removed, "added": sorted(new.keys() - old.keys()),
        "note": "CRC comparison alone does not establish full ABI or device compatibility.",
    }


def check_retained_modules(reference, symvers):
    exports = {}
    for line in symvers.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] == "vmlinux":
            exports[fields[1]] = fields[0]
    required = reference["kernel_required_crcs"]
    if not required or not exports:
        raise ValueError("Cannot check module CRCs without kernel requirements and vmlinux exports.")
    missing = sorted(required.keys() - exports.keys())
    mismatched = {
        name: {"required": required[name], "built": exports[name]}
        for name in sorted(required.keys() & exports.keys())
        if int(required[name], 16) != int(exports[name], 16)
    }
    return {
        "status": "matched" if not missing and not mismatched else "mismatch",
        "unique_modules": reference["unique_modules"],
        "kernel_crc_requirements": len(required),
        "missing": missing, "mismatched": mismatched,
        "scope": reference["scope"],
        "note": "Module CRC matching does not establish runtime or full ABI compatibility.",
    }


def read_config(path):
    values = {}
    for line in path.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            values[line[2:-11]] = "n"
    return values


def config_diff(before, after):
    return {
        name: {"before": before.get(name, "n"), "after": after.get(name, "n")}
        for name in sorted(before.keys() | after.keys())
        if before.get(name, "n") != after.get(name, "n")
    }


def cloud_build(args):
    """Use Kbuild with the ordered configuration merges from LineageOS kernel.mk."""
    profile = load_json(PROFILE)
    source, bundle = args.kernel_source.resolve(), args.bundle.resolve()
    output, clang = args.output.resolve(), args.clang.resolve()
    manifest = checked_bundle(bundle)
    clean_source(source, manifest["kernel_ref"])
    reference = ROOT / profile["running_config"]
    if file_sha256(reference) != profile["running_config_sha256"]:
        raise ValueError("The captured running configuration checksum changed.")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Artifact output must be a new or empty directory.")
    output.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.pop("LOCALVERSION", None)
    environment.pop("KCONFIG_CONFIG", None)
    environment["PATH"] = str(clang / "bin") + os.pathsep + environment["PATH"]
    environment["LD_LIBRARY_PATH"] = (
        str(clang / "lib64") + os.pathsep + environment.get("LD_LIBRARY_PATH", "")
    )
    compiler = subprocess.check_output(
        [str(clang / "bin/clang"), "--version"], env=environment, text=True
    ).splitlines()[0]
    if compiler != read_config(reference)["CONFIG_CC_VERSION_TEXT"].strip('"'):
        raise ValueError(f"Compiler differs from the running kernel:\n{compiler}")
    kernel_out = source.parent / f"kernel-out-{args.variant}"
    if kernel_out.exists() and any(kernel_out.iterdir()):
        raise ValueError("Use an empty kernel output directory.")
    kernel_out.mkdir(exist_ok=True)
    make = [
        "make", "-C", str(source), f"O={kernel_out}", "ARCH=arm64",
        "LLVM=1", "LLVM_IAS=1", "CLANG_TRIPLE=aarch64-linux-gnu-",
        "TARGET_PRODUCT=mondrian", "TARGET_BOARD_PLATFORM=taro",
    ]
    config_dir = source / "arch/arm64/configs"
    fragments = [config_dir / name for name in profile["config_fragments"]]
    commands = []
    shutil.copyfile(fragments[0], kernel_out / ".config")
    commands.append(make + ["olddefconfig"])
    # LineageOS defaults to olddefconfig after each fragment, not one final merge.
    for fragment in fragments[1:]:
        commands.extend([
            ["bash", str(source / "scripts/kconfig/merge_config.sh"), "-m",
             "-O", str(kernel_out), str(kernel_out / ".config"), str(fragment)],
            make + ["olddefconfig"],
        ])
    with (output / "build.log").open("wb") as log:
        def execute(command):
            print("+ " + " ".join(command), flush=True)
            process = subprocess.Popen(
                command, cwd=kernel_out, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            try:
                for line in iter(process.stdout.readline, b""):
                    log.write(line)
                    log.flush()
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
                if process.wait():
                    raise RuntimeError("Kernel build command failed; inspect build.log.")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
                process.stdout.close()

        for command in commands:
            execute(command)
        differences = config_diff(read_config(reference), read_config(kernel_out / ".config"))
        write_json(output / "reference-config-diff.json", differences)
        if differences:
            raise ValueError(
                "Official fragment merge differs from the captured running config. "
                "Inspect reference-config-diff.json before attempting a replacement kernel."
            )
        if args.variant == "patched":
            patch = (bundle / "kernel.patch").read_bytes()
            if patch:
                run("git", "apply", "--check", "--whitespace=nowarn", cwd=source, data=patch)
                run("git", "apply", "--whitespace=nowarn", cwd=source, data=patch)
            execute([
                "bash", str(source / "scripts/kconfig/merge_config.sh"), "-m",
                "-O", str(kernel_out), str(kernel_out / ".config"),
                str(bundle / "droidspaces.config"),
            ])
            execute(make + ["olddefconfig"])
            verify_config(kernel_out / ".config", manifest["configs"])
        execute(["/usr/bin/time", "-v", *make, f"-j{args.jobs}", "Image", "modules"])
    for path, name in (
        (kernel_out / ".config", "kernel.config"),
        (kernel_out / "arch/arm64/boot/Image", "Image"),
        (kernel_out / "Module.symvers", "Module.symvers"),
        (kernel_out / "System.map", "System.map"),
        (kernel_out / "include/config/kernel.release", "kernel.release"),
    ):
        shutil.copyfile(path, output / name)
    shutil.copyfile(bundle / "manifest.json", output / "manifest.json")
    write_json(output / "build-result.json", {
        "variant": args.variant, "compiler": compiler, "kernel_ref": manifest["kernel_ref"],
        "config_fragments": profile["config_fragments"], "make": make,
        "device_tested": False, "vendor_modules_rebuilt": False,
        "toolchain_ref": profile["clang_ref"],
    })


def package(args):
    baseline, patched = args.baseline.resolve(), args.patched.resolve()
    output, anykernel = args.output.resolve(), args.anykernel.resolve()
    manifest = load_json(patched / "manifest.json")
    if load_json(baseline / "manifest.json") != manifest:
        raise ValueError("Baseline and patched builds have different source/feature manifests.")
    report = compare_abi(baseline / "Module.symvers", patched / "Module.symvers")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "abi-report.json", report)
    write_json(output / "config-diff.json", config_diff(
        read_config(baseline / "kernel.config"), read_config(patched / "kernel.config")
    ))
    if report["status"] != "unchanged":
        raise ValueError(
            "Existing symbol CRCs changed or disappeared. Refusing an Image-only installer. "
            "Inspect abi-report.json; port the kABI changes or rebuild the full module set."
        )
    profile = load_json(PROFILE)
    stock_reference_path = ROOT / profile["module_reference"]
    stock_reference = load_json(stock_reference_path)
    if stock_reference["kernel_ref"] != manifest["kernel_ref"]:
        raise ValueError("Refresh the retained-module reference for this kernel commit.")
    if file_sha256(stock_reference_path) != profile["module_reference_sha256"]:
        raise ValueError("Retained-module reference checksum changed.")
    stock_report = check_retained_modules(stock_reference, patched / "Module.symvers")
    write_json(output / "retained-module-crcs.json", stock_report)
    if stock_report["status"] != "matched":
        raise ValueError("New kernel does not match retained module CRCs; refusing the installer.")
    actual = run("git", "rev-parse", "HEAD", cwd=anykernel).decode().strip()
    if actual != profile["anykernel_ref"]:
        raise ValueError("AnyKernel3 checkout differs from the pinned version.")
    shutil.copyfile(patched / "Image", output / "Image")
    shutil.copyfile(patched / "kernel.config", output / "kernel.config")
    shutil.copyfile(patched / "Module.symvers", output / "Module.symvers")
    write_json(output / "manifest.json", {
        **manifest, "abi_status": "baseline_symbol_crcs_unchanged",
        "stock_rom_module_crcs_checked": True, "device_tested": False,
        "anykernel_ref": actual,
    })
    installer = ROOT / "scripts/lineage-anykernel.sh"
    archive = output / "AnyKernel3-mondrian-LineageOS-Droidspaces.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
        for path in sorted(anykernel.rglob("*")):
            relative = path.relative_to(anykernel)
            if ".git" in relative.parts or not path.is_file():
                continue
            if relative.name == "anykernel.sh" and len(relative.parts) == 1:
                continue
            archive_file.write(path, str(relative))
        archive_file.write(installer, "anykernel.sh")
        archive_file.write(patched / "Image", "Image")
    (output / "SHA256SUMS").write_text("\n".join(
        f"{file_sha256(path)}  {path.name}"
        for path in sorted(output.iterdir()) if path.is_file() and path.name != "SHA256SUMS"
    ) + "\n")
    print(f"Baseline CRC comparison passed. Device compatibility remains untested: {archive}")


class TemporaryChanges:
    """Restore only our exact file versions; retain concurrent edits."""

    def __init__(self):
        self.before = {}
        self.after = {}

    def remember(self, path):
        if path not in self.before:
            if path.is_symlink():
                raise ValueError(f"Refusing to modify a symlink: {path}")
            self.before[path] = path.read_bytes() if path.exists() else None

    def capture(self):
        for path in self.before:
            self.after[path] = path.read_bytes() if path.exists() else None

    def restore(self):
        conflicts = []
        for path, original in reversed(list(self.before.items())):
            current = path.read_bytes() if path.exists() else None
            if path not in self.after or current != self.after[path]:
                conflicts.append(str(path))
                continue
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        if conflicts:
            raise RuntimeError("Source files changed during the build; retained them:\n"
                               + "\n".join(conflicts))


def stream_build(tree, environment, log):
    process = subprocess.Popen(
        ["bash", "-c", BUILD_SCRIPT], cwd=tree, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        with log.open("wb") as stream:
            for line in iter(process.stdout.readline, b""):
                stream.write(line)
                stream.flush()
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        if process.wait():
            raise RuntimeError(f"LineageOS build failed; see {log}")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        process.stdout.close()


def build(args):
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise ValueError("Build requires an x86_64 Linux LineageOS build host.")
    tree, bundle, output = args.tree.resolve(), args.bundle.resolve(), args.output.resolve()
    if any(character.isspace() for character in str(tree)):
        raise ValueError("The Android source path must not contain whitespace.")
    manifest = checked_bundle(bundle)
    source = tree / manifest["kernel_source"]
    clean_source(source, manifest["kernel_ref"])
    for required in (".repo", "build/envsetup.sh", "vendor/lineage/build/tasks/kernel.mk",
                     "kernel/xiaomi/sm8450-modules", "kernel/xiaomi/sm8450-devicetrees",
                     "vendor/xiaomi/mondrian", "vendor/xiaomi/sm8450-common"):
        if not (tree / required).exists():
            raise ValueError(f"Missing LineageOS dependency: {tree / required}")
    board = tree / manifest["board_config"]
    fragment = board.parent / "lineage-droidspaces.config"
    if fragment.exists() or "lineage-droidspaces.config" in board.read_text():
        raise ValueError("An existing Droidspaces integration is present; review it first.")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Artifact output must be a new or empty directory.")
    output.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "kernel.patch", "droidspaces.config"):
        shutil.copyfile(bundle / name, output / name)
    run("repo", "manifest", "-r", "-o", str(output / "source-manifest.xml"), cwd=tree)
    selection_id = sha256((bundle / "manifest.json").read_bytes())[:12]
    android_out = tree / "out" / f"lineage-droidspaces-{selection_id}"
    product = android_out / "target/product/mondrian"
    kernel_out = product / "obj/KERNEL_OBJ"
    patch = (bundle / "kernel.patch").read_bytes()
    if patch:
        run("git", "apply", "--check", "--whitespace=nowarn", cwd=source, data=patch)

    changes = TemporaryChanges()
    for path in manifest["paths"]:
        changes.remember(source / path)
    changes.remember(board)
    changes.remember(fragment)
    try:
        if patch:
            run("git", "apply", "--whitespace=nowarn", cwd=source, data=patch)
        fragment.write_bytes((bundle / "droidspaces.config").read_bytes())
        with board.open("a") as stream:
            stream.write("\n# Temporary Droidspaces fragment, managed by scripts/lineage.py.\n")
            stream.write("TARGET_KERNEL_CONFIG_EXT += device/xiaomi/mondrian/lineage-droidspaces.config\n")
        changes.capture()
        environment = os.environ.copy()
        environment["OUT_DIR"] = str(android_out.relative_to(tree))
        stream_build(tree, environment, output / "build.log")
        verify_config(kernel_out / ".config", manifest["configs"])
        for name in IMAGES:
            image = product / name
            if not image.is_file() or not image.stat().st_size:
                raise ValueError(f"Expected LineageOS image is missing: {image}")
            shutil.copyfile(image, output / name)
        for source_path, name in (
            (kernel_out / ".config", "kernel.config"),
            (kernel_out / "arch/arm64/boot/Image", "Image"),
            (kernel_out / "Module.symvers", "Module.symvers"),
            (kernel_out / "System.map", "System.map"),
            (kernel_out / "include/config/kernel.release", "kernel.release"),
        ):
            shutil.copyfile(source_path, output / name)
        report = {"status": "not_compared", "device_tested": False}
        if args.baseline_symvers:
            report = compare_abi(args.baseline_symvers, output / "Module.symvers")
            report["device_tested"] = False
        write_json(output / "abi-report.json", report)
        write_json(output / "build-result.json", {
            "android_out": str(android_out), "targets": BUILD_SCRIPT.splitlines()[-2:],
            "kernel_release": (output / "kernel.release").read_text().strip(),
            "source_state": "base manifest plus kernel.patch and generated fragment",
            "device_tested": False,
        })
    finally:
        changes.restore()
    checksums = [
        f"{file_sha256(path)}  {path.name}"
        for path in sorted(output.iterdir()) if path.is_file() and path.name != "SHA256SUMS"
    ]
    (output / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
    print(f"Build artifacts: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="Validate and export selected patches.")
    prepare_parser.add_argument("--kernel-source", type=Path, required=True)
    prepare_parser.add_argument("--kernel-ref")
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--droidspaces", choices=("off", "678", "123", "345"), default="678")
    prepare_parser.add_argument("--ntsync", action="store_true")
    prepare_parser.add_argument("--networking", action="store_true")
    prepare_parser.set_defaults(function=prepare)
    build_parser = commands.add_parser("build", help="Run the official build in an existing tree.")
    build_parser.add_argument("--tree", type=Path, required=True)
    build_parser.add_argument("--bundle", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--baseline-symvers", type=Path)
    build_parser.set_defaults(function=build)
    cloud_parser = commands.add_parser("cloud-build", help="Build Image/modules on a hosted runner.")
    cloud_parser.add_argument("--kernel-source", type=Path, required=True)
    cloud_parser.add_argument("--bundle", type=Path, required=True)
    cloud_parser.add_argument("--clang", type=Path, required=True)
    cloud_parser.add_argument("--output", type=Path, required=True)
    cloud_parser.add_argument("--variant", choices=("baseline", "patched"), required=True)
    cloud_parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    cloud_parser.set_defaults(function=cloud_build)
    package_parser = commands.add_parser("package", help="Gate an Image-only installer on baseline CRCs.")
    package_parser.add_argument("--baseline", type=Path, required=True)
    package_parser.add_argument("--patched", type=Path, required=True)
    package_parser.add_argument("--anykernel", type=Path, required=True)
    package_parser.add_argument("--output", type=Path, required=True)
    package_parser.set_defaults(function=package)
    args = parser.parse_args()
    try:
        args.function(args)
    except (RuntimeError, ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
