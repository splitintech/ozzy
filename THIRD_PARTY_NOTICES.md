# Third-party components

Voice Studio's own source is MIT-licensed. Distributed components retain their upstream licenses. The release pipeline must include its generated dependency inventory/SBOM, applicable license texts, and corresponding source information before distributing a runtime.

| Component | Source | License family / release treatment |
|---|---|---|
| Chatterbox | https://github.com/resemble-ai/chatterbox | MIT; pinned commit in model-lock.json and uv.lock |
| Perth | https://github.com/resemble-ai/Perth | MIT; pinned commit; watermarking remains enabled |
| Chatterbox weights | https://huggingface.co/ResembleAI | Preserve the model card and license for each pinned revision |
| PyTorch / torchaudio | https://github.com/pytorch | Preserve upstream license/third-party notices |
| React / Vite / Tauri | Official project repositories in dependency inventories | Preserve licenses and notices for the locked dependency graph |
| FFmpeg / ffprobe | https://ffmpeg.org | License obligations depend on the actual build flags and linked libraries; include exact build provenance, license text, and corresponding source information |

A development runtime using locally installed FFmpeg is not automatically a redistributable release. Stable packaging requires a separately reviewed, checksum-pinned FFmpeg artifact and its notices/source information. The repository's source license does not relicense bundled executable dependencies.
