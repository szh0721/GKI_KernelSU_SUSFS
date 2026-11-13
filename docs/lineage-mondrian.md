# LineageOS mondrian / Droidspaces

此入口为 Redmi K60 / POCO F5 Pro（`mondrian`）的 LineageOS 5.10 内核准备。
默认优先使用 GitHub 公有仓库的 Ubuntu runner；完整 LineageOS 源码构建是后备入口。
原有 GKI 工作流继续使用原来的方式。

## 与原 GKI 入口的区别

- 内核来自 `LineageOS/android_kernel_xiaomi_sm8450`，不再从 Google 通用 GKI 分支选内核。
- 使用设备原本的五个配置文件，并按照 LineageOS `kernel.mk` 默认顺序逐个合并、逐个运行 `olddefconfig`。
- 手机导出的配置只用于核对合并结果，不代替设备的 defconfig 和 fragments。
- 保留 Full LTO、CFI、调试信息和原本的 SCM 版本生成方式；不伪装官方 commit 后缀。
- Droidspaces 和 NTSync 补丁固定到完整 commit。任一补丁无法完整应用即停止。
- 不集成 SUSFS、内建 KernelSU 或其他隐藏功能；root 与容器补丁分开处理。
- 不自动刷机。

配置和版本锁定在 `config/lineage-mondrian.json`。
默认内核 commit 对应此前记录的 `eff4b40407a1...`；升级 ROM 后应重新确认源码版本并更新手机配置参考。
仅凭这份内核 commit 不能复现该 nightly 的完整 ROM。

## GitHub runner 构建

在 Actions 的 `Android 内核构建-自定义` 中：

1. `build_backend` 选择 `lineage-mondrian`。
   `lineage_build_backend` 默认 `github`；选 `patches-only` 只检查并导出补丁，
   选 `self-hosted` 使用准备好的本地 Linux runner。
2. `droidspaces` 选择 `678`。
3. 按需打开 `droidspaces_ntsync`。
4. `use_networking` 在这个后端只表示容器 NAT/macvlan/firewall 配置，不包含原 GKI 入口的 BBRv3。

内核版本仍应选 `5.10`。其他尚未移植的 GKI 选项（ZRAM 增强、BBG、KPM、
Re-Kernel、NoMount、额外安全补丁链、一加适配）若开启，会明确报错，不会静默忽略。

也可以使用独立的 `LineageOS mondrian - Droidspaces` 工作流，选择 `build_backend=github`。
新增 workflow 首次进入仓库默认分支之前，使用已经存在于默认分支的“自定义”入口，
并将运行分支选为包含本次修改的分支。

GitHub 路线：

1. 在准确的 LineageOS 内核源码上预检并合成为 `kernel.patch`，导出配置片段和来源清单。
2. 使用两个 runner 分别编译未修改基线、所选补丁版本。
3. 将原生五个 fragments 的最终合并结果与手机导出的配置逐项比较；不一致就停止。
4. 使用固定的 Android clang r596125，运行标准 Kbuild `Image modules`。
5. 保留原调试信息和 Full LTO，增加最高 32 GiB swap；不保证所有 runner 都有足够资源。
6. 比较两个版本 `Module.symvers` 的已有符号，并核对手机现有模块要求的内核符号 CRC。
7. 比较通过后生成设备受限的 AnyKernel3 包，保留原有 ramdisk、vendor_boot、vendor_dlkm 和设备模块。

这是遵循设备参数的独立 Kbuild 流程，**不是完整 LineageOS ROM 构建**。
它不重新编译高通外部模块；保留这些模块的前提是补丁保持其依赖的内核接口。
额外参考来自当前设备的 vendor_dlkm、vendor_boot DLKM ramdisk 和 boot 中的 KernelSU LKM：
共 391 个去重模块，其中 390 个 ROM 模块提供 2725 项内核符号 CRC 要求。
当前 KernelSU LKM 的版本记录为空，单独列为无法通过 CRC 核验的模块，root 仍需实际启动确认。
记录只包含模块名、代码哈希、vermagic 和符号信息，
不包含手机序列号或原始分区镜像。
该检查不等于完整 ABI 认证，产物仍标记 `device_tested=false`。
编译成功、配置一致、CRC 对照通过，均不能保证手机开机。

下载 `lineage-mondrian-installer-and-reports` 查看安装包、ABI 报告和配置差异。
`retained-module-crcs.json` 记录现有模块 CRC 比较结果。
不要把用于对照的 `baseline` artifact 当成容器补丁内核。
CI 失败时查看 `baseline` / `patched` artifact 的 `build.log` 和配置差异；
不要通过伪造版本、关闭 MODVERSIONS 或忽略失败来绕过检查。

