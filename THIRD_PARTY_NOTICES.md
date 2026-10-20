# Third-party components

Full pinned list with hashes: `requirements.lock`. Licenses of the main components (verify on each release with `pip-licenses`):

| Component | Use | License |
|---|---|---|
| OpenAI Whisper weights (`large-v3-turbo`) | transcription model, downloaded by the user from OpenAI's CDN, not redistributed | MIT |
| mlx, mlx-whisper | inference on Apple silicon | MIT |
| Silero VAD (ONNX, bundled in `dop_call_recorder/data/`) | voice activity detection | MIT |
| onnxruntime | runs Silero | MIT |
| numpy, scipy | numerics | BSD-3-Clause |
| numba | numerics | BSD-2-Clause |
| tiktoken | tokenizer | MIT |
| webrtcvad-wheels | fallback VAD | MIT |
| Pillow | screenshots for speaker-ring detection | HPND |
| PyYAML, tqdm, more-itertools, huggingface_hub | utilities | MIT / MPL-2.0 / MIT / Apache-2.0 |
| ffmpeg (Homebrew dependency, not bundled) | audio conversion | LGPL/GPL (depends on build) |
| Python 3.13 (Homebrew dependency) | runtime | PSF |
