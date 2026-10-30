"""Which model does what. Only the worker (screen understanding and touch) uses Muse Wonder."""
ORCHESTRATOR_MODEL = "muse-spark-1.3"
WORKER_MODEL = "muse-wonder-1.0"
TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"  # speech to text for the chat microphone ($0.18 per hour of audio)
SLEEP_AFTER_IDLE_MIN = 5
STOP_AFTER_PAUSED_MIN = 60
OWNER_WAIT_MIN = 15  # a task waiting for the owner (a card or the phone handed over) gives the phone up after this long, so the queue never freezes
HOLD_IDLE_MIN = 10  # owner control of the phone ends by itself after this long without any input
MAX_JOB_MINUTES = 30  # wall-clock limit for one phone task, not counting time spent waiting for the owner
CRASH_LIMIT = 3  # an app that crashes this many times during one task is not going to work: stop and say so
MAX_WAIT_S = 60  # the longest single wait the worker may ask for (a download, a loading screen)
MAX_ACTIVE_PER_BOT = 5  # queued and running tasks one bot may have at once

# Screenshots cost prompt tokens (about 800 at 540 px wide, about 280 at 320 px) but barely change latency (2 to 3 s per call either way).
# Measured on 8 scenarios: 360 px passed as often as 540 px with about 28% fewer prompt tokens, and sending an image every step was
# as accurate as the text-only "adaptive" mode, which was not faster. So the default is an image every step at 360 px.
# "adaptive" attaches one only when it matters (first step, thin UI tree, stuck screen, after an error, and before accepting "done").
import os

IMAGE_MODE = os.environ.get("PHONE_IMAGE", "always")
IMAGE_WIDTH = int(os.environ.get("PHONE_IMAGE_WIDTH", "360"))
