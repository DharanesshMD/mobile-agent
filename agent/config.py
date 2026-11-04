"""Which model does what. Only the worker (screen understanding and touch) uses Muse Wonder."""
ORCHESTRATOR_MODEL = "muse-spark-1.3"
WORKER_MODEL = "muse-wonder-1.0"
OPEN_WEIGHT_MODEL = "meta-models/muse-spark-open-1026"  # Muse Spark Open (open weights) on Baseten Model APIs; the orchestrator can run on it (ORCHESTRATOR_BACKEND=baseten)
OPEN_WEIGHT_BASE_URL = "https://inference.baseten.co/v1"  # OpenAI-compatible; auth is `Authorization: Bearer $BASETEN_API_KEY`
TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"  # speech to text for the chat microphone ($0.18 per hour of audio)
import os

SLEEP_AFTER_IDLE_MIN = int(os.environ.get("PHONE_SLEEP_AFTER_MIN", "15"))  # the phone is paused after this long without work or an owner who is looking at the page
STOP_AFTER_PAUSED_MIN = int(os.environ.get("PHONE_STOP_AFTER_MIN", "180"))  # and stopped entirely after this long paused (a cold boot takes a minute, so this is long)
OWNER_WAIT_MIN = 15  # a task waiting for the owner (a card or the phone handed over) gives the phone up after this long, so the queue never freezes
HANDOFF_IDLE_S = 10  # a handed-over step counts as finished when the owner touched the phone and then left it alone this long
HOLD_IDLE_MIN = 10  # owner control of the phone ends by itself after this long without any input
MAX_JOB_MINUTES = 30  # wall-clock limit for one phone task, not counting time spent waiting for the owner
CRASH_LIMIT = 3  # an app that crashes this many times during one task is not going to work: stop and say so
CHAIN_MAX = 4  # how many phones an app that cannot run is tried on, in order, before the agent stops and explains
MAX_WAIT_S = 60  # the longest single wait the worker may ask for (a download, a loading screen)
MAX_ACTIVE_PER_BOT = 5  # queued and running tasks one bot may have at once

# Screenshots cost prompt tokens (about 800 at 540 px wide, about 280 at 320 px) but barely change latency (2 to 3 s per call either way).
# Measured on 8 scenarios: 360 px passed as often as 540 px with about 28% fewer prompt tokens, and sending an image every step was
# as accurate as the text-only "adaptive" mode, which was not faster. So the default is an image every step at 360 px.
# "adaptive" attaches one only when it matters (first step, thin UI tree, stuck screen, after an error, and before accepting "done").
import os

IMAGE_MODE = os.environ.get("PHONE_IMAGE", "always")
IMAGE_WIDTH = int(os.environ.get("PHONE_IMAGE_WIDTH", "360"))
