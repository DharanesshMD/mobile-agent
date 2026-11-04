# Documentation index

Start with the [project README](../README.md). The files below are grouped by what you want to know.

## Run it and try it
| File | What |
|---|---|
| [docker.md](docker.md) | Run everything in Docker: build, ports, data volume, what works without a phone emulator |
| [local-device.md](local-device.md) | Set up the shared Android emulator on a Mac (Apple Silicon) |
| [virtual-android.md](virtual-android.md) | Phone 2 (Redroid in a Lima VM): what is built, how to install free games, the phone list |
| [phone-client.md](phone-client.md) | The Mobile Agent app for a paired phone: feasibility, protocol, build |
| [real-phone-test.md](real-phone-test.md) | A step-by-step test on your own Android phone |
| [image-input.md](image-input.md) | Pictures in chat: upload, privacy, what the model gets |

## How it is designed and why it is safe
| File | What |
|---|---|
| [design-v2.md](design-v2.md) | Chat-first design: bots, queue, vault, phone power (the original design, built since) |
| [game-safety.md](game-safety.md) | Why and how the agent never plays games that ban bots; the web "other ways" card |
| [models.md](models.md) | Meta models used, pricing, measured behaviour |

## Evidence
| File | What |
|---|---|
| [qa-run-2026-10-31.md](qa-run-2026-10-31.md) | Live end-to-end QA run with the real models: 12 scenarios, bugs found and fixed |
| [virtual-android-spike.md](virtual-android-spike.md) | Measurements of Phone 2 (boot, memory, speed, pause) |
| [quality-log.md](quality-log.md) | Speed and cost measurements of the phone worker over time |

## Research and history
| File | What |
|---|---|
| [plan.md](plan.md) | The first plan (27 Oct) and the hackathon constraints |
| [demo-plan.md](demo-plan.md) | The demo and submission plan |
| [reference-desktop-agent.md](reference-desktop-agent.md) | Design reference study of a desktop agent app (concepts only, nothing copied) |
| [bluestacks-spike.md](bluestacks-spike.md), [bluestacks-air-architecture.md](bluestacks-air-architecture.md) | Why a desktop emulator product is not part of the design |
