"""
Disk-backed storage for the stitcher's full-size canvas frames.

Inpaint Crop keeps one full-size canvas per frame so Inpaint Stitch can paste the result
back. For a long video that is a second full copy of the clip held in RAM from crop until
stitch, i.e. across the whole sampling and decode. With stitcher_storage="disk" the
canvases go to one raw float32 file instead (lossless) and the stitcher only carries
offsets. The file is named after the crop node, so a re-run overwrites it, and the stitch
nodes delete it once every frame has been pasted back (cleanup_disk).

Page cache counts against a container's memory limit, so the writer and reader drop the
pages they touched (posix_fadvise DONTNEED) instead of leaving gigabytes of cached file
behind.
"""

import os
import re
import sys
import tempfile
import uuid

import numpy as np
import torch

try:
    import folder_paths
except ImportError:  # outside ComfyUI (tests)
    folder_paths = None

PREFERRED_ROOT = "/root/autodl-tmp"
SUBDIR = "inpaint_stitcher"
SYNC_EVERY_BYTES = 1 << 30


def storage_root():
    root = os.environ.get("INPAINT_STITCHER_DIR")
    if not root:
        if os.path.isdir(PREFERRED_ROOT) and os.access(PREFERRED_ROOT, os.W_OK):
            root = os.path.join(PREFERRED_ROOT, SUBDIR)
        elif folder_paths is not None:
            root = os.path.join(folder_paths.get_temp_directory(), SUBDIR)
        else:
            root = os.path.join(tempfile.gettempdir(), SUBDIR)
    os.makedirs(root, exist_ok=True)
    return root


def canvas_path(unique_id):
    if unique_id is None:
        name = uuid.uuid4().hex
    else:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", str(unique_id))
    return os.path.join(storage_root(), f"canvas_{name}.f32")


def _drop_page_cache(fd, offset=0, length=0):
    if hasattr(os, "posix_fadvise"):
        try:
            os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass


class CanvasWriter:
    """Appends canvases to <path>.part and renames it to <path> on close."""

    def __init__(self, path):
        self.path = path
        self.tmp_path = path + ".part"
        # a previous run's file must not outlive a failed rewrite
        try:
            os.remove(path)
        except OSError:
            pass
        self.file = open(self.tmp_path, "wb")
        self.offset = 0
        self.unsynced = 0

    def write(self, canvas):
        arr = canvas.detach().to("cpu", torch.float32).contiguous().numpy()
        desc = {"offset": self.offset, "shape": tuple(int(s) for s in arr.shape)}
        arr.tofile(self.file)
        self.offset += arr.nbytes
        self.unsynced += arr.nbytes
        if self.unsynced >= SYNC_EVERY_BYTES:
            self._sync()
        return desc

    def _sync(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        _drop_page_cache(self.file.fileno())
        self.unsynced = 0

    def close(self):
        self._sync()
        self.file.close()
        os.replace(self.tmp_path, self.path)

    def abort(self):
        try:
            self.file.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp_path)
        except OSError:
            pass


class CanvasReader:
    def __init__(self, path):
        if not path or not os.path.isfile(path):
            raise RuntimeError(
                f"Inpaint Stitch: the stitcher's canvas file is missing ({path}). It is deleted "
                "after a stitch with cleanup_disk on; queue the workflow again so Inpaint Crop "
                "re-runs, or turn cleanup_disk off while iterating."
            )
        self.path = path
        self.file = open(path, "rb")

    def read(self, desc):
        count = int(np.prod(desc["shape"]))
        self.file.seek(desc["offset"])
        arr = np.fromfile(self.file, dtype=np.float32, count=count)
        if arr.size != count:
            raise RuntimeError(f"Inpaint Stitch: canvas file is truncated ({self.path})")
        _drop_page_cache(self.file.fileno(), desc["offset"], arr.nbytes)
        return torch.from_numpy(arr.reshape(desc["shape"]))

    def close(self):
        self.file.close()


def is_disk_stitcher(stitcher):
    return stitcher.get("canvas_storage") == "disk"


def discard_canvas_file(stitcher):
    if not is_disk_stitcher(stitcher):
        return
    path = stitcher.get("canvas_file")
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def vhs_disk_module():
    """The disk_nodes module of the VideoHelperSuite fork (VHS_DISK_MEDIA producer)."""
    for name, module in list(sys.modules.items()):
        if name.endswith("videohelpersuite.disk_nodes") and hasattr(module, "_FfmpegFrameWriter"):
            return module
    raise RuntimeError(
        "Inpaint Stitch to Disk needs the VideoHelperSuite fork with disk nodes "
        "(VHS_ImagesToDisk / VHS_DiskCombine); its disk_nodes module is not loaded."
    )
