# BlueStacks Air: how it is built, and what we can learn (1 Nov 2026)

Design study of the owner's own installed copy (`/Applications/BlueStacks.app`, 5.21.790.7505, Team ID UJYFHY4XNR). Read-only, metadata level: bundle layout,
code-signing, linked libraries, file types, the app's own log from an earlier run, and configuration key names. In our own words. Nothing was run, patched or copied
(the app was **not launched for this study**: the Mac's swap was nearly full). Device-identity data, anti-detection behaviour and licensing/ads/update logic were deliberately not studied
and are not described; they exist, and that is all this document says about them.

## 1. Architecture (text diagram)

```
macOS host (Apple M1 Pro, arm64)
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ BlueStacks.app  (one process: Qt 6 + QtWebEngine UI, 24 MB main binary, hardened runtime)   │
│                                                                                           │
│  UI / services      Qt Quick (Metal renderer), cloud client (ads, store, stats), input     │
│                     mapper, camera/mic, shortcuts, eco mode, "AI Runtime" helper app        │
│                                                                                           │
│  VMM  (libqvirt.dylib, 24 MB)  ── Hypervisor.framework (HVF) ── aarch64 Android guest       │
│        QEMU-derived, in-process, slirp user networking, entitlement com.apple.security.hypervisor
│        (a separate stock qemu-system-aarch64 10.2.0 + qemu-img + EDK2 firmware also ship)    │
│                                                                                           │
│  host↔guest RPC     "HCALL"/"GCALL" over a VM message channel (guest kernel module bstvmsg) │
│                     locale, IME, volume, time sync, activity-displayed events, enable-adb,   │
│                     install-apk, launch-activity                                           │
│                                                                                           │
│  GPU path ("AGA" engine, "PgaServer")                                                       │
│    guest apps ──GLES calls over a per-process socket──▶ guest module bstpgaipc              │
│        ──▶ host AGA/PGA server (libOpenglRender + shader translator + software ASTC decode)  │
│        ──▶ ANGLE (libEGL/libGLESv2)  ──▶ Vulkan 1.2 on MoltenVK ──▶ Metal ──▶ Apple GPU     │
│    frames reach the Qt window through IOSurface / QuartzCore                                 │
│                                                                                           │
│  adb: bundled hd-adb 1.0.41 (platform-tools 35.0.1); guest adbd forwarded on host port 5555  │
└───────────────────────────────────────────────────────────────────────────────────────────┘
Guest: Android 13 (arm64 only), Linux 5.15.137 Android common kernel (4K pages, binder, ashmem),
       custom guest drivers: pga-ipc (graphics), input, audio, camera, vm-message, gmd; initrd with busybox boot stages;
       Root.qcow2 (10 GiB virtual, 1.7 GiB used) + data template qcow2 (128 GiB virtual, 142 MB); ships Google Play + GMS + Chrome + its own launcher/store.
```

## 2. Facts table

| # | Fact | Evidence | Confidence |
|---|---|---|---|
| 1 | Bundle is 2.7 GB: 600 MB frameworks (mostly Qt 6), 1.9 GB guest images, 31 MB Qt plugins, 9.5 MB AI Runtime helper | `du` of `Contents/*` | observed |
| 2 | Executables in `MacOS/`: BlueStacks (24 MB), qemu-system-aarch64 (30 MB), qemu-img, hd-adb (13 MB), hd-aapt, ffmpeg (55 MB), 7zz, HD-DiskCompaction, HD-LogCollector, EDK2 aarch64 firmware (64 MB) | directory listing | observed |
| 3 | Hardened runtime, Developer ID now.gg Inc., notarized; entitlements: hypervisor, audio-input, camera only | `codesign -d --entitlements`, REA `inspect_signature` | observed |
| 4 | The VM runs inside the main BlueStacks process through `libqvirt.dylib`, which links `Hypervisor.framework` and slirp; the log shows one PID for UI, VM thread and graphics server | `otool -L libqvirt.dylib`; Player.log (single PID in every line) | observed (that qvirt is QEMU-derived: inferred from the name, the bundled QEMU and the device set) |
| 5 | Acceleration is Apple HVF on arm64: the guest is native aarch64, no CPU translation. Log: "Current VM Host ISA is aarch64"; guest ABI list is arm64 only | Player.log, `bluestacks.conf` key `abi_list` | observed |
| 6 | The separate `qemu-system-aarch64` is QEMU 10.2.0 (a Homebrew-style build path), linked to Hypervisor, **ParavirtualizedGraphics**, Metal, CoreVideo, CoreAudio, vmnet. Its device list includes `apple-gfx-pci`, `virtio-gpu`, `virtio-gpu-rutabaga`, virgl, virtio-net/blk/input/snd/balloon, vsock, ivshmem | REA `inspect_macho` dependencies, `strings` | observed (not shown to be what the app uses for the Android guest) |
| 7 | Host GPU renderer for the guest is **ANGLE over Vulkan 1.2 on MoltenVK on Metal**: `GL_RENDERER = ANGLE (Apple, Vulkan 1.2.250 (Apple M1 Pro), MoltenVK-0.2.2012)`, GLES 3.0/3.2 exposed | Player.log, `libEGL`/`libGLESv2` link Metal | observed |
| 8 | The guest graphics path is a custom paravirtual channel, not virtio-gpu: log tags `AGA`, `PgaServerInit: success!`, a socket per guest process ("New SOCKET connection: com.android.systemui ..."); guest kernel module `bstpgaipc.ko` | Player.log; `initrd_hvf.img` file list | observed (module's role: inferred from its name and the log) |
| 9 | Host renderer pieces: `libOpenglRender.dylib` (18 MB, the Android-emulator-style GL decoder; links OpenGL, IOSurface), `libshadertranslator`, `libHD-Astcdecoder`, ANGLE, Dawn (WebGPU native, Metal) | `otool -L`, `ls Frameworks` | observed |
| 10 | Config: `graphics_engine="aga"`, `graphics_renderer="vlcn"`, `vulkan_supported="1"`, `astc_decoding_mode="software"`, `enable_vsync=1`, `max_fps=60`, eco-mode fps cap 5, per-instance `cpus`, `ram`, `dpi`, `fb_width/height` (1080x1920 at 320 dpi), memory options (dedup, swap, page-cache limits), camera and mic devices | `bluestacks.conf` key names and non-identity values | observed (meaning of `vlcn`: inferred, probably a Vulkan-based renderer) |
| 11 | Guest kernel: Linux 5.15.137 Android common kernel, clang 14, `PREEMPT`, 4K pages, built 10 Sep 2026; has binder (`binder,hwbinder,vndbinder`), ashmem, 9p over virtio, binderfs control | `kernel_hvf` header and strings | observed |
| 12 | Boot: UEFI-less direct kernel boot (`kernel_hvf` + `initrd_hvf.img`, busybox stages) then Android 13 init; boot to launcher in about 87 s on this Mac (first run includes setup) | file list, spike measurement | observed |
| 13 | Guest has its own services: a command processor (`com.bluestacks.BstCommandProcessor`), launcher, game store, Google Play and GMS present | Player.log | observed |
| 14 | adb: bundled `hd-adb`; setting `enable_adb_access` is `0` by default, yet a listener on `*:5555` exists; shell is restricted until enabled | `bluestacks.conf`, spike notes | observed (earlier spike) |
| 15 | Instances are managed by the UI process (`Tiramisu64` is the one instance, `next_vm_id` counter, multi-instance manager keys); stop is `SIGTERM` or UI quit; pause is not a feature (SIGSTOP works, memory stays) | conf keys; spike | observed |
| 16 | No headless mode found: Qt GUI process, the VM thread is tied to the player window lifecycle | log lifecycle states (Initializing, StartingKernel, StartingAndroid, Ready, Stopping) | inferred |
| 17 | It contacts BlueStacks cloud hosts at start (config updates, promotions, upgrade check), an IP-echo service, and NTP; sends boot/app-click stats; sets a Google advertising id in the guest | Player.log (39 cloud lines in one session) | observed |
| 18 | An "AI Runtime" helper app (`bair://` URL scheme, Qt WebEngine UI) and config keys `openclaw_*` (gateway, ssh and vnc local ports, tunnel flags) show an AI-agent integration exists | helper `Info.plist`, conf key names | observed (what it does: unknown) |
| 19 | Memory: default 4 cores / 4 GB; RSS of the UI process alone was 477 MB 3.5 minutes after launch, the guest adds its RAM | `bluestacks.conf`, `ps` | observed |

## 3. What is unknown
- Whether the in-process `libqvirt` is QEMU proper or a smaller custom VMM; its device model beyond what the guest modules imply.
- The exact wire protocol between the guest `bstpgaipc` channel and the host AGA server, and whether guest Vulkan is translated or only GLES is (config says Vulkan is supported).
- What `vlcn` and `gmd` stand for; how the guest sees touch input and the camera at the protocol level.
- Partition layout inside `Root.qcow2` (not mounted), the contents of the guest's system image beyond package names seen in the log.
- Real frame rate and GPU load under a game (no run was made); whether eco mode freezes the guest or just caps fps.
- Which host features the AI Runtime / OpenClaw integration exposes.

## 4. What we can learn for our stack (free routes to GPU for an Android guest on an M1)

The headline: **BlueStacks gets host-GPU acceleration the same way the official Android Emulator does** (forward the guest's OpenGL ES calls to a host renderer, translated by ANGLE to Vulkan on MoltenVK to Metal). It is not magic and not a special OS. What is proprietary is the paravirtual guest drivers and the Qt shell around it.

| Route | What it is | Free? | Realistic on M1 | Effort | Notes |
|---|---|---|---|---|---|
| A. **Official Android Emulator, host GPU** (already ours: `scripts/emulator.sh`) | Same idea as BlueStacks (gfxstream/ANGLE/MoltenVK/Metal) | yes | yes, works today | 0 h (tune) | Hostile-app problem is separate from GPU; this is our "emulator" phone. Try `-gpu host`, 720x1600, `-memory 2048` |
| B. **QEMU 10.2 (HVF) + `virtio-gpu-rutabaga` with the gfxstream backend** and a Cuttlefish-style Android guest image | Upstream QEMU already contains the rutabaga device (BlueStacks' own QEMU lists it) | yes | possible, unproven on macOS for Android | 10 to 20 h, high risk | Needs an Android 13/14 arm64 image with the gfxstream guest HAL (AOSP cuttlefish target), `-accel hvf -device virtio-gpu-rutabaga,gfxstream-vulkan=on,...`, rutabaga/gfxstream built for macOS with MoltenVK |
| C. **QEMU + `virtio-gpu-gl` (virglrenderer on ANGLE/Metal)** | Mesa virgl guest, GL only | yes | GL ES 3.x games only | 8 to 12 h | UTM already ships this path; Android guest needs Mesa virgl + minigbm gralloc |
| D. **`apple-gfx-pci`** (Apple ParavirtualizedGraphics, in QEMU 10.2) | Meant for macOS guests | yes | **no** for Android | n/a | The guest needs Apple's driver; BlueStacks' build contains the device but its logs show its own AGA path instead |
| E. **libkrun / krunkit** (virtio-gpu Venus Vulkan on macOS, used by Podman machine) running Redroid in the guest | Linux VM with Vulkan through MoltenVK | yes | maybe | 10 to 16 h, high risk | Would give Redroid GPU instead of SwiftShader; needs a guest kernel with binder, Mesa Venus, Redroid `gpu_mode=host` |
| F. Lima `vz` (current games phone) | Virtualization.framework VM | yes | works, **no 3D GPU** (software ANGLE/SwiftShader) | 0 h | Our measured results: ordinary GLES games run, Mindustry heavier |
| G. Build a custom paravirtual GLES channel like `bstpgaipc` | Own guest kernel module + host renderer | yes | yes but huge | weeks | Not for this project |

Practical reading for us: nothing about BlueStacks' graphics needs to be reproduced. If a game needs GPU, route it to the **tuned official emulator (A)**; if the game refuses the emulator, the games phone (F) runs light games in software; a real phone covers the rest. Route B is the only free route that could give the games phone real GPU and is a research task, not a demo task.

Other design lessons worth borrowing (ideas only):
1. **A host↔guest RPC channel** (their HCALL/GCALL) for things adb is bad at: activity-displayed events, install-apk status, locale/time sync. We could get the same from an on-guest helper app plus a socket, as our Android client app already does on real phones.
2. **Eco mode** (max 5 fps when idle) and **memory dedup/swap options**: cheap ways to keep many phones alive on a small Mac; our `docker pause` and `SIGSTOP` already beat it.
3. **Software ASTC decoding** on the host: the cost of compressed textures is a known pain; worth knowing if a game looks wrong.
4. It exposes adb on all interfaces and phones home: do not copy that; keep ours loopback-only.

## 5. Recommendation: ignore for the product, wrap only on request
- **Adopt: no.** It is a heavy Qt desktop app (needs a window, 4 GB guest, no headless mode, no real command-line control), it carries ads and telemetry, and listens on `*:5555`. Nothing in it is something we cannot do with free pieces.
- **Wrap: only if the owner wants it.** If the owner turns on Android Debug Bridge in its Settings and notes the port, it can be a normal `ADB_PHONES` entry (`bluestacks=<LAN address>:<port>`), with the firewall blocking that port from other machines. No code is needed beyond what exists.
- **Ignore for the demo.** The emulator (Play Store, host GPU), the games phone (light games) and the real-phone app already cover the hybrid story; BlueStacks adds memory pressure on a machine that is already swapping.

## 6. State left behind
- REA session closed; no extracted copies were made; temp string dumps deleted.
- BlueStacks was **not started by this study**. It was found running (started 18:54 on 1 Nov, parent pid 1, 477 MB and growing) while the Mac's swap was almost full (4.8 of 5.1 GB). If the owner did not start it, quitting it frees about 2 GB.
