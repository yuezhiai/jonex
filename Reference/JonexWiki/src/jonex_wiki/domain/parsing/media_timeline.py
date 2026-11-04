"""Consumer-side keyframes. No modifications to upstream AV parsing are required."""
from __future__ import annotations
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def supplement_video(original: Path, output_dir: Path, *, max_frames=32, interval_seconds=5.0) -> dict:
    with original.open("rb") as stream:
        source_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    timeline = {"schema": "wiki.media_timeline.v1", "processor": "ffmpeg.scene-periodic.v1",
        "source_sha256": source_sha, "frames": [],
        "gaps": ["scene_and_periodic_samples_are_not_exhaustive", "keyframes_have_no_automatic_OCR_or_table_value_verification"]}
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        timeline["gaps"].append("ffmpeg_unavailable")
    else:
        try:
            probe = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(original)],
                capture_output=True, check=True, timeout=20)
            metadata = json.loads(probe.stdout)
            duration = float(metadata["format"]["duration"])
            timeline["duration_seconds"] = duration
            timeline["has_audio"] = any(s.get("codec_type") == "audio" for s in metadata.get("streams", []))
            folder = output_dir / "images" / "keyframes";folder.mkdir(parents=True, exist_ok=True)
            filter_text = f"select='eq(n,0)+gt(scene,0.15)+gte(t-prev_selected_t,{float(interval_seconds)})',scale='min(960,iw)':-2,showinfo"
            extracted = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-threads", "1", "-i", str(original),
                "-an", "-vf", filter_text, "-fps_mode", "vfr", "-frames:v", str(int(max_frames)),
                "-q:v", "3", str(folder / "frame-%05d.jpg")], capture_output=True, timeout=90)
            if extracted.returncode:
                timeline["gaps"].append("keyframe_extraction_failed")
            else:
                timestamps = [float(v) for v in re.findall(r"\bn:\s*\d+[^\n]*?\bpts_time:\s*([\d.]+)", extracted.stderr.decode(errors="replace"))]
                frames = sorted(folder.glob("frame-*.jpg"))
                for i, frame in enumerate(frames):
                    if i >= len(timestamps):
                        timeline["gaps"].append("unmatched_frame_timestamp");break
                    timeline["frames"].append({"path": frame.relative_to(output_dir).as_posix(),
                        "sha256": hashlib.sha256(frame.read_bytes()).hexdigest(), "start_seconds": timestamps[i],
                        "end_seconds": timestamps[i + 1] if i + 1 < len(timestamps) and i + 1 < len(frames) else duration})
                if len(frames) >= max_frames:
                    timeline["gaps"].append("frame_budget_reached; final_sample_is_not_proof_of_remaining_visual_content")
                if not frames:
                    timeline["gaps"].append("no_keyframes_extracted")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            timeline["gaps"].append("media_probe_or_extraction_unavailable")
    (output_dir / "media-timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), "utf-8")
    return timeline
