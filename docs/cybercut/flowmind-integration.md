# FlowMind edit_* and CyberCut

FlowMind remains the product and public API: `edit_project`, `edit_status`, and
`edit_review` are still the user-facing workflow. Their execution is delegated
internally to the CyberCut media/edit/render DAG by default
(`editing.backend = "cybercut"`). Set `editing.backend = "flowmind"` in
`flowmind.config.toml` to retain the previous FlowMind pipeline during
migration. Backend errors are surfaced; the adapter never silently falls back.

CyberCut uses the existing FlowMind project ID as its idempotency key and stores
the CyberCut project/run IDs in the FlowMind project record. Local videos and
images are ingested into CyberCut's project storage. Standalone audio inputs
are rejected because the current CyberCut ingest pipeline accepts video and
image assets only. `edit_status` reads current CyberCut node states and returns
published render artifacts from CyberCut storage.

The CyberCut path also rejects requested TTS voiceover and `subtitle=false`;
voice generation is unavailable and subtitles are included in its render.
For fixed watermarks, pass `watermark_bbox` to `edit_project`; FlowMind's LaMa
inpainting preprocessing runs before the video is ingested into CyberCut. It
requires `simple-lama-inpainting` and a video-only project.

The integration requires the CyberCut PostgreSQL migrations to be applied and
the same database connection variables used by FlowMind (`FLOWMIND_PG_DSN` or
`RAK_PG_*`), FFmpeg/FFprobe on `PATH`, the `media` optional dependencies, and
optional `CYBERCUT_DATA_ROOT`. Apply migrations with:

```powershell
python -c "from cybercut.storage.db import Database; from cybercut.storage import migrate; print(migrate.apply_migrations(Database()))"
```

The embedded worker claims attempts only from the current CyberCut Run, and
FlowMind task cancellation is forwarded to that Run at node boundaries.

The current CyberCut creative provider defaults to `FakeCreativeProvider`, so
this path does not claim real AI-generated creative output. The adapter also
disables the optional ASR requirement; it does not produce speech transcripts.
TTS remains unavailable. Watermark removal is performed by FlowMind's existing
LaMa preprocessing component at the ingestion boundary, before CyberCut runs
the media/edit/render DAG.
