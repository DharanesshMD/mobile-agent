"""Move an installed app from one adb phone to another without any store: pull its APK files from the source and install them on the destination.

Only the app is copied: never its data, its login or any account token (those stay where they are; sharing a sign-in is done with the vault, never by copying).
"""
import shutil
import tempfile
from pathlib import Path


def move_app(src, dst, package: str) -> str:
    """Copy `package` from `src` to `dst` (both `agent.device.Device`-like with apk_paths, pull, install_apks, packages) and verify it is listed on `dst`.
    Returns a short summary; raises ValueError with a reason that is safe to show."""
    paths = src.apk_paths(package)
    if not paths:
        raise ValueError(f"{package} is not installed on the source phone")
    tmp = Path(tempfile.mkdtemp(prefix="moveapp-"))
    try:
        local = []
        for i, remote in enumerate(paths):
            target = tmp / f"{i}_{Path(remote).name}"
            src.pull(remote, str(target))
            local.append(str(target))
        size = sum(Path(f).stat().st_size for f in local)
        dst.install_apks(local)
        if package not in dst.packages():
            raise ValueError(f"the install reported success but {package} is not listed on the destination phone")
        return f"{package}: {len(local)} APK file(s), {size / 1e6:.0f} MB"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # the copies never stay on this computer
