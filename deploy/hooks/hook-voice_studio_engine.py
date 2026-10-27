"""Keep JIT and lazy-loader sources available in the frozen inference runtime.

Numba needs real source paths for its user-provided cache locator. Librosa also
loads submodules lazily, so collecting only modules observed by analysis misses
functions exercised during reference preparation.
"""

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

hiddenimports = collect_submodules("librosa") + collect_submodules("resampy")
module_collection_mode = {"librosa": "py", "resampy": "py"}
# Diffusers/Transformers validate transitive requirements via importlib.metadata.
datas = copy_metadata("chatterbox-tts", recursive=True)