## 可选补丁

| 选项 | 含义 |
| --- | --- |
| `off` | 未启用容器补丁的基线；NTSync 和容器网络开关也必须关闭 |
| `678` | Droidspaces 官方 SYSVIPC 6/7/8 预留位补丁和 5.10 POSIX_MQUEUE 补丁 |
| `droidspaces_ntsync` | 原仓库同源 NTSync backport，用于 Wine 等场景，默认关闭 |
| `container_networking` | Droidspaces GKI 文档列出的 NAT、macvlan、UFW/Fail2ban 配置 |

这里的 `678` 是结构体 kABI padding 位，不是手机 A/B 分区。
在默认 `eff4b40407a1...` 上，原版 `678` 和 `678 + NTSync` 已通过补丁应用预检；
按当前 K60 的使用方式，只支持 `678`，不移植或测试其他槽位。
原 GKI 工作流仍保留自己的槽位选项，但选择 LineageOS 后端时会拒绝 `123` / `345`。
补丁预检通过不代表编译或设备验证通过。

不自动启用 `CONFIG_CFS_BANDWIDTH`、`CONFIG_CGROUP_PIDS`；
Droidspaces 上游明确说明它们会改变 GKI kABI，现有 padding 补丁没有覆盖。
保留旧模块时不要为了消除容器检查提示而随意启用这些配置。

## 本地 Linux / self-hosted 后备构建

适用于云端资源不足，或需要连同设备模块和分区镜像一起重编的情况。
先按 LineageOS 设备构建文档准备 x86_64 Linux、完整源码、工具链和对应版本的 proprietary blobs，
确认无补丁版本能够构建；内核源码 HEAD 必须与补丁包一致。
不要把另一版 ROM 的设备树、模块源码和 blobs 与当前版本混用。

本地命令：

```bash
python3 scripts/lineage.py prepare \
  --kernel-source /srv/lineage/kernel/xiaomi/sm8450 \
  --droidspaces 678 --output /srv/lineage-patches

python3 scripts/lineage.py build \
  --tree /srv/lineage \
  --bundle /srv/lineage-patches \
  --output /srv/lineage-artifacts
```

第二步使用官方 `TARGET_KERNEL_CONFIG_EXT` 临时添加片段，并调用：

```bash
source build/envsetup.sh
breakfast mondrian userdebug
m kernel
m bootimage vendorbootimage vendor_dlkmimage dtboimage recoveryimage
```

正常完成或构建命令报错后，工具恢复自己临时修改的文件。
若构建期间文件被其他进程修改，保留该文件并明确报错，不覆盖该进程的修改。
强制断电或 SIGKILL 无法保证清理完成，重新运行前应检查源码状态。

如使用 GitHub self-hosted：

- runner 标签需要 `self-hosted, linux, x64, lineage`。
- 仓库变量 `LINEAGE_TREE` 指向已准备好的 Android 源码根目录。
- 独立工作流的 `build_backend` 选择 `self-hosted`。

完整树路线输出 `boot.img`、`vendor_boot.img`、`vendor_dlkm.img`、`dtbo.img`、
`recovery.img`、Image、配置、Module.symvers、源码 manifest 和日志。
可用 `--baseline-symvers /path/to/baseline/Module.symvers` 加入基线 CRC 报告。
这些镜像同样未经设备启动验证。

## Root 与设备验证

容器补丁不要求把 BakaSU 编进内核。
云端 AnyKernel3 路线保留当前 boot 的 ramdisk，但已安装的 BakaSU LKM 是否仍能加载需要实际确认。
完整树路线产出的原生 `boot.img` 不包含之前管理器的 LKM 修补，需要按所用 root 方案重新处理。

设备启动后检查：模块加载错误、网络/通话/相机/指纹/充电、root，
以及 Droidspaces 的 Settings → Requirements → Check Requirements 和实际容器启动。
先保留对应当前 ROM 的恢复镜像；本工具不执行刷机命令。

## 来源

- LineageOS 设备配置：`LineageOS/android_device_xiaomi_sm8450-common` 的 `BoardConfigCommon.mk`。
- 配置合并和完整构建：`LineageOS/android_vendor_lineage` 的 `build/tasks/kernel.mk`。
- Droidspaces：`ravindu644/Droidspaces-OSS` 的 `Documentation/Kernel-Configuration.md` 及 GKI 补丁目录。
- NTSync：原 GKI workflow 使用的 `Goldzxcbug/Droidspaces_Kernel_patch`。
- AnyKernel3：`osm0sis/AnyKernel3`，固定 commit 见配置。
